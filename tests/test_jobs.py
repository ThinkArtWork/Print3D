from __future__ import annotations

import json

import pytest

from xprint.errors import InvalidArgument, JobNotFound, JobStateError
from xprint.jobs import JobStore, new_job_id, sha256_file, validate_job_id


def test_job_id_shape() -> None:
    job_id = new_job_id()
    assert validate_job_id(job_id) == job_id
    assert job_id.startswith("j_")
    with pytest.raises(InvalidArgument):
        validate_job_id("../../etc/passwd")
    with pytest.raises(InvalidArgument):
        validate_job_id("")


def test_create_and_state_machine(workspace) -> None:
    store = JobStore(workspace)
    job = store.create(kind="generate_cloud", provider="hunyuan", params={"prompt": "cup"})
    assert job.state == "queued"
    job = store.transition(job.id, "running", message="submitted")
    assert job.state == "running"
    job = store.succeed(job.id, result={"artifact": "cup.stl"})
    assert job.state == "succeeded"
    assert job.result == {"artifact": "cup.stl"}
    assert job.duration_seconds is not None


def test_illegal_transition_rejected(workspace) -> None:
    store = JobStore(workspace)
    job = store.create(kind="generate_cloud")
    with pytest.raises(JobStateError):
        store.transition(job.id, "succeeded")


def test_terminal_states_are_frozen(workspace) -> None:
    store = JobStore(workspace)
    job = store.create(kind="generate_cloud")
    store.fail(job.id, {"code": "internal_error", "message": "boom"})
    with pytest.raises(JobStateError):
        store.transition(job.id, "running")


def test_cancel_terminal(workspace) -> None:
    store = JobStore(workspace)
    job = store.create(kind="generate_local")
    store.transition(job.id, "running")
    cancelled = store.cancel(job.id, reason="user")
    assert cancelled.state == "cancelled"
    assert cancelled.is_terminal


def test_missing_job(workspace) -> None:
    store = JobStore(workspace)
    with pytest.raises(JobNotFound):
        store.load("j_20260101T000000Z_deadbeef")
    with pytest.raises(InvalidArgument):
        store.load("nope")


def test_persistence(workspace) -> None:
    store = JobStore(workspace)
    job = store.create(kind="slice", params={"target": "u1"})
    again = JobStore(workspace).load(job.id)
    assert again.id == job.id
    assert again.kind == "slice"
    raw = json.loads((workspace.jobs_dir / job.id / "job.json").read_text())
    assert raw["job_id"] == job.id
    assert raw["state"] == "queued"


def test_progress_and_listing(workspace) -> None:
    store = JobStore(workspace)
    first = store.create(kind="generate_cloud")
    second = store.create(kind="slice")
    store.set_progress(first.id, 0.4, message="halfway")
    assert store.load(first.id).progress == 0.4
    with pytest.raises(InvalidArgument):
        store.set_progress(first.id, 1.5)
    assert {j.id for j in store.list()} == {first.id, second.id}
    assert [j.id for j in store.list(kind="slice")] == [second.id]
    assert store.counts()["queued"] == 2


def test_artifact_checksum(workspace) -> None:
    path = workspace.root / "cache" / "cup.stl"
    path.write_bytes(b"solid cup\nendsolid\n")
    digest = sha256_file(path)
    assert len(digest) == 64
    store = JobStore(workspace)
    job = store.create(kind="generate_cloud")
    store.add_artifact(job.id, path, kind="mesh")
    stored = store.load(job.id).artifacts[0]
    assert stored.sha256 == digest
    assert stored.bytes == path.stat().st_size
    assert stored.path == "cache/cup.stl"
