"""Shared fixtures: every test gets a private workspace and a clean env."""

from __future__ import annotations

from pathlib import Path

import pytest

from print3d.config import Secrets
from print3d.workspace import Workspace

ENV_KEYS = (
    "PRINT3D_WORKSPACE",
    "PRINT3D_CONFIG",
    "PRINT3D_HOST",
    "PRINT3D_PORT",
    "PRINT3D_TOKEN",
    "PRINT3D_HUNYUAN_API_KEY",
    "PRINT3D_TRIPO_API_KEY",
    "HUNYUAN3D_API_KEY",
    "TRIPO_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Print3D at a throwaway workspace and drop ambient credentials."""

    workspace = tmp_path / "ws"
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PRINT3D_WORKSPACE", str(workspace))
    return workspace


@pytest.fixture
def workspace(clean_env: Path) -> Workspace:
    return Workspace(clean_env)


@pytest.fixture
def secrets() -> Secrets:
    return Secrets({})
