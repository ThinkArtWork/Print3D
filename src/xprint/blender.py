"""Run Blender in background mode and turn a bpy script into a printable mesh.

The client model writes the script (text-to-3D); for image-to-3D it also gets
the picture as a normal MCP image block and may read the same file from
``sys.argv`` after ``--``.  XPrint only provides the execution + export +
geometry-check loop.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cad import clean_env
from .errors import InvalidArgument
from .mesh import stats_for_stl
from .workspace import atomic_write_text

DEFAULT_TIMEOUT_S = 300.0
MAX_OUTPUT_CHARS = 6000
BLENDER_CANDIDATES = (
    Path("/Applications/Blender.app/Contents/MacOS/Blender"),
    Path("/usr/local/bin/blender"),
    Path("/opt/homebrew/bin/blender"),
)

EXPORT_SCRIPT = """\
import bpy
import json
import os

out = os.environ["XPRINT_BLENDER_OUT"]
meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
for obj in meshes:
    obj.select_set(True)
if meshes:
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.wm.stl_export(
        filepath=os.path.join(out, "model.stl"),
        export_selected_objects=True,
        apply_modifiers=True,
    )
    bpy.ops.export_scene.gltf(
        filepath=os.path.join(out, "model.glb"),
        export_format="GLB",
        use_selection=True,
    )
print("XPRINT_BLENDER_EXPORT " + json.dumps({"objects": len(meshes)}))
"""


def find_blender(explicit: str | None = None) -> Path | None:
    """Return the Blender executable to use, or ``None`` when not installed."""

    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise InvalidArgument(
            f"Blender not found at {path}",
            hint="Set render.blender_path or pass blender_path= to blender_run.",
        )
    for candidate in BLENDER_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass(slots=True)
class BlenderResult:
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
            "engine": "blender",
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "timed_out": self.timed_out,
            "checks": self.checks,
            "artifacts": sorted(self.artifacts),
            "error": self.error,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "hint": self.hint,
        }


def _kill(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            process.kill()


def run_blender(
    script: str,
    *,
    workdir: str | Path,
    image: str | Path | None = None,
    blender_path: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> BlenderResult:
    """Execute ``script`` in ``blender -b``, then export and inspect the scene."""

    if timeout_s <= 0:
        raise InvalidArgument("timeout must be positive")
    blender = find_blender(blender_path)
    if blender is None:
        return BlenderResult(
            ok=False,
            error="Blender is not installed or not on the usual paths.",
            hint="Install Blender, then set render.blender_path in config.json.",
        )

    directory = Path(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    scene_path = directory / "scene.py"
    export_path = directory / "xprint_export.py"
    atomic_write_text(scene_path, script, mode=0o600)
    atomic_write_text(export_path, EXPORT_SCRIPT, mode=0o600)

    env = clean_env()
    env.update(
        {
            "XPRINT_BLENDER_OUT": str(directory),
            "BLENDER_USER_CONFIG": str(directory / ".blender-config"),
            "BLENDER_USER_SCRIPTS": str(directory / ".blender-scripts"),
            "BLENDER_USER_DATAFILES": str(directory / ".blender-data"),
            "TMPDIR": str(directory / "tmp"),
        }
    )
    (directory / "tmp").mkdir(exist_ok=True)

    extra: list[str] = []
    if image:
        image_path = Path(image).expanduser().resolve()
        if not image_path.is_file():
            raise InvalidArgument(
                f"Image not found: {image_path}",
                hint="Pass an existing PNG/JPEG/WebP path as image=.",
            )
        extra = [str(image_path)]

    command = [
        str(blender),
        "-b",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(scene_path),
        "--python",
        str(export_path),
    ]
    if extra:
        command.extend(["--", *extra])

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
        return BlenderResult(ok=False, error=f"Could not start Blender: {exc}")

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill(process)
        stdout, stderr = process.communicate()
        elapsed = time.monotonic() - started
        return BlenderResult(
            ok=False,
            exit_code=None,
            duration_s=elapsed,
            stdout=stdout[-max_output_chars:],
            stderr=stderr[-max_output_chars:],
            timed_out=True,
            error=f"Blender exceeded the {timeout_s:g}s budget",
            hint="Reduce subdivision/modifier counts, or raise render.timeout_s.",
        )

    elapsed = time.monotonic() - started
    stdout = (stdout or "")[-max_output_chars:]
    stderr = (stderr or "")[-max_output_chars:]
    stl_path = directory / "model.stl"
    glb_path = directory / "model.glb"

    if process.returncode != 0:
        return BlenderResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=elapsed,
            stdout=stdout,
            stderr=stderr,
            error=f"Blender exited with code {process.returncode}",
            hint="Read stderr_tail; the bpy script raised before exporting.",
        )
    if not stl_path.is_file() or stl_path.stat().st_size <= 84:
        return BlenderResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=elapsed,
            stdout=stdout,
            stderr=stderr,
            error="Blender finished without exporting a mesh",
            hint="The script must leave at least one MESH object in the scene.",
        )

    artifacts = {"model.stl": str(stl_path)}
    if glb_path.is_file() and glb_path.stat().st_size:
        artifacts["model.glb"] = str(glb_path)
    try:
        checks = stats_for_stl(stl_path)
    except (OSError, ValueError) as exc:
        return BlenderResult(
            ok=False,
            exit_code=process.returncode,
            duration_s=elapsed,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
            error=f"Exported STL could not be inspected: {exc}",
        )
    checks["engine"] = "blender"
    return BlenderResult(
        ok=True,
        exit_code=process.returncode,
        duration_s=elapsed,
        stdout=stdout,
        stderr=stderr,
        checks=checks,
        artifacts=artifacts,
        hint="Review the checks and the preview; fix the bpy script and run again if needed.",
    )
