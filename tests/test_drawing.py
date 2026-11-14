"""Tests for the STEP to SVG/DXF drawing renderer (``print3d.drawing``)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from print3d.drawing import DEFAULT_VIEWS, parse_views, run_drawing
from print3d.errors import InvalidArgument, NotFound

needs_build123d = pytest.mark.skipif(
    importlib.util.find_spec("build123d") is None, reason="build123d not installed"
)


def test_parse_views_normalises_to_sheet_order() -> None:
    assert parse_views("") == list(DEFAULT_VIEWS)
    assert parse_views("right, front") == ["front", "right"]
    assert parse_views("top") == ["top"]


def test_parse_views_rejects_unknown_names() -> None:
    with pytest.raises(InvalidArgument) as excinfo:
        parse_views("front,back")
    assert excinfo.value.details["known"] == ["front", "top", "right", "iso"]


def test_missing_step_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFound):
        run_drawing(tmp_path / "nope.step")


def test_scale_out_of_range_is_rejected(tmp_path: Path) -> None:
    step = tmp_path / "block.step"
    step.write_text("ISO-10303-21;", encoding="utf-8")
    with pytest.raises(InvalidArgument):
        run_drawing(step, scale=0.0)


@needs_build123d
def test_real_sheet_projects_two_views(tmp_path: Path) -> None:
    from build123d import Box, export_step

    source = tmp_path / "block.step"
    export_step(Box(20, 10, 6), str(source))
    result = run_drawing(source, workdir=tmp_path / "out", views="front,top", timeout_s=120)
    assert result.ok is True, result.error
    checks = result.checks
    assert [view["name"] for view in checks["views"]] == ["front", "top"]
    front, top = checks["views"]
    assert front["width_mm"] == pytest.approx(20.0, abs=0.5)
    assert front["height_mm"] == pytest.approx(6.0, abs=0.5)
    assert top["width_mm"] == pytest.approx(20.0, abs=0.5)
    assert top["height_mm"] == pytest.approx(10.0, abs=0.5)
    assert checks["edges"]["visible"] > 0
    assert checks["sheet_mm"][0] == pytest.approx(20.0, abs=1.0)
    assert top["at_mm"][1] > front["at_mm"][1] + front["height_mm"]

    svg = Path(result.files["drawing.svg"])
    dxf = Path(result.files["drawing.dxf"])
    assert svg.is_file() and dxf.is_file()
    assert "viewBox" in svg.read_text(encoding="utf-8")


@needs_build123d
def test_scale_multiplies_the_projection(tmp_path: Path) -> None:
    from build123d import Box, export_step

    source = tmp_path / "block.step"
    export_step(Box(20, 10, 6), str(source))
    result = run_drawing(source, workdir=tmp_path / "out", views="front", scale=2.0, timeout_s=120)
    assert result.ok is True, result.error
    front = result.checks["views"][0]
    assert front["width_mm"] == pytest.approx(40.0, abs=0.5)
    assert front["height_mm"] == pytest.approx(12.0, abs=0.5)
