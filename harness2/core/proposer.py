# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Improver sessions with file access in an isolated context workspace.

OpenCode reads output/answer.md or the final message; the Claude CLI reads the
final message. Prompts use stdin, and empty replies raise an error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .spec import ChannelSpec

SYSTEM_ARG_MAX = 120_000


MAX_CONCURRENCY = int(config.PROPOSER_CONCURRENCY)
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY) if MAX_CONCURRENCY > 0 else None


def _obj(d: dict, key: str) -> dict:
    """`d[key]` when it is a dict, else {}."""
    v = d.get(key)
    return v if isinstance(v, dict) else {}


class ProposerError(RuntimeError):
    """A call did not produce a usable reply. Never swallowed into an empty string."""

    def __init__(self, message: str, *, attempts: int = 0, cost_usd: float = 0.0):
        super().__init__(message)
        self.attempts = attempts
        self.cost_usd = cost_usd


@dataclass
class Reply:
    """One session: what came back, what it cost, and where the audit trail is."""

    label: str
    text: str
    ok: bool
    cost_usd: float = 0.0
    wall_s: float = 0.0
    attempts: int = 0
    cached: bool = False
    channel: str = ""
    error: str = ""
    audit: Path | None = None
    workspace: Path | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Proposer:
    """Runs agentic sessions on one channel, with an audit trail and a cache."""

    spec: ChannelSpec
    audit_dir: Path | None = None
    attempts: int = 2
    project: str = field(default_factory=lambda: config.VERTEX_PROJECT)
    region: str = field(default_factory=lambda: config.VERTEX_REGION)
    _calls: list[Reply] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.spec.model.strip():
            raise ValueError("Set HARNESS2_IMPROVER_MODEL before starting model sessions")

    def env(self) -> dict[str, str]:
        keep = () if self.spec.kind == "claude_cli" else config.provider_keys(self.spec.model)
        e = config.scrub_env(keep=keep)
        if self.spec.kind == "claude_cli":
            e.update(
                {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": self.project,
                    "CLOUD_ML_REGION": self.region,
                    "ANTHROPIC_MODEL": self.spec.model,
                }
            )
        else:
            e.update(
                {
                    "GOOGLE_VERTEX_PROJECT": self.project,
                    "GOOGLE_VERTEX_LOCATION": self.region,
                    "VERTEXAI_LOCATION": self.region,
                }
            )
        return e

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
    ) -> Reply:
        """Run one session. Raises ProposerError if no usable reply after `attempts`.

        `harvest` names a subpath of the session's working directory to copy out before that
        directory is destroyed -- e.g. "context/harness", which is how an edited tree comes back.
        """

        cache_key = _call_fingerprint(label, mission, system, context_dir)
        cached = self._cached(label, cache_key, require_workspace=bool(harvest))
        if cached is not None:
            self._calls.append(cached)
            return cached

        wall = int(timeout or self.spec.timeout)
        tries = max(1, int(self.attempts if max_attempts is None else max_attempts))
        last = ""
        spent_usd = 0.0
        t0 = time.time()
        for attempt in range(1, tries + 1):
            if attempt > 1:
                time.sleep(10 * (attempt - 1))
            tmp = Path(tempfile.mkdtemp(prefix="harness2-prop-"))
            try:
                if context_dir and Path(context_dir).is_dir():
                    shutil.copytree(context_dir, tmp / "context", dirs_exist_ok=True)
                with _SEM if _SEM is not None else contextlib.nullcontext():
                    text, cost, err = (
                        {"claude_cli": self._claude_cli, "codex": self._codex, "grok": self._grok, "pi": self._pi}.get(self.spec.kind, self._opencode)
                    )(tmp, mission, system, wall)
                spent_usd += cost
                if not text.strip():
                    last = err or "empty reply"
                    continue
                r = Reply(
                    label=label,
                    text=text,
                    ok=True,
                    cost_usd=spent_usd,
                    wall_s=round(time.time() - t0, 1),
                    attempts=attempt,
                    channel=self.spec.kind,
                )
                if harvest:
                    r.workspace = self._harvest(tmp, harvest, label, cache_key)
                    if r.workspace is None:
                        _drop_workspace(self._paths(label, cache_key))
                self._write_audit(r, mission, system, cache_key)
                self._calls.append(r)
                return r
            except subprocess.TimeoutExpired:
                last = f"timeout after {wall}s"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        r = Reply(
            label=label,
            text="",
            ok=False,
            wall_s=round(time.time() - t0, 1),
            attempts=tries,
            channel=self.spec.kind,
            error=last,
            cost_usd=spent_usd,
        )
        self._calls.append(r)
        self._write_audit(r, mission, system, cache_key)
        raise ProposerError(
            f"[{label}] {self.spec.kind}/{self.spec.model}: {last}",
            attempts=tries,
            cost_usd=spent_usd,
        )

    def _claude_cli(
        self, cwd: Path, mission: str, system: str, wall: int
    ) -> tuple[str, float, str]:
        cli = shutil.which("claude") or "claude"
        cmd = [
            cli,
            "--dangerously-skip-permissions",
            "-p",
            "--output-format",
            "json",
            "--effort",
            self.spec.effort,
        ]
        stdin = mission
        if system:
            if len(system) > SYSTEM_ARG_MAX:
                stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
                cmd += [
                    "--append-system-prompt",
                    "Follow the SYSTEM INSTRUCTIONS block at the top of the message.",
                ]
            else:
                cmd += ["--append-system-prompt", system]
        p = subprocess.run(
            cmd,
            input=stdin,
            cwd=cwd,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        payload = _parse_json(p.stdout)
        text = str(payload.get("result") or payload.get("text") or "")
        err = ""
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            err = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
            text = ""
        elif p.returncode != 0 and not text.strip():
            err = f"rc={p.returncode}: {(p.stderr or '')[-300:]}"
        return text, float(payload.get("total_cost_usd") or 0.0), err

    def _pi(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """Local patch: run the improver session in the pi coding agent, inside a capped container."""
        import os as _os, uuid as _uuid
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)
        ag = cwd / ".pihome" / ".pi" / "agent"
        ag.mkdir(parents=True, exist_ok=True)
        url = config.env("HARNESS2_CODEX_CONTAINER_URL") or config.CODEX_BASE_URL
        (ag / "models.json").write_text(json.dumps({"providers": {"sofie": {
            "baseUrl": url, "api": "openai-completions", "apiKey": "VERTEX_PROXY_KEY",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
            "models": [{"id": self.spec.model, "contextWindow": 100000}]}}}), encoding="utf-8")
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        name = f"h2pi-{_uuid.uuid4().hex[:10]}"
        cap = int(config.env_int("HARNESS2_PI_SESSION_CAP", 3600))
        pi_dir = config.env("HARNESS2_PI_DIR", str(Path.home() / "harness/pi"))
        cmd = ["timeout", "--kill-after=30", str(min(wall, cap)),
               "docker", "run", "--rm", "--name", name, "--user", f"{_os.getuid()}:{_os.getgid()}",
               "--add-host", "host.docker.internal:host-gateway",
               "-v", f"{cwd}:/work", "-v", f"{pi_dir}:/opt/pi:ro",
               "-e", "HOME=/work/.pihome", "-e", f"VERTEX_PROXY_KEY={config.VERTEX_PROXY_KEY}",
               "-w", "/work", "--entrypoint", "node",
               config.env("HARNESS2_CODEX_IMPROVER_IMAGE", "harness2/codex-improver:latest"),
               "/opt/pi/node_modules/@mariozechner/pi-coding-agent/dist/cli.js",
               "-p", "--no-session", "--provider", "sofie", "--model", self.spec.model, prompt]
        try:
            p = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=min(wall, cap) + 90)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        ans = out / "answer.md"
        if ans.is_file() and ans.read_text(errors="replace").strip():
            return ans.read_text(errors="replace"), 0.0, ""
        t = (p.stdout or "").strip()
        return (t, 0.0, "") if t else ("", 0.0, f"rc={p.returncode}: {(p.stderr or '')[-400:]}")

    def _grok(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """Local patch: run the improver session in Grok Build CLI, inside a throwaway container."""
        import os as _os
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)
        gh = cwd / ".grokhome" / ".grok"
        gh.mkdir(parents=True, exist_ok=True)
        url = config.env("HARNESS2_CODEX_CONTAINER_URL") or config.CODEX_BASE_URL
        m = self.spec.model
        (gh / "config.toml").write_text(
            f'[model.{m}]\nmodel = "{m}"\nbase_url = "{url}"\nname = "{m}"\n'
            'env_key = "VERTEX_PROXY_KEY"\napi_backend = "chat_completions"\ncontext_window = 100000\n\n'
            f'[models]\ndefault = "{m}"\ndefault_reasoning_effort = "medium"\n', encoding="utf-8")
        (gh / "trusted_folders.toml").write_text(
            '[folders."/work"]\ntrusted = true\ndecided_at = 1793276885\n', encoding="utf-8")
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        # Hard wall-clock cap: a session that keeps making slow model calls never trips an
        # idle timeout, so the container is named and force-removed when the cap is hit.
        import uuid as _uuid
        name = f"h2grok-{_uuid.uuid4().hex[:10]}"
        cap = int(config.env_int("HARNESS2_GROK_SESSION_CAP", 2700))
        cmd = ["timeout", "--kill-after=30", str(min(wall, cap)),
               "docker", "run", "--rm", "--name", name, "--user", f"{_os.getuid()}:{_os.getgid()}",
               "--add-host", "host.docker.internal:host-gateway",
               "-v", f"{cwd}:/work", "-v",
               f"{config.env('HARNESS2_GROK_BIN', str(Path.home() / '.grok/bin/grok'))}:/opt/grok/grok:ro",
               "-e", "HOME=/work/.grokhome", "-e", "GROK_HOME=/work/.grokhome/.grok",
               "-e", f"VERTEX_PROXY_KEY={config.VERTEX_PROXY_KEY}", "-w", "/work",
               "--entrypoint", "/opt/grok/grok",
               config.env("HARNESS2_CODEX_IMPROVER_IMAGE", "harness2/codex-improver:latest"),
               "-p", prompt, "-m", m, "--always-approve", "--disable-web-search",
               "--output-format", "json"]
        try:
            p = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=min(wall, cap) + 90)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        ans = out / "answer.md"
        if ans.is_file() and ans.read_text(errors="replace").strip():
            return ans.read_text(errors="replace"), 0.0, ""
        try:
            t = json.loads(p.stdout[p.stdout.index("{"):]).get("text", "")
        except (ValueError, AttributeError):
            t = ""
        return (t, 0.0, "") if t.strip() else ("", 0.0, f"rc={p.returncode}: {(p.stderr or p.stdout or '')[-400:]}")

    def _codex(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """Local patch: run the improver session in Codex CLI through the Responses bridge."""
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)
        home = cwd / ".codexhome"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text(
            f"model = {json.dumps(self.spec.model)}\nmodel_provider = \"harness2\"\n"
            'model_reasoning_effort = "medium"\n'
            '[model_providers.harness2]\nname = "bridge"\n'
            f"base_url = {json.dumps(config.CODEX_BASE_URL)}\n"
            'wire_api = "responses"\nenv_key = "VERTEX_PROXY_KEY"\n',
            encoding="utf-8",
        )
        # Codex's own Linux sandbox cannot start here (AppArmor blocks unprivileged user
        # namespaces), so the session runs in a throwaway Docker container that sees ONLY cwd.
        import os as _os
        url = config.env("HARNESS2_CODEX_CONTAINER_URL") or config.CODEX_BASE_URL
        (home / "config.toml").write_text(
            (home / "config.toml").read_text().replace(json.dumps(config.CODEX_BASE_URL), json.dumps(url)),
            encoding="utf-8",
        )
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        import uuid as _uuid
        cname = f"h2codex-{_uuid.uuid4().hex[:10]}"
        ccap = int(config.env_int("HARNESS2_IMPROVER_SESSION_CAP", 3600))
        cmd = ["timeout", "--kill-after=30", str(min(wall, ccap)),
               "docker", "run", "--rm", "--name", cname, "--user", f"{_os.getuid()}:{_os.getgid()}",
               "--add-host", "host.docker.internal:host-gateway",
               "-v", f"{cwd}:/work", "-v", f"{config.CODEX_DIST}:/opt/codex:ro",
               "-e", "CODEX_HOME=/work/.codexhome", "-e", "HOME=/work/.codexhome", "-e", "SHELL=/bin/bash",
               "-e", "PATH=/opt/codex/codex-path:/usr/local/bin:/usr/bin:/bin", "--entrypoint", "/opt/codex/bin/codex",
               "-e", f"VERTEX_PROXY_KEY={config.VERTEX_PROXY_KEY}", "-w", "/work",
               config.env("HARNESS2_CODEX_IMPROVER_IMAGE", "harness2/codex-improver:latest"),
               "exec", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", "-C", "/work", "-m", self.spec.model,
               "-o", "/work/output/last.md", prompt]
        try:
            p = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=min(wall, ccap) + 90)
        finally:
            subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
        for f in (out / "answer.md", out / "last.md"):
            if f.is_file() and f.read_text(errors="replace").strip():
                return f.read_text(errors="replace"), 0.0, ""
        return "", 0.0, f"rc={p.returncode}: {(p.stderr or p.stdout or '')[-400:]}"

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)

        (cwd / "opencode.json").write_text(
            json.dumps({"agent": {"build": {"steps": self.spec.steps}}}), encoding="utf-8"
        )
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        oc = _opencode_dir()

        env = self.env()
        env["OPENCODE_PERMISSION"] = json.dumps(
            {"external_directory": {"*": "deny", str(cwd): "allow", f"{cwd}/**": "allow"}}
        )
        env["XDG_DATA_HOME"] = str(cwd / ".ocdata")
        env["XDG_STATE_HOME"] = str(cwd / ".ocstate")

        ocap = int(config.env_int("HARNESS2_IMPROVER_SESSION_CAP", 3600))
        cmd = [
            "timeout", "--kill-after=30", str(min(wall, ocap)),  # local patch: hard cap on stalled streams
            str(config.BUN),
            "run",
            "--cwd",
            str(oc / "packages" / "opencode"),
            "--conditions=browser",
            "src/index.ts",
            "run",
            prompt,
            "--dir",
            str(cwd),
            "--model",
            self.spec.model,
            "--format",
            "json",
        ]
        if self.spec.effort:
            cmd += ["--variant", self.spec.effort]
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        cost = _opencode_cost(p.stdout)
        text, err = _opencode_stdout(p.stdout)
        if err:
            return "", cost, err
        ans = out / "answer.md"
        if ans.is_file() and ans.read_text(errors="replace").strip():
            return ans.read_text(errors="replace"), cost, ""
        if text.strip():
            return text, cost, ""
        tail = _strip_oc_banner(p.stderr or "").strip()[-300:]
        return "", cost, f"rc={p.returncode}: {err or tail or 'empty reply'}"

    def _harvest(self, tmp: Path, sub: str, label: str, cache_key: str = "") -> Path | None:
        """Copy `tmp/<sub>` somewhere durable before the session directory is removed."""
        src = tmp / sub
        if not src.is_dir():
            return None
        dest = Path(self.audit_dir or tempfile.mkdtemp(prefix="harness2-harvest-"))
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        dest = dest / f"{safe}.workspace"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return dest

    def _paths(self, label: str, cache_key: str = "") -> dict[str, Path] | None:
        if not self.audit_dir:
            return None
        d = Path(self.audit_dir)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        return {k: d / f"{safe}.{k}.md" for k in ("mission", "system", "response")} | {
            "meta": d / f"{safe}.meta.json",
            "workspace": d / f"{safe}.workspace",
        }

    def _cached(
        self, label: str, cache_key: str = "", *, require_workspace: bool = False
    ) -> Reply | None:
        p = self._paths(label, cache_key)
        if not p or not p["response"].is_file():
            return None
        if require_workspace and not p["workspace"].is_dir():
            return None
        txt = p["response"].read_text(encoding="utf-8", errors="replace")
        if not txt.strip():
            return None
        return Reply(
            label=label,
            text=txt,
            ok=True,
            cost_usd=0.0,
            cached=True,
            channel=self.spec.kind,
            audit=p["response"],
            workspace=p["workspace"] if p["workspace"].is_dir() else None,
        )

    def _write_audit(self, r: Reply, mission: str, system: str, cache_key: str = "") -> None:
        p = self._paths(r.label, cache_key)
        if not p:
            return
        p["mission"].write_text(mission, encoding="utf-8")
        if system:
            p["system"].write_text(system, encoding="utf-8")
        p["response"].write_text(r.text, encoding="utf-8")
        p["meta"].write_text(
            json.dumps(
                {
                    "label": r.label,
                    "ok": r.ok,
                    "channel": r.channel,
                    "model": self.spec.model,
                    "effort": self.spec.effort,
                    "attempts": r.attempts,
                    "wall_s": r.wall_s,
                    "cost_usd": r.cost_usd,
                    "error": r.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        r.audit = p["response"]


def _drop_workspace(paths: dict[str, Path] | None) -> None:
    """Remove a stale harvested workspace so a later cache read cannot serve it."""
    if paths:
        shutil.rmtree(paths["workspace"], ignore_errors=True)


def _opencode_dir() -> Path:
    """The upstream OpenCode checkout to run through bun: config.OPENCODE_DIR."""
    oc = config.OPENCODE_DIR
    if not (oc / "packages" / "opencode" / "src" / "index.ts").is_file():
        raise FileNotFoundError(
            f"no OpenCode checkout at {oc} (config.OPENCODE_DIR); run "
            f"scripts/setup_benchmark.py <benchmark> to clone the pinned commit there"
        )
    return oc


def _parse_json(stdout: str) -> dict:
    """The claude CLI's one JSON object, tolerating banners and trailing noise around it.

    Not for opencode stdout, which is a JSONL event stream: use `_opencode_stdout`.
    """
    s = (stdout or "").strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                pass
    return {"result": s}


_OC_BANNER = re.compile(
    r"^(?:Performing one time database migration[^\n]*|sqlite-migration:[^\n]*"
    r"|Database migration complete\.)[ \t]*$",
    re.M,
)


def _strip_oc_banner(s: str) -> str:
    """Drop the migration banner lines from OpenCode output. No-op when they are absent."""
    return _OC_BANNER.sub("", s or "")


def _opencode_stdout(stdout: str) -> tuple[str, str]:
    """Fold `opencode run --format json` into (final assistant text, error text).

    The stream is one raw event per line and assistant prose lives at part.text of a `text`
    event. All text events are joined, so a trailing terminator event cannot swallow the
    answer; `error` events become the error string. Plain-text stdout is returned as-is.
    """
    texts: list[str] = []
    errors: list[str] = []
    saw_json = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        saw_json = True
        part = _obj(ev, "part")
        ptype = part.get("type") or ev.get("type")
        if ptype == "text":
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t)
        elif ptype == "error":
            e = ev.get("error")
            if isinstance(e, dict):
                msg = str(_obj(e, "data").get("message") or e.get("name") or "").strip()
            else:
                msg = str(e or "").strip()
            if msg:
                errors.append(msg)
    if not saw_json:
        return _strip_oc_banner(stdout).strip(), ""
    return "\n\n".join(texts), "; ".join(errors)[-300:]


def _opencode_cost(stdout: str) -> float:
    """Sum `part.cost` over the step_finish events of an `opencode run --format json` stream."""
    total = 0.0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        part = _obj(ev, "part")
        if (part.get("type") or ev.get("type")) in ("step-finish", "step_finish"):
            c = part.get("cost")
            if isinstance(c, (int, float)):
                total += float(c)
    return round(total, 6)


def _call_fingerprint(label: str, mission: str, system: str, context_dir: Path | str | None) -> str:
    """Content-address one call, including its evidence and its mode/task workspace path.

    The resolved path is part of the key: the two modes can have byte-identical k=1 evidence
    and must still be independent draws.
    """
    h = hashlib.sha256()
    for value in (label, mission, system):
        h.update(value.encode("utf-8", errors="replace"))
        h.update(b"\0")
    if context_dir:
        root = Path(context_dir)
        h.update(str(root.resolve()).encode("utf-8", errors="replace"))
        h.update(b"\0")
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file() or "__pycache__" in p.parts:
                    continue
                h.update(str(p.relative_to(root)).encode("utf-8", errors="replace"))
                h.update(b"\0")
                try:
                    with p.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
                except OSError as exc:
                    h.update(f"<unreadable:{type(exc).__name__}:{exc}>".encode())
                h.update(b"\0")
    return h.hexdigest()[:16]
