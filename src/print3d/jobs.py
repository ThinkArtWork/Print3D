"""Job storage and the job state machine.

Layout: ``<workspace>/jobs/<job_id>/job.json`` (mode 0600, written atomically).

    queued ──▶ running ──▶ succeeded
       │           ├────▶ failed
       └───────────┴────▶ cancelled

Every long-running operation (cloud generation, slicing, rendering) is
asynchronous: submit returns a job id, hosts poll ``job_status``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import InvalidArgument, JobNotFound, JobStateError
from .workspace import Workspace, atomic_write_text

JOB_ID_RE = re.compile(r"^j_[A-Za-z0-9_]{4,64}$")
JOB_FILE = "job.json"


STATES: tuple[str, ...] = ("queued", "running", "succeeded", "failed", "cancelled")
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_job_id(now: datetime | None = None) -> str:
    """Time-sortable, path-safe job id: ``j_20261113T222000Z_1a2b3c4d``."""
    moment = now or utcnow()
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"j_{stamp}_{secrets.token_hex(4)}"


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise InvalidArgument(
            f"Invalid job id: {job_id!r}",
            hint="Job ids look like `j_20261113T222000Z_1a2b3c4d`.",
        )
    return job_id


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class Artifact:
    path: str
    kind: str | None = None
    bytes: int | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path}
        if self.kind:
            data["kind"] = self.kind
        if self.bytes is not None:
            data["bytes"] = self.bytes
        if self.sha256:
            data["sha256"] = self.sha256
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            path=str(data.get("path", "")),
            kind=data.get("kind"),
            bytes=data.get("bytes"),
            sha256=data.get("sha256"),
        )


@dataclass
class Job:
    id: str
    kind: str
    state: str = "queued"
    provider: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    progress: float | None = None
    message: str | None = None
    error: dict[str, Any] | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or utcnow()
        return round((end - self.started_at).total_seconds(), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "provider": self.provider,
            "params": self.params,
            "result": self.result,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        return cls(
            id=str(data["job_id"]),
            kind=str(data["kind"]),
            state=str(data.get("state", "queued")),
            provider=data.get("provider"),
            params=dict(data.get("params") or {}),
            result=dict(data.get("result") or {}),
            progress=data.get("progress"),
            message=data.get("message"),
            error=data.get("error"),
            artifacts=[Artifact.from_dict(item) for item in data.get("artifacts") or []],
            created_at=_parse_iso(data.get("created_at")) or utcnow(),
            updated_at=_parse_iso(data.get("updated_at")) or utcnow(),
            started_at=_parse_iso(data.get("started_at")),
            finished_at=_parse_iso(data.get("finished_at")),
        )


def _check_progress(progress: float | None) -> float | None:
    if progress is None:
        return None
    if not isinstance(progress, (int, float)) or isinstance(progress, bool):
        raise InvalidArgument(f"progress must be a number in [0, 1], got {progress!r}")
    if progress < 0 or progress > 1:
        raise InvalidArgument(f"progress must be in [0, 1], got {progress!r}")
    return float(progress)


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class JobStore:
    """Filesystem-backed job store rooted in one workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.jobs_dir
        self.root.mkdir(parents=True, exist_ok=True)

    # -- low level -------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        return self.root / validate_job_id(job_id)

    def job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / JOB_FILE

    def save(self, job: Job) -> Job:
        job.updated_at = utcnow()
        directory = self.job_dir(job.id)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            directory / JOB_FILE,
            json.dumps(job.to_dict(), indent=2, ensure_ascii=False) + "\n",
            mode=0o600,
        )
        return job

    def load(self, job_id: str) -> Job:
        path = self.job_path(job_id)
        if not path.is_file():
            raise JobNotFound(
                f"Job not found: {job_id}",
                hint="List jobs with `print3d jobs`, or check the id returned at submit time.",
                details={"job_id": job_id},
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobStateError(
                f"Corrupt job record: {path}",
                hint="Delete the job directory if it is not needed.",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        return Job.from_dict(data)

    def exists(self, job_id: str) -> bool:
        return self.job_path(job_id).is_file()

    def iter_jobs(self) -> Iterator[Job]:
        for entry in sorted(self.root.iterdir(), reverse=True):
            if entry.is_dir() and JOB_ID_RE.match(entry.name):
                try:
                    yield self.load(entry.name)
                except (JobNotFound, JobStateError):
                    continue

    # -- lifecycle -------------------------------------------------------
    def create(
        self,
        kind: str,
        *,
        params: dict[str, Any] | None = None,
        provider: str | None = None,
        state: str = "queued",
    ) -> Job:
        if state not in STATES:
            raise InvalidArgument(f"Unknown job state: {state}")
        job = Job(
            id=new_job_id(), kind=kind, state=state, params=dict(params or {}), provider=provider
        )
        if state == "running":
            job.started_at = utcnow()
        self.job_dir(job.id).mkdir(parents=True, exist_ok=True)
        return self.save(job)

    def transition(
        self,
        job_id: str,
        state: str,
        *,
        message: str | None = None,
        error: dict[str, Any] | None = None,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
    ) -> Job:
        if state not in STATES:
            raise InvalidArgument(f"Unknown job state: {state}")
        job = self.load(job_id)
        allowed = ALLOWED_TRANSITIONS[job.state]
        if state not in allowed:
            raise JobStateError(
                f"Cannot move job {job_id} from {job.state} to {state}",
                hint=(
                    f"Allowed from {job.state}: {', '.join(sorted(allowed))}"
                    if allowed
                    else f"{job.state} is terminal; create a new job instead."
                ),
                details={"job_id": job_id, "from": job.state, "to": state},
            )
        now = utcnow()
        job.state = state
        if state == "running" and job.started_at is None:
            job.started_at = now
        if state in TERMINAL_STATES:
            job.finished_at = now
        if state == "succeeded" and job.progress is None:
            job.progress = 1.0
        if message is not None:
            job.message = message
        if error is not None:
            job.error = error
        if progress is not None:
            job.progress = _check_progress(progress)
        if result is not None:
            job.result.update(result)
        return self.save(job)

    def succeed(
        self, job_id: str, *, message: str | None = None, result: dict[str, Any] | None = None
    ) -> Job:
        return self.transition(job_id, "succeeded", message=message, result=result, progress=1.0)

    def fail(self, job_id: str, error: dict[str, Any], *, message: str | None = None) -> Job:
        return self.transition(job_id, "failed", error=error, message=message)

    def cancel(self, job_id: str, *, reason: str | None = None) -> Job:
        return self.transition(job_id, "cancelled", message=reason or "cancelled by request")

    def set_progress(self, job_id: str, progress: float, *, message: str | None = None) -> Job:
        job = self.load(job_id)
        if job.is_terminal:
            raise JobStateError(
                f"Job {job_id} already finished ({job.state})",
                details={"job_id": job_id, "state": job.state},
            )
        job.progress = _check_progress(progress)
        if message is not None:
            job.message = message
        return self.save(job)

    def add_artifact(
        self,
        job_id: str,
        path: Path,
        *,
        kind: str | None = None,
        sha256: str | None = None,
        store_bytes: bool = True,
    ) -> Job:
        job = self.load(job_id)
        resolved = Path(path)
        artifact = Artifact(
            path=self.workspace.rel(resolved),
            kind=kind,
            bytes=resolved.stat().st_size if store_bytes and resolved.is_file() else None,
            sha256=sha256 or (sha256_file(resolved) if resolved.is_file() else None),
        )
        job.artifacts.append(artifact)
        return self.save(job)

    # -- queries ---------------------------------------------------------
    def list(
        self,
        *,
        limit: int = 50,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[Job]:
        items = list(self.iter_jobs())
        if kind:
            items = [job for job in items if job.kind == kind]
        if state:
            items = [job for job in items if job.state == state]
        items.sort(key=lambda job: job.created_at, reverse=True)
        return items[: max(0, limit)]

    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in STATES}
        for job in self.iter_jobs():
            counts[job.state] = counts.get(job.state, 0) + 1
        return counts
