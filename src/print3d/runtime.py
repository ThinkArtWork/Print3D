"""Process-wide runtime state and the unified tool error envelope."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .config import (
    LoadedConfig,
    Secrets,
    load_all,
)
from .errors import (
    Print3DError,
    error_payload,
)
from .jobs import Job, JobStore
from .registry import Registry, builtin_registry
from .runner import JobRunner
from .workspace import Workspace


@dataclass
class Runtime:
    """Everything a tool handler needs, resolved once per process."""

    workspace: Workspace
    loaded: LoadedConfig
    secrets: Secrets
    store: JobStore
    registry: Registry
    writes: list[str] = field(default_factory=list)
    runner: JobRunner | None = None

    @classmethod
    def create(
        cls,
        *,
        workspace: Workspace | None = None,
        config_path: str | os.PathLike[str] | None = None,
        registry: Registry | None = None,
    ) -> Runtime:
        ws = workspace or Workspace()
        loaded, secrets = load_all(path=config_path, workspace=ws.root)
        store = JobStore(ws)
        return cls(
            workspace=ws,
            loaded=loaded,
            secrets=secrets,
            store=store,
            registry=registry or builtin_registry(),
            runner=JobRunner(store),
        )

    def record_write(self, what: str) -> None:
        self.writes.append(what)


def tool_error(exc: BaseException) -> dict[str, Any]:
    """Convert an exception into the unified error envelope."""

    if isinstance(exc, Print3DError):
        return {"ok": False, "writes_performed": False, "error": error_payload(exc)}
    return {
        "ok": False,
        "writes_performed": False,
        "error": error_payload(
            Print3DError(str(exc) or exc.__class__.__name__, code="internal_error")
        ),
    }


def job_payload(job: Job) -> dict[str, Any]:
    data = job.to_dict()
    # Keep the contract stable: durations are computed, not stored.
    data["duration_seconds"] = job.duration_seconds
    return data
