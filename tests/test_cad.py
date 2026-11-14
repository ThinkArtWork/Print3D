"""Tests for the CAD subprocess executor (``print3d.cad``)."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

from print3d.cad import clean_env, run_script
from print3d.errors import InvalidArgument, NotFound

MODEL = "from build123d import *\n\ndef build_part():\n    return Box(1, 1, 1)\n"


def _write(path: Path, code: str) -> Path:
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


def _write_interpreter(path: Path, code: str) -> Path:
    """Write an executable stand-in for a Python interpreter."""
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(code), encoding="utf-8")
    path.chmod(0o755)
    return path


def test_missing_script_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFound):
        run_script(tmp_path / "nope.py")


def test_non_positive_timeout_rejected(tmp_path: Path) -> None:
    script = _write(tmp_path / "model.py", MODEL)
    with pytest.raises(InvalidArgument):
        run_script(script, timeout_s=0)


def test_clean_env_drops_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_PASSWORD", "hunter2")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = clean_env()
    assert "MOONSHOT_API_KEY" not in env
    assert "MY_PASSWORD" not in env
    assert "PYTHONPATH" not in env
    # The worker gets an allowlist, so an arbitrary variable never reaches it —
    # including one with a harmless name, which a denylist would have passed on.
    assert "KEEP_ME" not in env
    assert env["PRINT3D_SANDBOX"] == "1"


def test_result_is_read_back(tmp_path: Path) -> None:
    script = _write(tmp_path / "model.py", MODEL)
    fake = _write_interpreter(
        tmp_path / "fake_ok.py",
        """
        import json, pathlib, sys
        outdir = pathlib.Path(sys.argv[5])
        (outdir / "model.stl").write_text("solid")
        payload = {
            "ok": True,
            "checks": {"bbox_mm": [20.0, 20.0, 20.0], "watertight": True},
            "artifacts": {"stl": str(outdir / "model.stl")},
            "error": None,
        }
        (outdir / "result.json").write_text(json.dumps(payload))
        print("exported")
        """,
    )
    result = run_script(script, workdir=tmp_path / "out", python=str(fake))
    assert result.ok is True
    assert result.exit_code == 0
    assert result.checks["bbox_mm"] == [20.0, 20.0, 20.0]
    assert Path(result.artifacts["stl"]).is_file()
    assert "exported" in result.stdout


def test_timeout_kills_the_child(tmp_path: Path) -> None:
    script = _write(tmp_path / "model.py", MODEL)
    fake = _write_interpreter(
        tmp_path / "fake_slow.py",
        """
        import time
        time.sleep(30)
        """,
    )
    result = run_script(script, workdir=tmp_path / "out", python=str(fake), timeout_s=0.5)
    assert result.ok is False
    assert result.timed_out is True
    assert result.duration_s < 5
    assert "budget" in (result.error or "")


def test_missing_result_json_is_reported(tmp_path: Path) -> None:
    script = _write(tmp_path / "model.py", MODEL)
    fake = _write_interpreter(tmp_path / "fake_silent.py", "import sys\nsys.exit(3)\n")
    result = run_script(script, workdir=tmp_path / "out", python=str(fake), timeout_s=10)
    assert result.ok is False
    assert result.exit_code == 3
    assert "without a result" in (result.error or "")
    assert "build123d" in (result.hint or "")


@pytest.mark.skipif(importlib.util.find_spec("build123d") is None, reason="build123d not installed")
def test_real_cube_is_watertight(tmp_path: Path) -> None:
    script = _write(
        tmp_path / "model.py",
        """
        from build123d import *

        PARAMS = {"size": 20.0}

        def build_part():
            return Box(PARAMS["size"], PARAMS["size"], PARAMS["size"])
        """,
    )
    result = run_script(script, workdir=tmp_path / "out", timeout_s=120)
    assert result.ok is True, result.error
    assert result.checks["watertight"] is True
    checks = result.checks
    assert checks["bbox_mm"] == pytest.approx([20.0, 20.0, 20.0], abs=1e-3)
    assert checks["volume_mm3"] == pytest.approx(8000.0, rel=1e-3)
    assert Path(result.artifacts["stl"]).is_file()


@pytest.mark.skipif(importlib.util.find_spec("build123d") is None, reason="build123d not installed")
def test_broken_script_reports_the_exception(tmp_path: Path) -> None:
    script = _write(
        tmp_path / "model.py",
        """
        from build123d import *

        def build_part():
            return Box(0, 10, 10)
        """,
    )
    result = run_script(script, workdir=tmp_path / "out", timeout_s=120)
    assert result.ok is False
    assert result.error


def test_missing_step_input_is_rejected(tmp_path: Path) -> None:
    script = _write(tmp_path / "model.py", MODEL)
    with pytest.raises(NotFound):
        run_script(script, workdir=tmp_path / "out", input_step=tmp_path / "nope.step")


@pytest.mark.skipif(importlib.util.find_spec("build123d") is None, reason="build123d not installed")
def test_step_input_is_exposed_as_input_step(tmp_path: Path) -> None:
    from build123d import Box, export_step

    source = tmp_path / "block.step"
    export_step(Box(20, 10, 6), str(source))
    script = _write(
        tmp_path / "model.py",
        """
        from build123d import *

        def build_part():
            return import_step(INPUT_STEP)
        """,
    )
    result = run_script(script, workdir=tmp_path / "out", input_step=source, timeout_s=120)
    assert result.ok is True, result.error
    assert result.checks["bbox_mm"] == pytest.approx([20.0, 10.0, 6.0], abs=1e-3)
    assert result.checks["volume_mm3"] == pytest.approx(1200.0, rel=1e-3)
