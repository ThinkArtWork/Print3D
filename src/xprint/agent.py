"""The closed modeling loop: write, execute, inspect, revise.

A one-shot prompt produces scripts that *look* right; the difference between the
72.8% and 92.7% CAD benchmarks is that the model gets to see what its script
actually produced.  This module gives the model three tools (``write_script``,
``run_cad``, ``finish``) and executes them locally, under the same policy gate
and subprocess limits used everywhere else.

The model never touches the filesystem directly: every tool call is executed
here, and only a compact JSON summary is returned to it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .cad import DEFAULT_TIMEOUT_S, RunResult, run_script
from .errors import InvalidArgument, XPrintError
from .modelers import ChatReply, extract_code
from .modelers_policy import AGENT_PROMPT
from .script_policy import check_script

MAX_TOOL_RESULT_CHARS = 6000
MAX_ASSISTANT_CHARS = 2000


class ChatModeler(Protocol):
    """Anything that can answer a chat turn, e.g. :class:`KimiModeler`."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> ChatReply: ...


def _function(
    name: str,
    description: str,
    parameters: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": parameters,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _function(
        "write_script",
        (
            "Replace the current model.py with a complete build123d script. "
            "Nothing is executed until run_cad is called."
        ),
        {
            "code": {
                "type": "string",
                "description": "The full Python source of model.py (no markdown fence).",
            }
        },
        ["code"],
    ),
    _function(
        "run_cad",
        (
            "Execute model.py in XPrint's sandbox and return real geometry checks "
            "(bounding box, volume, solids, watertightness) and any error output."
        ),
        {},
    ),
    _function(
        "finish",
        "End the modeling session. Call this once the part is printable or you are out of ideas.",
        {
            "summary": {
                "type": "string",
                "description": "One paragraph: what the part is, its dimensions, known limits.",
            }
        },
        ["summary"],
    ),
]


@dataclass
class AgentResult:
    script: str | None
    trace: list[dict[str, Any]]
    usage: dict[str, Any]
    rounds_used: int
    finished: bool
    summary: str | None
    build: RunResult | None
    stopped_reason: str
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return bool(self.script) and (self.build.ok if self.build else False)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "finished": self.finished,
            "stopped_reason": self.stopped_reason,
            "rounds": self.rounds_used,
            "elapsed_s": round(self.elapsed_s, 2),
            "usage": self.usage,
            "summary": self.summary,
            "steps": len(self.trace),
        }
        if self.build:
            data["build"] = {
                key: value
                for key, value in self.build.feedback().items()
                if key in {"ok", "checks", "error", "timed_out"}
            }
        return data


@dataclass
class _State:
    script: str | None = None
    build: RunResult | None = None
    runs: int = 0
    finished: bool = False
    summary: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


