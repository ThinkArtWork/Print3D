from __future__ import annotations

import sys
from pathlib import Path

import pytest

from print3d.errors import ConfigError, InvalidArgument, NotFound
from print3d.local_mesh import build_command, run_local


def _provider(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "provider.py"
    script.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _entry(script: Path, *, args: list[str] | None = None) -> dict:
    return {"command": [sys.executable, str(script)], "args": args or [], "mode": "both"}


def test_build_command_substitutes_placeholders(tmp_path: Path) -> None:
    entry = {
        "command": ["/usr/bin/python3", "gen.py"],
        "args": ["--prompt", "{prompt}", "--out", "{outdir}", "--mode", "{mode}"],
    }
    argv = build_command(
        "triposr",
        entry,
        prompt="a 40 mm cube",
        image=None,
        outdir=tmp_path,
        name="cube",
        mode="text",
    )
    assert argv == [
        "/usr/bin/python3",
        "gen.py",
        "--prompt",
        "a 40 mm cube",
        "--out",
        str(tmp_path),
        "--mode",
        "text",
    ]


def test_build_command_accepts_string_form(tmp_path: Path) -> None:
    entry = {"command": "python3 gen.py", "args": "--prompt {prompt}"}
    argv = build_command("x", entry, prompt="p", image=None, outdir=tmp_path, name="n", mode="text")
    assert argv == ["python3", "gen.py", "--prompt", "p"]


def test_build_command_requires_command(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        build_command("x", {}, prompt="p", image=None, outdir=tmp_path, name="n", mode="text")


def test_run_local_produces_mesh(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path.cwd() / 'tests')!r})\n"
        "from stl_helpers import binary_stl_bytes\n"
        "Path(os.environ['PRINT3D_LOCAL_OUT'], 'mesh.stl').write_bytes(binary_stl_bytes())\n"
        "print('done')\n",
    )
    result = run_local(
        "fake",
        _entry(provider),
        prompt="a cube",
        image=None,
        outdir=tmp_path / "out",
        name="cube",
        mode="text",
        timeout_s=30,
    )
    assert result.ok, result.to_dict()
    assert result.mesh and Path(result.mesh).is_file()


def test_run_local_discovers_mesh_from_stdout(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path.cwd() / 'tests')!r})\n"
        "from stl_helpers import binary_stl_bytes\n"
        "out = Path(os.environ['PRINT3D_LOCAL_OUT'])\n"
        "mesh = out / 'result.stl'\n"
        "mesh.write_bytes(binary_stl_bytes())\n"
        "print(json.dumps({'mesh': str(mesh)}))\n",
    )
    result = run_local(
        "fake",
        _entry(provider),
        prompt="a cube",
        image=None,
        outdir=tmp_path / "out",
        name="cube",
        mode="text",
        timeout_s=30,
    )
    assert result.ok, result.to_dict()
    assert result.mesh and result.mesh.endswith("result.stl")


def test_run_local_reports_missing_mesh(tmp_path: Path) -> None:
    provider = _provider(tmp_path, "print('no stl')\n")
    result = run_local(
        "fake",
        _entry(provider),
        prompt="a cube",
        image=None,
        outdir=tmp_path / "out",
        name="cube",
        mode="text",
        timeout_s=30,
    )
    assert not result.ok
    assert "without producing an STL" in (result.error or "")


def test_run_local_timeout(tmp_path: Path) -> None:
    provider = _provider(tmp_path, "import time\ntime.sleep(30)\n")
    result = run_local(
        "fake",
        _entry(provider),
        prompt="a cube",
        image=None,
        outdir=tmp_path / "out",
        name="cube",
        mode="text",
        timeout_s=0.5,
    )
    assert not result.ok
    assert result.timed_out


def test_run_local_rejects_missing_image(tmp_path: Path) -> None:
    provider = _provider(tmp_path, "print('x')\n")
    with pytest.raises(NotFound):
        run_local(
            "fake",
            _entry(provider),
            prompt="",
            image=tmp_path / "missing.png",
            outdir=tmp_path / "out",
            name="cube",
            mode="image",
            timeout_s=5,
        )


def test_run_local_rejects_bad_timeout(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        run_local(
            "fake",
            {"command": [sys.executable]},
            prompt="p",
            image=None,
            outdir=tmp_path / "out",
            name="cube",
            mode="text",
            timeout_s=0,
        )
