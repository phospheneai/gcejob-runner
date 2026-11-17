"""Find and remove leftover gcejob jobs (the janitor)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gcejob.backends import Backend
from gcejob.states import JobInfo, JobState

NOT_STARTED = (JobState.QUEUED, JobState.STARTING)


def select_stale(jobs: list[JobInfo], older_than: timedelta, include_active: bool = False,
                 now: datetime | None = None, stuck_only: bool = False) -> list[JobInfo]:
    """Jobs older than `older_than`.

    Default: only finished jobs. `include_active`: also queued/running ones.
    `stuck_only`: only jobs that still haven't started (QUEUED/STARTING); never running or finished
    ones, so it is safe to run on a schedule.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for j in jobs:
        if j.created is None:
            continue
        created = j.created if j.created.tzinfo else j.created.replace(tzinfo=timezone.utc)
        if now - created < older_than:
            continue
        if stuck_only:
            if j.state in NOT_STARTED:
                out.append(j)
        elif j.terminal or include_active:
            out.append(j)
    return out


def cleanup(backend: Backend, older_than: timedelta, include_active: bool = False,
            dry_run: bool = True, stuck_only: bool = False) -> list[JobInfo]:
    stale = select_stale(backend.list(), older_than, include_active, stuck_only=stuck_only)
    if not dry_run:
        for j in stale:
            backend.delete(j.job_id)
    return stale
