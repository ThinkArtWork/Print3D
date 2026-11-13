"""Asset store: versioned model scripts and their provenance.

Layout (inside the workspace)::

    assets/<asset_id>/asset.json          # manifest, one per asset
    assets/<asset_id>/v001/model.py       # the CAD script
    assets/<asset_id>/v001/params.json    # prompt, model, checks, usage

Revisions are append-only: ``v001``, ``v002`` … so a prompt tweak never
destroys the version that was printed.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import InvalidArgument, NotFound
from .jobs import sha256_file
from .workspace import Workspace, atomic_write_text

ASSET_ID_RE = re.compile(r"^a_[A-Za-z0-9_]{6,64}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
MANIFEST = "asset.json"
SCRIPT = "model.py"
PARAMS = "params.json"


def new_asset_id(now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"a_{moment}_{secrets.token_hex(3)}"


def validate_asset_id(asset_id: str) -> str:
    if not isinstance(asset_id, str) or not ASSET_ID_RE.match(asset_id):
        raise InvalidArgument(
            f"Invalid asset id: {asset_id!r}",
            hint="Asset ids look like a_20260101T000000Z_ab12cd.",
        )
    return asset_id


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise InvalidArgument(
            f"Invalid asset name: {name!r}",
            hint="Names are 2-41 chars of lowercase letters, digits, '-' or '_'.",
        )
    return name


def _iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AssetStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    # -- paths -----------------------------------------------------------
    def asset_dir(self, asset_id: str) -> Path:
        return self.workspace.asset_dir(validate_asset_id(asset_id))

    def manifest_path(self, asset_id: str) -> Path:
        return self.asset_dir(asset_id) / MANIFEST

    def revision_dir(self, asset_id: str, revision: str) -> Path:
        if not re.match(r"^v\d{3,}$", revision):
            raise InvalidArgument(
                f"Invalid revision: {revision!r}", hint="Revisions look like v001."
            )
        return self.asset_dir(asset_id) / revision

    # -- lifecycle -------------------------------------------------------
    def create(self, name: str) -> dict[str, Any]:
        name = validate_name(name)
        asset_id = new_asset_id()
        directory = self.asset_dir(asset_id)
        directory.mkdir(parents=True, exist_ok=False)
        manifest = {
            "asset_id": asset_id,
            "name": name,
            "created_at": _iso(),
            "updated_at": _iso(),
            "revisions": [],
        }
        self._write_manifest(asset_id, manifest)
        return manifest

    def load(self, asset_id: str) -> dict[str, Any]:
        path = self.manifest_path(asset_id)
        if not path.is_file():
            raise NotFound(
                f"Unknown asset: {asset_id}",
                hint="Run `xprint asset list` to see available assets.",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def next_revision(self, manifest: dict[str, Any]) -> str:
        count = len(manifest.get("revisions", []))
        return f"v{count + 1:03d}"

    def add_revision(
        self,
        asset_id: str,
        script: str,
        *,
        prompt: str,
        model: str,
        modeler: str,
        usage: dict[str, Any] | None = None,
        checks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write the script + provenance and return the new revision record."""

        manifest = self.load(asset_id)
        revision = self.next_revision(manifest)
        directory = self.revision_dir(asset_id, revision)
        directory.mkdir(parents=True, exist_ok=False)

        script_path = directory / SCRIPT
        atomic_write_text(script_path, script, mode=0o644)
        digest = sha256_file(script_path)

        params = {
            "asset_id": asset_id,
            "revision": revision,
            "prompt": prompt,
            "model": model,
            "modeler": modeler,
            "created_at": _iso(),
            "script_sha256": digest,
            "usage": usage or {},
            "checks": checks or {},
        }
        atomic_write_text(
            directory / PARAMS,
            json.dumps(params, indent=2, ensure_ascii=False) + "\n",
            mode=0o644,
        )

        record = {
            "revision": revision,
            "created_at": params["created_at"],
            "model": model,
            "modeler": modeler,
            "script": f"{revision}/{SCRIPT}",
            "sha256": digest,
            "prompt": prompt,
            "usage": usage or {},
            "checks": checks or {},
        }
        manifest.setdefault("revisions", []).append(record)
        manifest["updated_at"] = record["created_at"]
        self._write_manifest(asset_id, manifest)
        return record

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.workspace.assets_dir.is_dir():
            return items
        for path in sorted(self.workspace.assets_dir.iterdir(), reverse=True):
            if not path.is_dir() or not (path / MANIFEST).is_file():
                continue
            try:
                manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(
                {
                    "asset_id": manifest.get("asset_id", path.name),
                    "name": manifest.get("name"),
                    "revisions": len(manifest.get("revisions", [])),
                    "updated_at": manifest.get("updated_at"),
                }
            )
            if len(items) >= limit:
                break
        return items

    def _write_manifest(self, asset_id: str, manifest: dict[str, Any]) -> None:
        atomic_write_text(
            self.manifest_path(asset_id),
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            mode=0o644,
        )


__all__ = [
    "ASSET_ID_RE",
    "AssetStore",
    "NAME_RE",
    "new_asset_id",
    "validate_asset_id",
    "validate_name",
]
