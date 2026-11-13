"""Shared fixtures: every test gets a private workspace and a clean env."""

from __future__ import annotations

from pathlib import Path

import pytest

from xprint.config import Secrets
from xprint.workspace import Workspace

ENV_KEYS = (
    "XPRINT_WORKSPACE",
    "XPRINT_CONFIG",
    "XPRINT_HOST",
    "XPRINT_PORT",
    "XPRINT_TOKEN",
    "XPRINT_HUNYUAN_API_KEY",
    "XPRINT_TRIPO_API_KEY",
    "HUNYUAN3D_API_KEY",
    "TRIPO_API_KEY",
    "MOONSHOT_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point XPrint at a throwaway workspace and drop ambient credentials."""

    workspace = tmp_path / "ws"
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XPRINT_WORKSPACE", str(workspace))
    return workspace


@pytest.fixture
def workspace(clean_env: Path) -> Workspace:
    return Workspace(clean_env)


@pytest.fixture
def secrets() -> Secrets:
    return Secrets({})
