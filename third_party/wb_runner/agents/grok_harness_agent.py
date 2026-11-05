"""GrokHarnessAgent -- Grok Build CLI (`grok -p`) as the executor, on the codex_root harness
surface (AGENTS.md + .agents/skills + scripts, which Grok also reads). Local patch.

Grok's streaming-json is converted into codex-style JSONL (agent_message / command_execution)
so harness2's existing codex transcript reader and the improver see the same evidence shape."""
from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from workbuddy_bench.agents._agent_user import ensure_agent_user
from workbuddy_bench.agents.codex_harness_agent import CodexHarnessAgent

_GROK_OUT = "grok-output.jsonl"
_CODEX_STYLE_OUT = "codex-output.jsonl"


def grok_to_codex_events(lines):
    """Map Grok streaming-json events to codex-exec-style events (pure function)."""
    out, text, calls, order = [], [], {}, []
    def flush():
        if "".join(text).strip():
            out.append({"type": "item.completed", "item": {"type": "agent_message", "text": "".join(text)}})
        text.clear()
    for raw in lines:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        t = e.get("type")
        if t == "text":
            text.append(str(e.get("data") or ""))
        elif t == "tool_call":
            flush()
            cid = e.get("toolCallId")
            ri = e.get("rawInput") or {}
            cmd = ri.get("command") if isinstance(ri, dict) and "command" in ri else f"{e.get('toolName')} {json.dumps(ri, ensure_ascii=False)}"
            calls[cid] = {"type": "command_execution", "command": str(cmd), "aggregated_output": "", "exit_code": None}
            order.append(cid)
        elif t == "tool_call_update":
            c = calls.get(e.get("toolCallId"))
            if c is None:
                continue
            ro = e.get("rawOutput")
            if ro is not None:
                c["aggregated_output"] = ro if isinstance(ro, str) else json.dumps(ro, ensure_ascii=False)
            if e.get("status") in ("completed", "failed"):
                c["exit_code"] = 0 if e["status"] == "completed" else 1
                out.append({"type": "item.completed", "item": dict(c)})
                calls.pop(e.get("toolCallId"), None)
        elif t == "end":
            flush()
            for c in calls.values():
                out.append({"type": "item.completed", "item": c})
            u = e.get("usage") or {}
            out.append({"type": "turn.completed", "usage": {
                "input_tokens": u.get("input_tokens", 0), "cached_input_tokens": u.get("cache_read_input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0), "reasoning_output_tokens": u.get("reasoning_tokens", 0)}})
    flush()
    return out


class GrokHarnessAgent(CodexHarnessAgent):
    """Grok Build CLI + the codex_root harness workspace installed at /workspace."""

    def __init__(self, logs_dir: Path, *args, **kwargs):
        self._grok_bin = str(kwargs.pop("GROK_BIN", "/opt/grok/grok"))
        kwargs.pop("GROK_VERSION", None)
        super().__init__(logs_dir, *args, **kwargs)

    @staticmethod
    def name() -> str:
        return "grok-harness"

    def get_version_command(self) -> str | None:
        return f"{self._grok_bin} --version"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(f'test -x "{self._grok_bin}" || {{ echo "ERROR: grok not mounted at {self._grok_bin}" >&2; exit 1; }}; '
                     f'ln -sf "{self._grok_bin}" /usr/local/bin/grok && grok --version'),
        )
        await ensure_agent_user(self, environment)

    def _grok_config(self, url: str) -> str:
        m = self._codex_model
        return (f'[model.{m}]\nmodel = "{m}"\nbase_url = "{url}"\nname = "{m}"\n'
                f'env_key = "VERTEX_PROXY_KEY"\napi_backend = "chat_completions"\ncontext_window = 72000\n\n'
                f'[models]\ndefault = "{m}"\n')

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if self._harness_ws is not None:
            await self._materialize_harness(environment)
        instruction = self.render_instruction(instruction)
        url = (self._get_env("CODEX_BRIDGE_URL") or "").strip().rstrip("/")
        if not url or not self._has_env("VERTEX_PROXY_KEY"):
            raise RuntimeError("grok-harness needs CODEX_BRIDGE_URL and VERTEX_PROXY_KEY (job env)")
        cfg = base64.b64encode(self._grok_config(url).encode()).decode()
        trust = base64.b64encode(b'[folders."/workspace"]\ntrusted = true\ndecided_at = 1793276885\n').decode()
        home_fix = 'export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"; export GROK_HOME="$HOME/.grok"; '
        await self.exec_as_agent(environment, command=(
            f'{home_fix}mkdir -p "$GROK_HOME" && echo {cfg} | base64 -d > "$GROK_HOME/config.toml" && '
            f'echo {trust} | base64 -d > "$GROK_HOME/trusted_folders.toml"'))
        await self.exec_as_agent(environment, command=(
            f"{home_fix}cd /workspace && grok -p {shlex.quote(instruction)} -m {shlex.quote(self._codex_model)} "
            f"--always-approve --disable-web-search --output-format streaming-json "
            f"</dev/null 2>/logs/agent/grok-stderr.log | tee /logs/agent/{_GROK_OUT}"))

    def populate_context_post_run(self, context: AgentContext) -> None:
        src = self.logs_dir / _GROK_OUT
        if src.is_file():
            ev = grok_to_codex_events(src.read_text(encoding="utf-8", errors="replace").splitlines())
            (self.logs_dir / _CODEX_STYLE_OUT).write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in ev) + "\n", encoding="utf-8")
        super().populate_context_post_run(context)
