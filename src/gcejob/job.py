"""High-level Job API: submit, wait, startup timeout, logs."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from gcejob.backends import Backend, BatchBackend
from gcejob.spec import JobSpec
from gcejob.states import FailureKind, JobInfo, JobState
from gcejob.storage import upload_code


class JobError(RuntimeError):
    def __init__(self, message: str, info: JobInfo | None = None):
        super().__init__(message)
        self.info = info


class StartupTimeout(JobError):
    """The job never reached RUNNING within `startup_timeout`."""


class Job:
    def __init__(self, spec: JobSpec, backend: Backend | None = None, unique: bool = True):
        self.spec = spec
        self.backend = backend or BatchBackend(spec.project, spec.region)
        self.job_id = self._make_id(spec.name, unique)
        self.info: JobInfo | None = None

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path, backend: Backend | None = None, **overrides) -> Job:
        return cls(JobSpec.from_yaml(path, **overrides), backend=backend)

    @classmethod
    def attach(cls, job_id: str, project: str, region: str, backend: Backend | None = None) -> Job:
        """Handle for an existing job (for status/logs/cancel from another process)."""
        spec = JobSpec(name="attached", project=project, region=region, command="true")
        job = cls(spec, backend=backend or BatchBackend(project, region))
        job.job_id = job_id
        return job

    @staticmethod
    def _make_id(name: str, unique: bool) -> str:
        if not unique:
            return name
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{name}-{stamp}"[:63]

    # ---- lifecycle -------------------------------------------------------
    def submit(self) -> Job:
        code_uri = None
        if self.spec.code:
            code_uri = upload_code(self.spec.code, self.spec.staging, self.job_id, self.spec.project)
        self.info = self.backend.submit(self.spec, self.job_id, code_uri)
        return self

    def refresh(self) -> JobInfo:
        self.info = self.backend.get(self.job_id)
        return self.info

    @property
    def state(self) -> JobState:
        return (self.info or self.refresh()).state

    def wait(
        self,
        poll: float = 10.0,
        on_update: Callable[[JobInfo], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> JobInfo:
        """Block until the job reaches a terminal state.

        Raises StartupTimeout (and cancels the job) if it is not RUNNING in `startup_timeout`.
        """
        started = clock()
        ever_running = False
        last_state = None
        while True:
            info = self.refresh()
            ever_running = ever_running or info.state == JobState.RUNNING
            if on_update and info.state != last_state:
                on_update(info)
            last_state = info.state
            if info.terminal:
                if info.state == JobState.FAILED:
                    raise JobError(f"job {self.job_id} failed: {info.reason or 'see logs'}", info)
                return info
            if not ever_running and clock() - started > self.spec.startup_timeout_seconds:
                self.cancel()
                hint = f" ({info.failure.value}: {info.reason})" if info.failure != FailureKind.NONE else ""
                raise StartupTimeout(
                    f"job {self.job_id} not RUNNING after {self.spec.startup_timeout}{hint}; cancelled", info
                )
            sleep(poll)

    def logs(self, follow: bool = False):
        return self.backend.logs(self.job_id, follow=follow)

    def cancel(self) -> None:
        self.backend.cancel(self.job_id)

    def delete(self) -> None:
        self.backend.delete(self.job_id)

    def run(self, **wait_kwargs) -> JobInfo:
        """submit() then wait()."""
        return self.submit().wait(**wait_kwargs)
