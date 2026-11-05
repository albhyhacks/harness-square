"""PiHarnessAgent -- pi coding agent (`pi -p --mode json`) as the executor, on the codex_root
harness surface (AGENTS.md + .agents/skills + scripts, which pi also reads). Local patch.

Pi's JSON events are converted into codex-style JSONL so harness2's codex transcript reader
and the improver see the same evidence shape as for Codex and Grok."""
from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from workbuddy_bench.agents._agent_user import ensure_agent_user
from workbuddy_bench.agents.codex_harness_agent import CodexHarnessAgent

_PI_OUT = "pi-output.jsonl"
_CODEX_STYLE_OUT = "codex-output.jsonl"
_PI_CLI = "/opt/pi/node_modules/@mariozechner/pi-coding-agent/dist/cli.js"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text") if isinstance(c, dict) else c) for c in content if c)
    if isinstance(content, dict):
        return _text(content.get("content") or content.get("text") or "")
    return "" if content is None else str(content)


def pi_to_codex_events(lines):
    """Map pi --mode json events to codex-exec-style events (pure function)."""
    out, calls, usage = [], {}, {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    for raw in lines:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        t = e.get("type")
        if t == "message_end":
            m = e.get("message") or {}
            if m.get("role") == "assistant":
                txt = "\n".join(c.get("text", "") for c in m.get("content", []) if isinstance(c, dict) and c.get("type") == "text")
                if txt.strip():
                    out.append({"type": "item.completed", "item": {"type": "agent_message", "text": txt}})
                u = m.get("usage") or {}
                usage["input_tokens"] += int(u.get("input") or 0)
                usage["output_tokens"] += int(u.get("output") or 0)
                usage["cached_input_tokens"] += int(u.get("cacheRead") or 0)
        elif t == "tool_execution_start":
            a = e.get("args") or {}
            cmd = a.get("command") if isinstance(a, dict) and "command" in a else f"{e.get('toolName')} {json.dumps(a, ensure_ascii=False)}"
            calls[e.get("toolCallId")] = {"type": "command_execution", "command": str(cmd)}
        elif t == "tool_execution_end":
            c = calls.pop(e.get("toolCallId"), {"type": "command_execution", "command": str(e.get("toolName"))})
            c["aggregated_output"] = _text(e.get("result"))[:20000]
            c["exit_code"] = 1 if e.get("isError") else 0
            out.append({"type": "item.completed", "item": c})
    out.append({"type": "turn.completed", "usage": {**usage, "reasoning_output_tokens": 0}})
    return out


class PiHarnessAgent(CodexHarnessAgent):
    """pi coding agent + the codex_root harness workspace installed at /workspace."""

    def __init__(self, logs_dir: Path, *args, **kwargs):
        self._pi_cli = str(kwargs.pop("PI_CLI", _PI_CLI))
        kwargs.pop("PI_VERSION", None)
        super().__init__(logs_dir, *args, **kwargs)

    @staticmethod
    def name() -> str:
        return "pi-harness"

    def get_version_command(self) -> str | None:
        return f"node {self._pi_cli} --version"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(environment, command=(
            f'test -f "{self._pi_cli}" || {{ echo "ERROR: pi not mounted at {self._pi_cli}" >&2; exit 1; }}; '
            'command -v node >/dev/null || { echo "ERROR: node not in task image" >&2; exit 1; }; node --version'))
        await ensure_agent_user(self, environment)

    def _models_json(self, url: str) -> str:
        return json.dumps({"providers": {"sofie": {
            "baseUrl": url, "api": "openai-completions", "apiKey": "VERTEX_PROXY_KEY",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
            "models": [{"id": self._codex_model, "contextWindow": 72000}]}}})

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if self._harness_ws is not None:
            await self._materialize_harness(environment)
        instruction = self.render_instruction(instruction)
        url = (self._get_env("CODEX_BRIDGE_URL") or "").strip().rstrip("/")
        if not url or not self._has_env("VERTEX_PROXY_KEY"):
            raise RuntimeError("pi-harness needs CODEX_BRIDGE_URL and VERTEX_PROXY_KEY (job env)")
        cfg = base64.b64encode(self._models_json(url).encode()).decode()
        home_fix = 'export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"; '
        await self.exec_as_agent(environment, command=(
            f'{home_fix}mkdir -p "$HOME/.pi/agent" && echo {cfg} | base64 -d > "$HOME/.pi/agent/models.json"'))
        await self.exec_as_agent(environment, command=(
            f"{home_fix}cd /workspace && node {self._pi_cli} -p --mode json --no-session "
            f"--provider sofie --model {shlex.quote(self._codex_model)} {shlex.quote(instruction)} "
            f"</dev/null 2>/logs/agent/pi-stderr.log | tee /logs/agent/{_PI_OUT}"))

    def populate_context_post_run(self, context: AgentContext) -> None:
        src = self.logs_dir / _PI_OUT
        if src.is_file():
            ev = pi_to_codex_events(src.read_text(encoding="utf-8", errors="replace").splitlines())
            (self.logs_dir / _CODEX_STYLE_OUT).write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in ev) + "\n", encoding="utf-8")
        super().populate_context_post_run(context)
