"""Credentials for the Google clients."""

from __future__ import annotations

from typing import Any


def credentials(project: str) -> Any:
    """Application default credentials, billed to `project` for API quota.

    A user login (`gcloud auth application-default login`) has no quota project by default, so
    requests are counted against a small shared Google project and hit "quota exceeded". If the
    credentials have none, use the job's project.
    """
    import google.auth

    creds, _ = google.auth.default()
    if getattr(creds, "quota_project_id", None) is None and hasattr(creds, "with_quota_project"):
        creds = creds.with_quota_project(project)
    return creds
