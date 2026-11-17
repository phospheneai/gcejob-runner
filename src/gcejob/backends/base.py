"""Backend interface. Batch is the first implementation; other services can follow."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from gcejob.spec import JobSpec
from gcejob.states import JobInfo


class Backend(ABC):
    @abstractmethod
    def submit(self, spec: JobSpec, job_id: str, code_uri: str | None = None) -> JobInfo: ...

    @abstractmethod
    def get(self, job_id: str) -> JobInfo: ...

    @abstractmethod
    def cancel(self, job_id: str) -> None:
        """Ask the service to stop the job (VMs are removed by the service)."""

    @abstractmethod
    def delete(self, job_id: str) -> None:
        """Remove the job record (and any VM still attached to it)."""

    @abstractmethod
    def list(self, label_filter: dict[str, str] | None = None) -> list[JobInfo]: ...

    @abstractmethod
    def logs(self, job_id: str, follow: bool = False) -> Iterator[str]: ...
