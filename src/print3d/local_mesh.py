"""Adapter for user-installed local 3D generators.

Print3D does not bundle model weights.  A local provider is a command that
follows a small contract:

* it receives at least one of ``prompt`` / ``image`` (via the ``{prompt}`` and
  ``{image}`` placeholders in its ``args`` template),
* it writes a triangle mesh to ``<outdir>/mesh.stl`` (or prints a JSON object
  with a ``mesh`` path on stdout),
* it exits non-zero on failure.

Anything that can do that — TripoSR, Hunyuan3D, Shap-E, a ComfyUI workflow, a
private service wrapper — becomes a provider without changing Print3D.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cad import clean_env
from .errors import ConfigError, InvalidArgument, NotFound

DEFAULT_TIMEOUT_S = 600.0
MAX_OUTPUT_CHARS = 6000
_PLACEHOLDER_RE = re.compile(r"\{(prompt|image|outdir|name|mode)\}")


@dataclass(slots=True)
class LocalRunResult:
    ok: bool
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    mesh: str | None = None
    error: str | None = None
    timed_out: bool = False
    hint: str | None = None
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "timed_out": self.timed_out,
            "command": self.command,
            "error": self.error,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "hint": self.hint,
        }


def _as_argv(value: Any, *, field_name: str) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ConfigError(
        f"providers.local.{field_name} must be a string or a list of strings",
        hint='Example: "command": ["/opt/triposr/.venv/bin/python", "run.py"]',
    )


def build_command(
    provider_id: str,
    entry: dict[str, Any],
    *,
    prompt: str,
    image: str | None,
    outdir: Path,
    name: str,
    mode: str,
) -> list[str]:
    """Render the configured argv template for one generation call."""

    command = entry.get("command")
    if not command:
        raise ConfigError(
            f"Local provider {provider_id!r} has no command",
            hint='Configure providers.local.<id>.command, e.g. ["/path/to/python", "gen.py"].',
        )
    argv = _as_argv(command, field_name=f"{provider_id}.command")
    template = entry.get("args") or []
    argv += _as_argv(template, field_name=f"{provider_id}.args")

    mapping = {
        "{prompt}": prompt,
        "{image}": str(image or ""),
        "{outdir}": str(outdir),
        "{name}": name,
        "{mode}": mode,
    }
    rendered = []
    for token in argv:
        for key, value in mapping.items():
            token = token.replace(key, value)
        rendered.append(token)
    return rendered


def _parse_mesh_from_output(stdout: str, outdir: Path) -> str | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        candidate: str | None = None
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                mesh = payload.get("mesh") or payload.get("stl") or payload.get("path")
                if isinstance(mesh, str) and mesh:
                    candidate = mesh
        if candidate is None:
            matches = re.findall(r"[^\s'\"]+\.stl", line, flags=re.IGNORECASE)
            if matches:
                candidate = matches[-1]
        if candidate:
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                path = outdir / path
            if path.is_file():
                return str(path)
    return None


def _find_mesh(outdir: Path, stdout: str) -> str | None:
    direct = outdir / "mesh.stl"
    if direct.is_file() and direct.stat().st_size > 0:
        return str(direct)
    candidates = sorted(
        (path for path in outdir.glob("*.stl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return str(candidates[0])
    return _parse_mesh_from_output(stdout, outdir)


def run_local(
    provider_id: str,
    entry: dict[str, Any],
    *,
    prompt: str,
    image: str | Path | None,
    outdir: str | Path,
    name: str,
    mode: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> LocalRunResult:
    """Execute one local generator and return the mesh it produced."""

    if timeout_s <= 0:
        raise InvalidArgument("timeout must be positive")
    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)

    image_path: Path | None = None
    if image:
        image_path = Path(image).expanduser().resolve()
        if not image_path.is_file():
            raise NotFound(
                f"Image not found: {image_path}",
                hint="Import the picture first, then pass its path.",
            )

    command = build_command(
        provider_id,
        entry,
        prompt=prompt,
        image=str(image_path) if image_path else None,
        outdir=directory,
        name=name,
        mode=mode,
    )
    if not command:
        raise ConfigError(f"Local provider {provider_id!r} produced an empty command")

    env = clean_env()
    extra_env = entry.get("env") or {}
    if isinstance(extra_env, dict):
        env.update({str(key): str(value) for key, value in extra_env.items()})
    env["PRINT3D_LOCAL_OUT"] = str(directory)
    env["PRINT3D_LOCAL_PROVIDER"] = provider_id

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return LocalRunResult(
            ok=False,
            command=command,
            error=f"Could not start local provider {provider_id!r}: {exc}",
            hint="Check providers.local.<id>.command and that the file is executable.",
        )

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        stdout, stderr = process.communicate()
        elapsed = time.monotonic() - started
        return LocalRunResult(
            ok=False,
            exit_code=None,
            duration_s=elapsed,
            stdout=(stdout or "")[-MAX_OUTPUT_CHARS:],
            stderr=(stderr or "")[-MAX_OUTPUT_CHARS:],
            timed_out=True,
            command=command,
            error=f"Local provider {provider_id!r} exceeded the {timeout_s:g}s budget",
            hint="Raise providers.local.<id>.timeout_s or use a smaller model/step count.",
        )

    elapsed = time.monotonic() - started
    stdout = (stdout or "")[-MAX_OUTPUT_CHARS:]
    stderr = (stderr or "")[-MAX_OUTPUT_CHARS:]
    if process.returncode != 0:
        return LocalRunResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=elapsed,
            stdout=stdout,
            stderr=stderr,
            command=command,
            error=f"Local provider {provider_id!r} exited with code {process.returncode}",
            hint="Read stderr_tail; fix the provider command or its environment.",
        )

    mesh = _find_mesh(directory, stdout)
    if not mesh:
        return LocalRunResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=elapsed,
            stdout=stdout,
            stderr=stderr,
            command=command,
            error="The local provider exited without producing an STL",
            hint='Write <outdir>/mesh.stl (or print {"mesh": "/path/model.stl"}).',
        )

    return LocalRunResult(
        ok=True,
        exit_code=process.returncode,
        duration_s=elapsed,
        stdout=stdout,
        stderr=stderr,
        mesh=mesh,
        command=command,
        hint="Review the geometry checks, then preview or slice.",
    )
