"""Workspace layout and path sandbox.

``~/.xprint`` (or ``--workspace``) is the only directory XPrint is allowed to
read model inputs from or write artifacts to.  Every user-supplied path goes
through :meth:`Workspace.safe_path`, which resolves symlinks *before* checking
containment, so ``assets/link -> /etc`` cannot escape.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from .errors import InvalidArgument, NotFound, PathOutsideWorkspace

DEFAULT_WORKSPACE = "~/.xprint"

#: Directories created inside a workspace on first use.
SUBDIRS = ("assets", "references", "jobs", "profiles", "cache", "logs")


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` atomically, creating parents as needed."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically, creating parents as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


class Workspace:
    """Resolved workspace root plus the operations allowed inside it."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        raw = Path(
            str(root or os.environ.get("XPRINT_WORKSPACE") or DEFAULT_WORKSPACE)
        ).expanduser()
        self.root = raw.resolve()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRS:
            (self.root / name).mkdir(exist_ok=True)

    # -- named locations -------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def secrets_path(self) -> Path:
        return self.root / "secrets.json"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def references_dir(self) -> Path:
        return self.root / "references"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def audit_log(self) -> Path:
        return self.logs_dir / "audit.jsonl"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def asset_dir(self, asset_id: str) -> Path:
        return self.assets_dir / asset_id

    # -- sandbox ---------------------------------------------------------
    def safe_path(
        self,
        candidate: str | os.PathLike[str],
        *,
        must_exist: bool = False,
        create_parents: bool = False,
    ) -> Path:
        """Resolve ``candidate`` inside the workspace or raise.

        Relative paths resolve against the workspace root.  Symlinks are
        resolved first, so a link pointing outside the workspace is rejected.
        """
        text = str(candidate)
        if not text.strip() or "\x00" in text:
            raise InvalidArgument("Empty or illegal path", details={"path": text})

        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.root / path

        resolved = path.resolve()
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise PathOutsideWorkspace(
                f"Path escapes the workspace: {text}",
                details={"path": text, "resolved": str(resolved), "workspace": str(self.root)},
            )
        if must_exist and not resolved.exists():
            raise NotFound(
                f"Path does not exist: {text}",
                hint="Check the path, or create it with the matching tool first.",
                details={"path": text},
            )
        if create_parents:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def rel(self, path: str | os.PathLike[str]) -> str:
        """Return a workspace-relative POSIX path (absolute if outside)."""
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return str(resolved)

    def json_listing(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "config": self.rel(self.config_path),
            "secrets": self.rel(self.secrets_path),
        }
