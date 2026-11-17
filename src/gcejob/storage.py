"""Upload local code to GCS so the job VM can unpack it."""

from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path

from gcejob.spec import split_gcs_uri

EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache"}
EXCLUDE_SUFFIXES = (".pyc",)
MAX_ARCHIVE_MB = 500


def make_code_archive(code_dir: str | Path, dest: str | Path) -> Path:
    """Create a .tar.gz of `code_dir` (without VCS/venv junk) at `dest`."""
    root = Path(code_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"code directory not found: {root}")

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(p in EXCLUDE_DIRS for p in parts) or info.name.endswith(EXCLUDE_SUFFIXES):
            return None
        return info

    with tarfile.open(dest, "w:gz") as tar:
        tar.add(root, arcname=".", filter=_filter)
    return Path(dest)


def upload_code(code_dir: str | Path, staging_uri: str, job_id: str, project: str | None = None) -> str:
    """Archive and upload code; returns the gs:// URI of the archive."""
    from google.cloud import storage

    bucket_name, prefix = split_gcs_uri(staging_uri)
    object_name = "/".join(p for p in (prefix, f"{job_id}-code.tar.gz") if p)
    with tempfile.TemporaryDirectory() as tmp:
        archive = make_code_archive(code_dir, os.path.join(tmp, "code.tar.gz"))
        size_mb = archive.stat().st_size / 1e6
        if size_mb > MAX_ARCHIVE_MB:
            raise ValueError(
                f"code archive is {size_mb:.0f} MB (limit {MAX_ARCHIVE_MB} MB); "
                f"point source_dir/code at a smaller folder or put big files in GCS inputs"
            )
        from gcejob.auth import credentials

        client = storage.Client(project=project, credentials=credentials(project) if project else None)
        client.bucket(bucket_name).blob(object_name).upload_from_filename(str(archive))
    return f"gs://{bucket_name}/{object_name}"
