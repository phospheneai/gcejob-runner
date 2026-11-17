from gcejob.auth import credentials


class Creds:
    def __init__(self, quota=None):
        self.quota_project_id = quota

    def with_quota_project(self, project):
        return Creds(project)


def test_uses_job_project_when_login_has_no_quota_project(monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (Creds(None), "x"))
    assert credentials("my-proj").quota_project_id == "my-proj"


def test_keeps_an_existing_quota_project(monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (Creds("already"), "x"))
    assert credentials("my-proj").quota_project_id == "already"
