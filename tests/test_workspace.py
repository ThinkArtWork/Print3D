from __future__ import annotations

import os
from pathlib import Path

import pytest

from print3d.errors import InvalidArgument, NotFound, PathOutsideWorkspace
from print3d.workspace import Workspace, atomic_write_text


def test_layout_created(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "root")
    assert ws.root.is_dir()
    for name in ("assets", "jobs", "profiles", "cache", "logs"):
        assert (ws.root / name).is_dir()


def test_safe_path_relative(workspace: Workspace) -> None:
    target = workspace.safe_path("assets/a/model.stl", create_parents=True)
    assert target == workspace.root / "assets/a/model.stl"
    assert target.parent.is_dir()


def test_safe_path_absolute_inside(workspace: Workspace) -> None:
    target = workspace.safe_path(workspace.root / "jobs" / "x.json")
    assert target == workspace.root / "jobs" / "x.json"


def test_safe_path_rejects_parent_escape(workspace: Workspace) -> None:
    with pytest.raises(PathOutsideWorkspace):
        workspace.safe_path("../outside.txt")


def test_safe_path_rejects_absolute_escape(workspace: Workspace) -> None:
    with pytest.raises(PathOutsideWorkspace):
        workspace.safe_path("/etc/passwd")


def test_safe_path_rejects_symlink_escape(workspace: Workspace, tmp_path: Path) -> None:
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    link = workspace.root / "cache" / "escape"
    os.symlink(secret_dir, link)
    with pytest.raises(PathOutsideWorkspace):
        workspace.safe_path(link / "file.txt")


def test_safe_path_rejects_nul(workspace: Workspace) -> None:
    with pytest.raises(InvalidArgument):
        workspace.safe_path("assets/a\x00b")


def test_safe_path_rejects_empty(workspace: Workspace) -> None:
    with pytest.raises(InvalidArgument):
        workspace.safe_path("   ")


def test_safe_path_must_exist(workspace: Workspace) -> None:
    with pytest.raises(NotFound):
        workspace.safe_path("assets/missing.stl", must_exist=True)


def test_rel_roundtrip(workspace: Workspace) -> None:
    target = workspace.safe_path("assets/x/y.stl")
    assert workspace.rel(target) == "assets/x/y.stl"


def test_atomic_write_mode_and_content(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "secret.txt"
    atomic_write_text(target, "s3cret\n")
    assert target.read_text() == "s3cret\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(target.parent.glob(".secret.txt.*"))
