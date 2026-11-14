"""Render a built STEP file as a 2D drawing sheet, in a child process.

Same security posture as :mod:`print3d.cad`: the worker is a separate interpreter
with a scrubbed environment, a CPU/wall-clock budget and an output cap, and only
its JSON is read back.  The input is always a STEP file a build already stored in
the asset revision, so the sheet provably matches the model it documents.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cad import clean_env, kill_group, read_result, tail
from .errors import InvalidArgument, NotFound

WORKER = Path(__file__).with_name("drawing_worker.py")
DEFAULT_TIMEOUT_S = 120.0
MAX_OUTPUT_CHARS = 4000
DEFAULT_VIEWS = ("front", "top", "right", "iso")
KNOWN_VIEWS = ("front", "top", "right", "iso")
MIN_SCALE = 0.05
MAX_SCALE = 20.0


@dataclass(slots=True)
class DrawingResult:
    """Outcome of one drawing render."""

    ok: bool
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "checks": self.checks,
            "files": self.files,
            "warnings": self.warnings,
            "error": self.error,
            "timed_out": self.timed_out,
            "hint": self.hint,
        }

    def feedback(self, *, max_output_chars: int = 2000) -> dict[str, Any]:
        """Compact, model-facing summary."""

        data: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "checks": self.checks,
        }
        if self.files:
            data["files"] = self.files
        if self.error:
            data["error"] = self.error
        if self.timed_out:
            data["timed_out"] = True
        if self.warnings:
            data["warnings"] = self.warnings
        if self.stdout.strip():
            data["stdout_tail"] = tail(self.stdout, max_output_chars)
        if self.stderr.strip():
            data["stderr_tail"] = tail(self.stderr, max_output_chars)
        if not self.ok:
            data["hint"] = self.hint or (
                "Simplify the model or request fewer views (views='front,top') and retry."
            )
        return data


def parse_views(views: str | None) -> list[str]:
    """Normalise a comma-separated view list, keeping the sheet order."""

    if not views or not views.strip():
        return list(DEFAULT_VIEWS)
    wanted = {item.strip().lower() for item in views.split(",") if item.strip()}
    unknown = sorted(wanted - set(KNOWN_VIEWS))
    if unknown or not wanted:
        raise InvalidArgument(
            f"unknown view(s): {', '.join(unknown) or '(none)'}",
            details={"views": views, "known": list(KNOWN_VIEWS)},
            hint="Pick views from: front, top, right, iso.",
        )
    return [name for name in KNOWN_VIEWS if name in wanted]


def run_drawing(
    step: str | os.PathLike[str],
    *,
    workdir: str | os.PathLike[str] | None = None,
    views: str | list[str] | None = None,
    scale: float = 1.0,
    hidden: bool = True,
    labels: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    python: str | None = None,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> DrawingResult:
    """Project ``step`` into ``drawing.svg`` + ``drawing.dxf`` under ``workdir``."""

    step_path = Path(step).expanduser().resolve()
    if not step_path.is_file():
        raise NotFound(
            f"STEP file not found: {step}",
            hint="Build the revision first; drawings are rendered from model.step.",
        )
    if not MIN_SCALE <= scale <= MAX_SCALE:
        raise InvalidArgument(
            f"scale must be between {MIN_SCALE} and {MAX_SCALE}",
            details={"scale": scale},
        )
    if timeout_s <= 0:
        raise InvalidArgument("timeout_s must be positive")
    view_names = parse_views(views)

    outdir = Path(workdir).expanduser().resolve() if workdir else step_path.parent
    outdir.mkdir(parents=True, exist_ok=True)
    result_path = outdir / "result.json"
    result_path.unlink(missing_ok=True)

    interpreter = python or sys.executable
    command = [
        interpreter,
        "-I",
        "-B",
        str(WORKER),
        str(step_path),
        str(outdir),
        "--views",
        ",".join(view_names),
        "--scale",
        f"{scale:g}",
        "--cpu-seconds",
        str(int(timeout_s) + 5),
    ]
    if not hidden:
        command.append("--no-hidden")
    if not labels:
        command.append("--no-labels")

    started = time.monotonic()
    timed_out = False
    stdout = stderr = ""
    try:
        process = subprocess.Popen(
            command,
            cwd=str(outdir),
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return DrawingResult(
            ok=False,
            error=f"could not start the drawing interpreter {interpreter!r}: {exc}",
            hint="Set config.cad.python to a Python with build123d installed.",
            duration_s=time.monotonic() - started,
        )

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(process)
        stdout, stderr = process.communicate()
    duration = time.monotonic() - started

    stdout = tail(stdout or "", max_output_chars)
    stderr = tail(stderr or "", max_output_chars)
    payload = read_result(result_path)

    if timed_out:
        return DrawingResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=duration,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            error=f"drawing exceeded the {timeout_s:.0f}s budget",
            hint=(
                "Hidden-line removal on a dense STEP is the usual culprit: retry with "
                "hidden=False or fewer views."
            ),
        )

    if payload is None:
        return DrawingResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=duration,
            stdout=stdout,
            stderr=stderr,
            error=(
                "the drawing interpreter exited without a result "
                f"(code {process.returncode}); is build123d installed?"
            ),
            hint=(
                "Install build123d into the CAD interpreter, or set config.cad.python "
                "to a venv that has it."
            ),
        )

    return DrawingResult(
        ok=bool(payload.get("ok")),
        exit_code=process.returncode,
        duration_s=duration,
        stdout=stdout,
        stderr=stderr,
        checks=dict(payload.get("checks") or {}),
        files={k: str(v) for k, v in (payload.get("files") or {}).items()},
        warnings=[str(item) for item in payload.get("warnings") or []],
        error=payload.get("error"),
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_VIEWS",
    "KNOWN_VIEWS",
    "DrawingResult",
    "parse_views",
    "run_drawing",
]
