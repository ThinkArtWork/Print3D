"""Tests for the closed modeling loop (:mod:`xprint.agent`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xprint.agent import TOOL_SPECS, ModelingAgent
from xprint.cad import RunResult
from xprint.errors import InvalidArgument
from xprint.modelers import ChatReply

GOOD = """\
from build123d import *

PARAMS = {"size": 20.0}

def build_part():
    return Box(PARAMS["size"], PARAMS["size"], PARAMS["size"])

if __name__ == "__main__":
    export_stl(build_part(), "model.stl")
"""


def reply(*tool_calls: tuple[str, dict], content: str | None = None) -> ChatReply:
    message: dict = {"role": "assistant", "content": content or ""}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for index, (name, args) in enumerate(tool_calls)
        ]
    return ChatReply(
        message=message,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class ScriptedModeler:
    def __init__(self, replies: list[ChatReply]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, *, tools=None, tool_choice=None):
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "tool_choice": tool_choice,
                "tools": tools,
            }
        )
        return self.replies.pop(0) if self.replies else reply(("finish", {"summary": "done"}))


def fake_run(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True, error: str | None = None):
    def _run(script, *, workdir=None, timeout_s=0, python=None):
        return RunResult(
            ok=ok,
            exit_code=0 if ok else 1,
            checks={"watertight": ok, "bbox_mm": [20.0, 20.0, 20.0]},
            artifacts={"stl": str(Path(workdir) / "model.stl")} if ok else {},
            error=error,
        )

    monkeypatch.setattr("xprint.agent.run_script", _run)


def test_happy_path_writes_runs_and_finishes(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    modeler = ScriptedModeler(
        [
            reply(("write_script", {"code": GOOD})),
            reply(("run_cad", {})),
            reply(("finish", {"summary": "20 mm cube, watertight"})),
        ]
    )
    agent = ModelingAgent(modeler, tmp_path / "work", max_rounds=4)
    result = agent.run("a 20 mm cube")

    assert result.script is not None and "build_part" in result.script
    assert result.finished is True
    assert result.stopped_reason == "finish"
    assert result.rounds_used == 3
    assert result.usage == {
        "prompt_tokens": 30,
        "completion_tokens": 15,
        "total_tokens": 45,
        "rounds": 3,
    }
    assert result.build is not None and result.build.ok is True
    assert [step.get("tool") for step in result.trace if step.get("tool")] == [
        "write_script",
        "run_cad",
        "finish",
    ]
    assert (tmp_path / "work" / "model.py").is_file()
    # First turn is forced to call a tool, later turns may stop.
    assert modeler.calls[0]["tool_choice"] == "required"
    assert modeler.calls[1]["tool_choice"] == "auto"
    assert modeler.calls[0]["tools"] == TOOL_SPECS


def test_policy_error_keeps_the_script_out(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    bad = "import subprocess\n" + GOOD
    modeler = ScriptedModeler(
        [
            reply(("write_script", {"code": bad})),
            reply(("finish", {"summary": "could not do it"})),
        ]
    )
    result = ModelingAgent(modeler, tmp_path / "work").run("cube")
    assert result.script is None
    assert result.finished is True
    rejected = [step for step in result.trace if step.get("tool") == "write_script"][0]
    assert rejected["result"]["ok"] is False
    assert any("subprocess" in error["message"] for error in rejected["result"]["errors"])


def test_run_cad_before_write_reports_an_error(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    modeler = ScriptedModeler(
        [
            reply(("run_cad", {})),
            reply(("write_script", {"code": GOOD})),
            reply(("finish", {"summary": "ok"})),
        ]
    )
    result = ModelingAgent(modeler, tmp_path / "work").run("cube")
    first = [step for step in result.trace if step.get("tool") == "run_cad"][0]
    assert first["result"]["ok"] is False
    assert "write_script" in first["result"]["error"]


def test_execution_budget_is_enforced(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    modeler = ScriptedModeler(
        [
            reply(("write_script", {"code": GOOD})),
            reply(("run_cad", {})),
            reply(("run_cad", {})),
            reply(("finish", {"summary": "done"})),
        ]
    )
    result = ModelingAgent(modeler, tmp_path / "work", max_rounds=4, max_runs=1).run("cube")
    second = [step for step in result.trace if step.get("tool") == "run_cad"][-1]
    assert second["result"]["ok"] is False
    assert "budget" in second["result"]["error"]


def test_round_limit_stops_the_loop(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    modeler = ScriptedModeler(
        [
            reply(("write_script", {"code": GOOD})),
            reply(("run_cad", {})),
            reply(("run_cad", {})),
        ]
    )
    result = ModelingAgent(modeler, tmp_path / "work", max_rounds=2).run("cube")
    assert result.rounds_used == 2
    assert result.finished is False
    assert result.stopped_reason == "max_rounds"
    assert result.script is not None


def test_fenced_reply_is_salvaged_when_no_tool_is_called(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    modeler = ScriptedModeler([reply(content=f"here you go:\n```python\n{GOOD}```")])
    result = ModelingAgent(modeler, tmp_path / "work").run("cube")
    assert result.script is not None
    assert "build_part" in result.script
    assert result.stopped_reason == "no_tool_calls"


def test_invalid_tool_arguments_do_not_crash(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    broken = ChatReply(
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "write_script", "arguments": "{not json"},
                }
            ],
        }
    )
    modeler = ScriptedModeler([broken, reply(("finish", {"summary": "gave up"}))])
    result = ModelingAgent(modeler, tmp_path / "work").run("cube")
    assert result.script is None
    assert result.finished is True


def test_empty_prompt_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        ModelingAgent(ScriptedModeler([]), tmp_path / "work").run("   ")


def test_image_is_sent_as_a_data_url(tmp_path: Path, monkeypatch) -> None:
    fake_run(monkeypatch)
    image = tmp_path / "ref.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    modeler = ScriptedModeler([reply(("finish", {"summary": "done"}))])
    ModelingAgent(modeler, tmp_path / "work").run("make this", image=image)
    content = modeler.calls[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
