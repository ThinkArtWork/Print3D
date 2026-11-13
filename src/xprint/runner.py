"""Background execution for long adapter calls (local generators, future cloud jobs)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .errors import XPrintError, error_payload
from .jobs import Job, JobStore


class JobRunner:
    """A small thread pool that owns the job state transitions."""

    def __init__(self, store: JobStore, *, workers: int = 2) -> None:
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="xprint-job")
        self._lock = threading.Lock()

    def submit(
        self,
        kind: str,
        fn: Callable[[str], dict[str, Any]],
        *,
        params: dict[str, Any] | None = None,
        provider: str | None = None,
    ) -> Job:
        job = self.store.create(kind, params=params, provider=provider)
        self._pool.submit(self._execute, job.id, fn)
        return job

    def _execute(self, job_id: str, fn: Callable[[str], dict[str, Any]]) -> None:
        try:
            self.store.transition(job_id, "running")
        except Exception:
            return
        try:
            result = fn(job_id) or {}
        except XPrintError as exc:
            self._fail(job_id, exc)
        except BaseException as exc:  # noqa: BLE001 - a crashing job must not kill the pool
            self._fail(
                job_id,
                XPrintError(
                    f"{type(exc).__name__}: {exc}",
                    hint="This is a bug in XPrint or in the provider command.",
                ),
            )
        else:
            with contextlib.suppress(Exception):
                self.store.succeed(job_id, result=dict(result))

    def _fail(self, job_id: str, exc: XPrintError) -> None:
        with contextlib.suppress(Exception):
            self.store.fail(job_id, error_payload(exc), message=exc.message)

    def wait(self, job_id: str, *, timeout_s: float = 60.0, interval: float = 0.05) -> Job | None:
        """Block until the job is terminal (tests and CLI convenience)."""

        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            job = self.store.load(job_id)
            if job.is_terminal:
                return job
            time.sleep(interval)
        return self.store.load(job_id)

    def shutdown(self, *, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)
