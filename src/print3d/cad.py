"""Run model scripts as external processes with limits, and report geometry.

The security model from the design doc applies here: an LLM-written script is
still untrusted code.  It is never imported into the 3DPrint process; it runs in a
child interpreter with a scrubbed environment, a CPU/wall-clock budget and a
size cap, and only the JSON it produced is read back.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InvalidArgument, NotFound

WORKER = Path(__file__).with_name("cad_worker.py")
DEFAULT_TIMEOUT_S = 120.0
MAX_OUTPUT_CHARS = 4000

# Environment variables that must never reach generated code.
_SECRET_MARKERS = ("API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass(slots=True)
class RunResult:
    """Outcome of one ``model.py`` execution."""

    ok: bool
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
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
            "artifacts": self.artifacts,
            "error": self.error,
            "timed_out": self.timed_out,
            "hint": self.hint,
        }

    def feedback(self, *, max_output_chars: int = 2000) -> dict[str, Any]:
        """Compact, model-facing summary: the numbers matter, not the log."""

        data: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "checks": self.checks,
        }
        if self.error:
            data["error"] = self.error
        if self.timed_out:
            data["timed_out"] = True
        if self.stdout.strip():
            data["stdout_tail"] = tail(self.stdout, max_output_chars)
        if self.stderr.strip():
            data["stderr_tail"] = tail(self.stderr, max_output_chars)
        if not self.ok:
            data["hint"] = self.hint or (
                "Fix the script and call write_script again, then run_cad."
            )
        return data


def tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text.strip()
    return "…" + text[-limit:].strip()


def clean_env(extra_deny: tuple[str, ...] = ()) -> dict[str, str]:
    """Copy the environment minus anything that looks like a credential."""

    denied = set(extra_deny)
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in denied or any(marker in upper for marker in _SECRET_MARKERS):
            continue
        if key in {"PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME"}:
            continue
        env[key] = value
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "MPLBACKEND": "Agg",
            "PRINT3D_SANDBOX": "1",
        }
    )
    return env


def kill_group(process: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def read_result(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_script(
    script: str | os.PathLike[str],
    *,
    workdir: str | os.PathLike[str] | None = None,
    input_step: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    python: str | None = None,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> RunResult:
    """Execute ``script`` in a child interpreter and read back its geometry.

    ``python`` selects the CAD interpreter (defaults to the one running 3DPrint);
    set ``config.cad.python`` when ``build123d`` lives in another venv.
    ``input_step`` is an existing STEP file the script may import; the worker
    exposes it to the script as the string globals ``INPUT_STEP`` and
    ``INPUT_NAME`` (empty strings when no file was passed).
    """

    script_path = Path(script).expanduser().resolve()
    if not script_path.is_file():
        raise NotFound(
            f"model script not found: {script}",
            hint="Call write_script first, or pass an existing model.py path.",
        )
    if timeout_s <= 0:
        raise InvalidArgument("timeout_s must be positive")
    input_path = Path(input_step).expanduser().resolve() if input_step else None
    if input_path is not None and not input_path.is_file():
        raise NotFound(
            f"input STEP not found: {input_step}",
            hint="Copy the file into the workspace, or pass an absolute path.",
        )

    outdir = Path(workdir).expanduser().resolve() if workdir else script_path.parent
    outdir.mkdir(parents=True, exist_ok=True)
    result_path = outdir / "result.json"
    result_path.unlink(missing_ok=True)

    interpreter = python or sys.executable
    command = [
        interpreter,
        "-I",
        "-B",
        str(WORKER),
        str(script_path),
        str(outdir),
        "--cpu-seconds",
        str(int(timeout_s) + 5),
    ]
    if input_path is not None:
        command += ["--input", str(input_path)]

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
        return RunResult(
            ok=False,
            error=f"could not start the CAD interpreter {interpreter!r}: {exc}",
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
        return RunResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=duration,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            error=f"model script exceeded the {timeout_s:.0f}s budget",
            hint=(
                "Simplify the geometry or loosen config.cad.timeout_s; "
                "fillet/chamfer on dense edges is the usual culprit."
            ),
        )

    if payload is None:
        return RunResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=duration,
            stdout=stdout,
            stderr=stderr,
            error=(
                "the CAD interpreter exited without a result "
                f"(code {process.returncode}); is build123d installed?"
            ),
            hint=(
                "Install build123d into the CAD interpreter, or set config.cad.python "
                "to a venv that has it (`uv pip install build123d`)."
            ),
        )

    return RunResult(
        ok=bool(payload.get("ok")),
        exit_code=process.returncode,
        duration_s=duration,
        stdout=stdout,
        stderr=stderr,
        checks=dict(payload.get("checks") or {}),
        artifacts={k: str(v) for k, v in (payload.get("artifacts") or {}).items()},
        error=payload.get("error"),
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "RunResult",
    "clean_env",
    "kill_group",
    "read_result",
    "run_script",
    "tail",
]
