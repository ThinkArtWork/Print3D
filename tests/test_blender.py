from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.stl_helpers import binary_stl_bytes

from xprint.blender import find_blender, run_blender
from xprint.blender_policy import check_blender_script
from xprint.errors import InvalidArgument

CUBE_SCRIPT = """\
import bpy
bpy.ops.mesh.primitive_cube_add(size=20)
bpy.context.object.location.z = 10
"""


def _fake_blender(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-blender"
    script.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _write_tetra(outdir: str) -> None:
    Path(outdir, "model.stl").write_bytes(binary_stl_bytes())


# --- policy -----------------------------------------------------------------


def test_policy_accepts_a_plain_bpy_scene() -> None:
    issues = check_blender_script(CUBE_SCRIPT)
    assert [issue for issue in issues if issue.severity == "error"] == []


def test_policy_requires_bpy() -> None:
    issues = check_blender_script("print('hello')\n")
    assert any("bpy" in issue.message for issue in issues if issue.severity == "error")


def test_policy_bans_imports_and_calls() -> None:
    issues = check_blender_script("import bpy\nimport subprocess\nsubprocess.run(['ls'])\n")
    messages = " ".join(issue.message for issue in issues if issue.severity == "error")
    assert "subprocess" in messages


def test_policy_warns_when_no_mesh_is_created() -> None:
    issues = check_blender_script("import bpy\nprint(bpy.app.version)\n")
    assert any(issue.severity == "warning" for issue in issues)


# --- runner -----------------------------------------------------------------


def test_explicit_missing_blender_raises(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        find_blender(str(tmp_path / "nope"))


def test_run_with_fake_blender_exports_and_checks(tmp_path: Path) -> None:
    fake = _fake_blender(
        tmp_path,
        "import os\n"
        "import sys\n"
        f"sys.path.insert(0, {str(Path.cwd() / 'tests')!r})\n"
        "from stl_helpers import binary_stl_bytes\n"
        "from pathlib import Path\n"
        "Path(os.environ['XPRINT_BLENDER_OUT'], 'model.stl').write_bytes(binary_stl_bytes())\n"
        "print('exported')\n",
    )
    result = run_blender(CUBE_SCRIPT, workdir=tmp_path / "run", blender_path=str(fake))
    assert result.ok
    assert result.checks["triangles"] == 4
    assert result.checks["watertight"] is True
    assert "model.stl" in result.artifacts


def test_fake_blender_failure_is_reported(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
    result = run_blender(CUBE_SCRIPT, workdir=tmp_path / "run", blender_path=str(fake))
    assert not result.ok
    assert result.exit_code == 3
    assert "code 3" in (result.error or "")


def test_fake_blender_without_stl_is_reported(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, "print('nothing here')\n")
    result = run_blender(CUBE_SCRIPT, workdir=tmp_path / "run", blender_path=str(fake))
    assert not result.ok
    assert "without exporting" in (result.error or "")


def test_timeout_kills_fake_blender(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, "import time\ntime.sleep(30)\n")
    result = run_blender(
        CUBE_SCRIPT, workdir=tmp_path / "run", blender_path=str(fake), timeout_s=0.5
    )
    assert not result.ok
    assert result.timed_out
    assert "budget" in (result.error or "")


@pytest.mark.skipif(find_blender() is None, reason="Blender is not installed")
def test_real_blender_builds_a_watertight_cube(tmp_path: Path) -> None:
    result = run_blender(CUBE_SCRIPT, workdir=tmp_path / "real", timeout_s=120)
    assert result.ok, result.to_dict()
    assert result.checks["watertight"] is True
    assert result.checks["bbox_mm"][0] == pytest.approx(20.0, abs=0.01)
    assert result.checks["volume_mm3"] == pytest.approx(8000.0, rel=1e-3)