class ModelingAgent:
    """Drive a chat model through write → run → revise until it calls ``finish``."""

    def __init__(
        self,
        modeler: ChatModeler,
        workdir: str | Path,
        *,
        max_rounds: int = 4,
        max_runs: int = 3,
        cad_timeout_s: float = DEFAULT_TIMEOUT_S,
        python: str | None = None,
    ) -> None:
        self.modeler = modeler
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.script_path = self.workdir / "model.py"
        self.max_rounds = max_rounds
        self.max_runs = max_runs
        self.cad_timeout_s = cad_timeout_s
        self.python = python

    # -- tools ---------------------------------------------------------
    def _write_script(self, state: _State, code: str) -> dict[str, Any]:
        code = code.strip("\n") + "\n"
        issues = check_script(code)
        errors = [issue.as_dict() for issue in issues if issue.severity == "error"]
        if errors:
            return {
                "ok": False,
                "errors": errors,
                "hint": "Fix these and call write_script again; nothing was executed.",
            }
        self.script_path.write_text(code, encoding="utf-8")
        state.script = code
        warnings = [issue.as_dict() for issue in issues if issue.severity == "warning"]
        result: dict[str, Any] = {
            "ok": True,
            "chars": len(code),
            "hint": "Now call run_cad to execute it.",
        }
        if warnings:
            result["warnings"] = warnings
        return result

    def _run_cad(self, state: _State) -> dict[str, Any]:
        if not state.script:
            return {"ok": False, "error": "No script yet. Call write_script first."}
        if state.runs >= self.max_runs:
            return {
                "ok": False,
                "error": f"Execution budget exhausted ({self.max_runs} runs).",
                "hint": "Call finish with the best version you have and say what still fails.",
            }
        state.runs += 1
        run = run_script(
            self.script_path,
            workdir=self.workdir,
            timeout_s=self.cad_timeout_s,
            python=self.python,
        )
        state.build = run
        payload = run.feedback()
        payload["artifacts"] = sorted(run.artifacts)
        payload["runs_left"] = max(0, self.max_runs - state.runs)
        if run.ok:
            payload["hint"] = "Check the dimensions against the request, then call finish."
        return payload

    def _finish(self, state: _State, summary: str) -> dict[str, Any]:
        state.finished = True
        state.summary = summary.strip() or None
        return {"ok": True, "note": "Session closed."}

    def _dispatch(self, state: _State, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "write_script":
            return self._write_script(state, str(args.get("code") or ""))
        if name == "run_cad":
            return self._run_cad(state)
        if name == "finish":
            return self._finish(state, str(args.get("summary") or ""))
        return {"ok": False, "error": f"Unknown tool {name!r}."}

    # -- loop ----------------------------------------------------------
    def run(self, prompt: str, *, image: Path | None = None) -> AgentResult:
        prompt = (prompt or "").strip()
        if not prompt:
            raise InvalidArgument("prompt must not be empty")

        started = time.monotonic()
        state = _State()
        usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AGENT_PROMPT},
            {"role": "user", "content": self._user_content(prompt, image)},
        ]
        rounds_used = 0
        stopped_reason = "max_rounds"

        for round_index in range(1, self.max_rounds + 1):
            rounds_used = round_index
            reply = self.modeler.chat(
                messages,
                tools=TOOL_SPECS,
                tool_choice="required" if round_index == 1 else "auto",
            )
            self._add_usage(usage, reply)
            messages.append(reply.message)
            if reply.content.strip():
                state.trace.append(
                    {
                        "round": round_index,
                        "assistant": _clip(reply.content, MAX_ASSISTANT_CHARS),
                    }
                )

            if not reply.tool_calls:
                stopped_reason = "no_tool_calls"
                if not state.script and reply.content.strip():
                    salvaged = extract_code(reply.content)
                    if "build123d" in salvaged:
                        self._write_script(state, salvaged)
                break

            stop = False
            for call in reply.tool_calls:
                name, args = self._parse_call(call, state.trace, round_index)
                tool_started = time.monotonic()
                result = self._call_tool(state, name, args)
                state.trace.append(
                    {
                        "round": round_index,
                        "tool": name,
                        "arguments": self._trace_args(name, args),
                        "result": _clip_json(result),
                        "duration_s": round(time.monotonic() - tool_started, 2),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{round_index}",
                        "name": name,
                        "content": _clip(
                            json.dumps(result, ensure_ascii=False), MAX_TOOL_RESULT_CHARS
                        ),
                    }
                )
                if state.finished:
                    stop = True
                    break
            if stop:
                stopped_reason = "finish"
                break

        if state.finished:
            stopped_reason = "finish"

        return AgentResult(
            script=state.script,
            trace=state.trace,
            usage=usage,
            rounds_used=rounds_used,
            finished=state.finished,
            summary=state.summary,
            build=state.build,
            stopped_reason=stopped_reason,
            elapsed_s=time.monotonic() - started,
        )

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _user_content(prompt: str, image: Path | None) -> str | list[dict[str, Any]]:
        if image is None:
            return prompt
        from .modelers import _image_part

        return [{"type": "text", "text": prompt}, _image_part(image)]

    def _call_tool(self, state: _State, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._dispatch(state, name, args)
        except XPrintError as exc:
            return {"ok": False, "error": exc.to_dict()}

    @staticmethod
    def _parse_call(
        call: dict[str, Any],
        trace: list[dict[str, Any]],
        round_index: int,
    ) -> tuple[str, dict[str, Any]]:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw = function.get("arguments")
        if isinstance(raw, dict):
            return name, raw
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            trace.append(
                {
                    "round": round_index,
                    "tool": name,
                    "arguments": {"invalid_json": str(raw)[:500]},
                }
            )
            return name, {}
        return name, parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _trace_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "write_script":
            code = str(args.get("code") or "")
            return {"chars": len(code), "sha256": _digest(code)}
        return {key: _clip(str(value), 200) for key, value in args.items()}

    @staticmethod
    def _add_usage(usage: dict[str, Any], reply: ChatReply) -> None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = reply.usage.get(key) if isinstance(reply.usage, dict) else None
            if isinstance(value, int):
                usage[key] += value
        usage["rounds"] = usage.get("rounds", 0) + 1


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip_json(data: Any) -> Any:
    try:
        encoded = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)[:500]
    if len(encoded) <= MAX_TOOL_RESULT_CHARS:
        return data
    return {"truncated": encoded[: MAX_TOOL_RESULT_CHARS - 20] + "…"}


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_agent(
    modeler: ChatModeler,
    prompt: str,
    *,
    workdir: str | Path,
    image: Path | None = None,
    max_rounds: int = 4,
    cad_timeout_s: float = DEFAULT_TIMEOUT_S,
    python: str | None = None,
) -> AgentResult:
    agent = ModelingAgent(
        modeler,
        workdir,
        max_rounds=max_rounds,
        cad_timeout_s=cad_timeout_s,
        python=python,
    )
    return agent.run(prompt, image=image)


__all__ = ["TOOL_SPECS", "AgentResult", "ModelingAgent", "run_agent"]
