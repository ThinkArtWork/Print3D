from __future__ import annotations

import time

from xprint.errors import XPrintError
from xprint.jobs import JobStore
from xprint.runner import JobRunner


def test_runner_completes_and_stores_result(workspace) -> None:
    store = JobStore(workspace)
    runner = JobRunner(store, workers=1)
    try:
        job = runner.submit("test", lambda job_id: {"value": 42, "job": job_id})
        done = runner.wait(job.id, timeout_s=5)
        assert done is not None
        assert done.state == "succeeded"
        assert done.result["value"] == 42
        assert done.result["job"] == job.id
        assert done.started_at and done.finished_at
    finally:
        runner.shutdown()


def test_runner_records_errors(workspace) -> None:
    store = JobStore(workspace)
    runner = JobRunner(store, workers=1)

    def explode(job_id: str) -> dict:
        raise XPrintError("provider exploded", hint="try another provider")

    try:
        job = runner.submit("test", explode)
        done = runner.wait(job.id, timeout_s=5)
        assert done is not None
        assert done.state == "failed"
        assert done.error["code"] == "internal_error"
        assert "provider exploded" in done.error["message"]
    finally:
        runner.shutdown(wait=False)


def test_runner_handles_many_jobs(workspace) -> None:
    store = JobStore(workspace)
    runner = JobRunner(store, workers=2)
    try:
        jobs = [runner.submit("test", lambda job_id: {"n": 1}) for _ in range(4)]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            states = [store.load(job.id).state for job in jobs]
            if all(state == "succeeded" for state in states):
                break
            time.sleep(0.02)
        assert all(store.load(job.id).state == "succeeded" for job in jobs)
        assert store.counts()["succeeded"] == 4
    finally:
        runner.shutdown()
