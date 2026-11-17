"""Backend-neutral job states and info."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"  # VM being created / booting / installing drivers
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    DELETING = "DELETING"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.DELETING)


class FailureKind(str, enum.Enum):
    NONE = "none"
    CAPACITY = "capacity"  # zone has no free resources for the requested machine/GPU
    QUOTA = "quota"
    PERMISSION = "permission"
    TASK = "task"  # your command exited non-zero or timed out
    OTHER = "other"


@dataclass
class JobInfo:
    job_id: str
    state: JobState
    uid: str = ""
    events: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    created: datetime | None = None
    failure: FailureKind = FailureKind.NONE
    reason: str = ""

    @property
    def terminal(self) -> bool:
        return self.state.terminal


def classify_events(events: list[str]) -> tuple[FailureKind, str]:
    """Pull a human-meaningful cause out of Batch status events."""
    text = " | ".join(events)
    upper = text.upper()
    if "RESOURCE_POOL_EXHAUSTED" in upper or "DOES NOT HAVE ENOUGH RESOURCES" in upper:
        return FailureKind.CAPACITY, _snippet(text, "does not have enough resources") or (
            "zone has no free capacity for this machine/GPU"
        )
    if "QUOTA" in upper and "EXCEEDED" in upper:
        return FailureKind.QUOTA, _snippet(text, "quota")
    if "PERMISSION" in upper and "DENIED" in upper:
        return FailureKind.PERMISSION, _snippet(text, "permission")
    if "EXIT CODE" in upper or ("TASK" in upper and "FAILED" in upper):
        return FailureKind.TASK, _snippet(text, "exit code") or "task failed"
    if "BATCH ERROR" in upper:  # any other error Batch reports; show it instead of "see logs"
        return FailureKind.OTHER, _after(text, "Batch Error")
    return FailureKind.NONE, ""


def _after(text: str, marker: str, width: int = 400) -> str:
    i = text.find(marker)
    return text[i : i + width].strip() if i >= 0 else ""


def _snippet(text: str, needle: str, width: int = 220) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return ""
    start = max(0, i - 80)
    return text[start : start + width].strip()
