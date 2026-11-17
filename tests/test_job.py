from datetime import datetime, timedelta, timezone

import pytest

from gcejob.backends.base import Backend
from gcejob.cleanup import select_stale
from gcejob.job import Job, JobError, StartupTimeout
from gcejob.spec import JobSpec
from gcejob.states import FailureKind, JobInfo, JobState, classify_events


class FakeBackend(Backend):
    def __init__(self, states, events=None):
        self.states = list(states)
        self.events = events or []
        self.cancelled = False
        self.submitted = None

    def submit(self, spec, job_id, code_uri=None):
        self.submitted = (spec, job_id, code_uri)
        return JobInfo(job_id, JobState.QUEUED)

    def get(self, job_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        kind, reason = classify_events(self.events)
        return JobInfo(job_id, state, events=self.events, failure=kind, reason=reason)

    def cancel(self, job_id):
        self.cancelled = True

    def delete(self, job_id): ...
    def list(self, label_filter=None): return []
    def logs(self, job_id, follow=False): return iter(["line1", "line2"])


def make_job(states, events=None, **spec_kw):
    spec = JobSpec(name="t", project="p", command="true", startup_timeout="10m", **spec_kw)
    return Job(spec, backend=FakeBackend(states, events))


def test_job_id_is_unique_and_valid():
    j = make_job([JobState.QUEUED])
    assert j.job_id.startswith("t-") and len(j.job_id) <= 63


def test_wait_succeeds():
    j = make_job([JobState.QUEUED, JobState.STARTING, JobState.RUNNING, JobState.SUCCEEDED])
    seen = []
    info = j.submit().wait(poll=0, sleep=lambda s: None, on_update=lambda i: seen.append(i.state))
    assert info.state == JobState.SUCCEEDED
    assert seen == [JobState.QUEUED, JobState.STARTING, JobState.RUNNING, JobState.SUCCEEDED]


def test_wait_raises_on_failure():
    j = make_job([JobState.RUNNING, JobState.FAILED])
    with pytest.raises(JobError):
        j.submit().wait(poll=0, sleep=lambda s: None)


def test_startup_timeout_cancels_and_explains_capacity():
    ev = ["VM in Managed Instance Group meets error: CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED ... "
          "The zone does not have enough resources available"]
    j = make_job([JobState.STARTING], events=ev)
    t = iter(range(0, 10_000, 400))  # fake clock: +400s per call
    with pytest.raises(StartupTimeout) as err:
        j.submit().wait(poll=0, sleep=lambda s: None, clock=lambda: next(t))
    assert j.backend.cancelled
    assert "capacity" in str(err.value)


def test_timeout_does_not_apply_once_running():
    j = make_job([JobState.RUNNING, JobState.RUNNING, JobState.SUCCEEDED])
    t = iter(range(0, 100_000, 5000))
    assert j.submit().wait(poll=0, sleep=lambda s: None, clock=lambda: next(t)).state == JobState.SUCCEEDED
    assert not j.backend.cancelled


def test_code_is_uploaded_before_submit(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    calls = {}
    monkeypatch.setattr("gcejob.job.upload_code",
                        lambda d, s, jid, p: calls.setdefault("uri", f"gs://b/{jid}.tgz"))
    spec = JobSpec(name="t", project="p", command="true", code=str(tmp_path / "src"), staging="gs://b/s")
    j = Job(spec, backend=FakeBackend([JobState.QUEUED]))
    j.submit()
    assert j.backend.submitted[2] == calls["uri"]


def test_classify_events():
    assert classify_events(["... CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED ..."])[0] == FailureKind.CAPACITY
    assert classify_events(["Permission 'logging.logEntries.create' denied"])[0] == FailureKind.PERMISSION
    assert classify_events(["Job state is set from QUEUED to SCHEDULED"])[0] == FailureKind.NONE


def test_cleanup_selection():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    old = now - timedelta(days=3)
    new = now - timedelta(hours=1)
    jobs = [
        JobInfo("old-done", JobState.SUCCEEDED, created=old),
        JobInfo("old-stuck", JobState.STARTING, created=old),
        JobInfo("new-done", JobState.SUCCEEDED, created=new),
    ]
    assert [j.job_id for j in select_stale(jobs, timedelta(hours=24), now=now)] == ["old-done"]
    assert {j.job_id for j in select_stale(jobs, timedelta(hours=24), True, now)} == {"old-done", "old-stuck"}


def test_stuck_only_selects_unstarted_jobs_and_never_running_or_finished():
    from gcejob.cleanup import select_stale

    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    old = now - timedelta(hours=2)
    jobs = [
        JobInfo("queued", JobState.QUEUED, created=old),
        JobInfo("starting", JobState.STARTING, created=old),
        JobInfo("running", JobState.RUNNING, created=old),
        JobInfo("done", JobState.SUCCEEDED, created=old),
        JobInfo("fresh-starting", JobState.STARTING, created=now - timedelta(minutes=5)),
    ]
    got = {j.job_id for j in select_stale(jobs, timedelta(minutes=30), now=now, stuck_only=True)}
    assert got == {"queued", "starting"}


def test_cleanup_stuck_cancels_via_delete():
    from gcejob.cleanup import cleanup

    class B(FakeBackend):
        def __init__(self):
            super().__init__([JobState.QUEUED])
            self.deleted = []
        def list(self, label_filter=None):
            old = datetime.now(timezone.utc) - timedelta(hours=3)
            return [JobInfo("s", JobState.STARTING, created=old), JobInfo("r", JobState.RUNNING, created=old)]
        def delete(self, job_id):
            self.deleted.append(job_id)

    b = B()
    assert [j.job_id for j in cleanup(b, timedelta(minutes=30), dry_run=True, stuck_only=True)] == ["s"]
    assert b.deleted == []                                   # dry run deletes nothing
    cleanup(b, timedelta(minutes=30), dry_run=False, stuck_only=True)
    assert b.deleted == ["s"]                                # the running job is untouched


def test_other_batch_errors_are_surfaced_not_hidden():
    ev = ["Job gets no longer retryable information Batch Error: code - CODE_GCE_BAD_REQUEST, "
          "description - Disk cannot be smaller than the chosen image (30.0 GB)."]
    kind, reason = classify_events(ev)
    assert kind == FailureKind.OTHER and "Disk cannot be smaller" in reason


def test_wait_and_stream_prints_logs_live_and_returns_info():
    j = make_job([JobState.RUNNING, JobState.SUCCEEDED])
    lines = []
    info = j.submit().wait_and_stream(emit=lines.append, poll=0, sleep=lambda s: None)
    assert info.state == JobState.SUCCEEDED
    assert lines == ["line1", "line2"]


def test_wait_and_stream_still_shows_logs_when_job_fails():
    j = make_job([JobState.RUNNING, JobState.FAILED])
    lines = []
    with pytest.raises(JobError):
        j.submit().wait_and_stream(emit=lines.append, poll=0, sleep=lambda s: None)
    assert lines == ["line1", "line2"]


def test_wait_and_stream_falls_back_without_duplicates_when_follow_breaks():
    j = make_job([JobState.RUNNING, JobState.SUCCEEDED])

    def logs(job_id, follow=False):
        if follow:
            def broken():
                yield "line1"
                raise RuntimeError("logging api down")
            return broken()
        return iter(["line1", "line2", "line3"])

    j.backend.logs = logs
    lines = []
    j.submit().wait_and_stream(emit=lines.append, poll=0, sleep=lambda s: None)
    assert lines == ["line1", "line2", "line3"]
