from __future__ import annotations

import json
import os

import pytest

from print3d.config import (
    Config,
    Secrets,
    default_config_dict,
    load_all,
    load_config,
    mask_secret,
    redact,
    write_config,
)
from print3d.errors import ConfigError


def test_defaults_when_missing(workspace) -> None:
    loaded = load_config()
    assert loaded.exists is False
    assert loaded.config.server.host == "127.0.0.1"
    assert loaded.config.server.port == 8765
    assert any("No config at" in w for w in loaded.warnings)


def test_config_file_roundtrip(workspace) -> None:
    payload = default_config_dict(str(workspace.root))
    payload["server"]["port"] = 9999
    workspace.config_path.write_text(json.dumps(payload))
    loaded = load_config()
    assert loaded.exists is True
    assert loaded.config.server.port == 9999


def test_env_overrides(workspace, monkeypatch) -> None:
    monkeypatch.setenv("PRINT3D_HOST", "0.0.0.0")
    monkeypatch.setenv("PRINT3D_PORT", "7777")
    loaded = load_config()
    assert loaded.config.server.host == "0.0.0.0"
    assert loaded.config.server.port == 7777


def test_env_workspace_overrides(tmp_path, monkeypatch) -> None:
    other = tmp_path / "other"
    monkeypatch.setenv("PRINT3D_WORKSPACE", str(other))
    loaded = load_config()
    assert loaded.config.workspace == str(other)
    assert other.is_dir()  # Workspace creation is a side effect of path resolution


def test_bom_tolerated(workspace) -> None:
    payload = json.dumps(default_config_dict(str(workspace.root)))
    workspace.config_path.write_bytes(b"\xef\xbb\xbf" + payload.encode())
    assert load_config().exists is True


def test_invalid_json(workspace) -> None:
    workspace.config_path.write_text("{not json")
    with pytest.raises(ConfigError) as info:
        load_config()
    assert "JSON" in str(info.value)


def test_invalid_schema(workspace) -> None:
    workspace.config_path.write_text(json.dumps({"server": {"port": "nope"}}))
    with pytest.raises(ConfigError) as info:
        load_config()
    assert "server.port" in str(info.value)


def test_unknown_target_and_printer() -> None:
    cfg = Config.model_validate(default_config_dict())
    with pytest.raises(ConfigError) as info:
        cfg.target("nope")
    assert "Configured targets" in str(info.value)
    with pytest.raises(ConfigError) as info:
        cfg.printer("nope")
    assert "Configured printers" in str(info.value)


def test_write_config_keeps_backup(workspace) -> None:
    loaded = load_config()
    loaded.config.server.port = 1111
    written = write_config(loaded)
    assert written == workspace.config_path
    assert json.loads(written.read_text())["server"]["port"] == 1111
    loaded.config.server.port = 2222
    write_config(loaded, force=True)
    assert written.with_suffix(".json.bak").read_text().find("1111") > 0
    assert loaded.config.server.port == 2222


def test_write_config_refuses_existing(workspace) -> None:
    write_config(load_config())
    with pytest.raises(ConfigError):
        write_config(load_config())


# -- secrets -----------------------------------------------------------------


def test_secrets_env_precedence(monkeypatch) -> None:
    secrets = Secrets({"providers": {"tripo": "from-file"}})
    assert secrets.provider_key("tripo") == "from-file"
    assert secrets.key_source("tripo") == "secrets"
    monkeypatch.setenv("PRINT3D_TRIPO_API_KEY", "from-env")
    assert secrets.provider_key("tripo") == "from-env"
    assert secrets.key_source("tripo") == "env"
    assert secrets.has_key("tripo") is True


def test_secrets_permissive_mode_warns(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text('{"providers": {"tripo": "k"}}')
    os.chmod(path, 0o644)
    secrets = Secrets.load(path)
    assert any("chmod 600" in w for w in secrets.warnings)


def test_secrets_section() -> None:
    secrets = Secrets({"server": {"token": "t0ken"}})
    assert secrets.section("server") == {"token": "t0ken"}
    assert secrets.section("missing") == {}


def test_mask_and_redact() -> None:
    assert mask_secret("short") == "***"
    assert mask_secret("abcdefghijkl") == "ab***kl"
    assert "hunter2" not in redact("key=hunter2", ["hunter2"])


def test_load_all_merges_warnings() -> None:
    loaded, secrets = load_all()
    assert isinstance(secrets, Secrets)
    assert loaded.warnings == secrets.warnings or any("No config" in w for w in loaded.warnings)
