"""Python API: configure() + ScriptRun / ContainerRun.

    import gcejob
    gcejob.configure(project="p", region="us-west1", staging_bucket="gs://b")

    run = gcejob.ScriptRun("finetune", script="train.py", requirements=["torch"])
    run.start(machine="g2-standard-4", args=["--epochs", "5"], output_dir="gs://b/runs/1")

    run = gcejob.ContainerRun("eval", image="us-docker.pkg.dev/p/r/img:1")
    run.start(machine="g2-standard-4")

Everything is a thin layer over `JobSpec` / `Job`; one VM per run.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gcejob.backends import Backend
from gcejob.job import Job, JobError
from gcejob.spec import _GPU_FAMILIES, JobSpec
from gcejob.states import JobInfo, JobState

log = logging.getLogger("gcejob")

DEFAULT_MACHINE = "e2-standard-4"
DEFAULT_MAX_RUN = "24h"  # a forgotten GPU VM is costly, so every run is capped


# ---- global config -----------------------------------------------------------------------
@dataclass
class _Config:
    project: str | None = None
    region: str = "us-west1"
    staging_bucket: str | None = None
    service_account: str | None = None
    network: str | None = None
    subnetwork: str | None = None
    no_external_ip: bool = False
    zones: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


_config = _Config()


def configure(*, project: str | None = None, region: str | None = None,
              staging_bucket: str | None = None, service_account: str | None = None,
              network: str | None = None, subnetwork: str | None = None,
              no_external_ip: bool | None = None, zones: list[str] | None = None,
              labels: dict[str, str] | None = None) -> None:
    """Set defaults used by every run in this process. Only the arguments you pass change."""
    updates = {
        "project": project, "region": region, "staging_bucket": staging_bucket,
        "service_account": service_account, "network": network, "subnetwork": subnetwork,
        "no_external_ip": no_external_ip, "zones": zones, "labels": labels,
    }
    for key, value in updates.items():
        if value is not None:
            setattr(_config, key, value)


def _project(explicit: str | None) -> str:
    p = explicit or _config.project or os.environ.get("GCEJOB_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not p:
        raise ValueError("no project: call gcejob.configure(project=...) or set GCEJOB_PROJECT")
    return p


# ---- helpers -----------------------------------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not s:
        s = "job"
    if not s[0].isalpha():
        s = "job-" + s
    return s[:42].rstrip("-")


def _accelerator(machine: str, accelerator: str | None, count: int) -> str | None:
    """GPU for n1-* machines (e.g. NVIDIA_TESLA_T4 -> nvidia-tesla-t4).

    G2/G4/A2/A3/A4 machine types already include their GPU, so nothing is attached for them.
    """
    if not accelerator:
        return None
    if machine.startswith(_GPU_FAMILIES):
        log.info("machine %s includes its GPU; accelerator=%s is informational", machine, accelerator)
        return None
    if machine.startswith("n1-"):
        if count < 1:
            raise ValueError("accelerator_count must be >= 1")
        return accelerator.lower().replace("_", "-")
    raise ValueError(
        f"machine {machine!r} can't take a GPU ({accelerator}); use an n1-* machine with an "
        "accelerator, or a GPU machine type (g2-*, g4-*, a2-*, a3-*)"
    )


def _staging_prefix(bucket: str | None) -> str | None:
    if not bucket:
        return None
    b = bucket if bucket.startswith("gs://") else f"gs://{bucket}"
    return b.rstrip("/") + "/gcejob/staging"


def _pip_setup(requirements: list[str] | None, on_host: bool) -> str | None:
    lines = []
    if on_host:
        # isolated env on the VM; falls back to system python if venv isn't available
        lines.append("if python3 -m venv /job/venv; then . /job/venv/bin/activate; fi")
    if requirements:
        py = "python3" if on_host else "python"
        lines.append(f"{py} -m pip install -q " + " ".join(shlex.quote(r) for r in requirements))
    return "\n".join(lines) or None


_OPTION_DEFAULTS: dict[str, Any] = {
    "machine": DEFAULT_MACHINE, "accelerator": None, "accelerator_count": 1, "disk_type": None,
    "disk_gb": 100, "output_dir": None, "env": None, "service_account": None, "network": None,
    "max_run": DEFAULT_MAX_RUN, "provisioning": "standard", "zones": None, "inputs": None, "buckets": None,
    "startup_timeout": "15m", "max_retries": None,
}


def _options(given: dict[str, Any]) -> dict[str, Any]:
    unknown = set(given) - set(_OPTION_DEFAULTS)
    if unknown:
        raise TypeError(f"unexpected options: {sorted(unknown)}")
    return {**_OPTION_DEFAULTS, **given}


# ---- base class --------------------------------------------------------------------------
class _Run:
    def __init__(self, name: str, *, project: str | None = None, region: str | None = None,
                 staging_bucket: str | None = None, labels: dict[str, str] | None = None,
                 backend: Backend | None = None):
        self.name = name
        self._project = _project(project)
        self._region = region or _config.region
        self._staging = _staging_prefix(staging_bucket or _config.staging_bucket)
        self._labels = {**_config.labels, **(labels or {})}
        self._backend = backend
        self._job: Job | None = None
        self._output_dir: str | None = None

    def _make_spec(self, options: dict[str, Any], **content: Any) -> JobSpec:
        o = _options(options)
        out = o["output_dir"].rstrip("/") if o["output_dir"] else None
        provisioning = o["provisioning"].lower().replace("-", "_")
        # Spot VMs can be taken back; by default re-run the task up to 3 times when that happens
        retries = o["max_retries"] if o["max_retries"] is not None else (3 if provisioning == "spot" else 0)
        self._output_dir = out
        return JobSpec(
            name=_slug(self.name),
            project=self._project,
            region=self._region,
            zones=o["zones"] if o["zones"] is not None else _config.zones,
            service_account=o["service_account"] or _config.service_account,
            machine=o["machine"],
            accelerator_type=_accelerator(o["machine"], o["accelerator"], o["accelerator_count"]),
            accelerator_count=o["accelerator_count"],
            provisioning=provisioning,
            disk_gb=o["disk_gb"],
            disk_type=o["disk_type"],
            network=o["network"] or _config.network,
            subnetwork=_config.subnetwork,
            no_external_ip=_config.no_external_ip,
            max_run=o["max_run"],
            startup_timeout=o["startup_timeout"],
            max_retries=retries,
            env=dict(o["env"] or {}),
            inputs=o["inputs"] or {},
            buckets=o["buckets"] or [],
            output=f"{out}/model" if out else None,
            checkpoints=f"{out}/checkpoints" if out else None,
            staging=self._staging,
            labels=self._labels,
            **content,
        )

    def _start(self, spec: JobSpec, wait: bool, show_logs: bool) -> None:
        self._job = Job(spec, backend=self._backend)
        self._job.submit()
        log.info("submitted %s", self.resource_name)
        if wait:
            self.wait(show_logs=show_logs)
        else:
            log.info("not waiting: schedule `gcejob cleanup --stuck --older-than 30m --yes` "
                     "so a job that never starts is cancelled")

    # -- lifecycle
    def wait(self, show_logs: bool = True) -> JobInfo:
        """Block until the run finishes. Raises JobError / StartupTimeout; prints logs either way."""
        job = self._require_job()
        try:
            info = job.wait(on_update=lambda i: log.info("%s: %s %s", job.job_id, i.state.value, i.reason))
        except JobError:
            if show_logs:
                self._print_logs()
            raise
        if show_logs:
            self._print_logs()
        return info

    def _print_logs(self) -> None:
        try:
            for line in self._require_job().logs():
                print(line)
        except Exception as err:  # noqa: BLE001 - logs are best-effort
            log.warning("could not read logs: %s", err)

    def cancel(self) -> None:
        self._require_job().cancel()

    def delete(self) -> None:
        self._require_job().delete()

    def logs(self, follow: bool = False):
        return self._require_job().logs(follow=follow)

    @property
    def state(self) -> JobState:
        return self._require_job().refresh().state

    @property
    def job_id(self) -> str:
        return self._require_job().job_id

    @property
    def resource_name(self) -> str:
        return f"projects/{self._project}/locations/{self._region}/jobs/{self.job_id}"

    @property
    def model_uri(self) -> str | None:
        """gs:// prefix where /job/output is saved (set when `output_dir` was given)."""
        return f"{self._output_dir}/model" if self._output_dir else None

    def _require_job(self) -> Job:
        if self._job is None:
            raise RuntimeError("run has not been started; call start() first")
        return self._job

    @classmethod
    def attach(cls, job_id: str, *, project: str | None = None, region: str | None = None,
               backend: Backend | None = None):
        """Handle for an existing job, by id or full resource name."""
        m = re.match(r"projects/([^/]+)/locations/([^/]+)/jobs/", job_id)
        if m:
            project, region = m.group(1), m.group(2)
        short_id = job_id.rsplit("/", 1)[-1]
        obj = cls.__new__(cls)
        obj.name = short_id
        obj._project = _project(project)
        obj._region = region or _config.region
        obj._staging, obj._labels, obj._backend, obj._output_dir = None, {}, backend, None
        obj._job = Job.attach(short_id, obj._project, obj._region, backend=backend)
        return obj


# ---- run types ---------------------------------------------------------------------------
class ScriptRun(_Run):
    """Run a local Python script.

    Without `image` the script runs directly on the VM in a fresh virtualenv, with `requirements`
    pip-installed first. With `image` it runs inside that container.
    """

    def __init__(self, name: str, script: str, image: str | None = None,
                 requirements: list[str] | None = None, source_dir: str | None = None, **kw: Any):
        super().__init__(name, **kw)
        path = Path(script).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"script not found: {script}")
        self.script = path
        self.source_dir = Path(source_dir).resolve() if source_dir else path.parent
        if self.source_dir not in path.parents:
            raise ValueError("script must be inside source_dir")
        self.image = image
        self.requirements = requirements

    def start(self, *, args: list[str] | None = None, wait: bool = True, show_logs: bool = True,
              **options: Any) -> None:
        """Submit the run. See API.md for `options` (machine, output_dir, inputs, max_run, ...)."""
        py = "python" if self.image else "python3"
        rel = self.script.relative_to(self.source_dir).as_posix()
        spec = self._make_spec(
            options,
            code=str(self.source_dir),
            image=self.image,
            setup=_pip_setup(self.requirements, on_host=not self.image),
            command=f"{py} {shlex.quote(rel)}",
            args=list(args or []),
        )
        self._start(spec, wait, show_logs)


class ContainerRun(_Run):
    """Run a container image. `command` replaces the image ENTRYPOINT; `args` are appended."""

    def __init__(self, name: str, image: str, command: list[str] | None = None, **kw: Any):
        super().__init__(name, **kw)
        self.image = image
        self.command = command

    def start(self, *, args: list[str] | None = None, wait: bool = True, show_logs: bool = True,
              **options: Any) -> None:
        """Submit the run. See API.md for `options` (machine, output_dir, inputs, max_run, ...)."""
        spec = self._make_spec(
            options,
            image=self.image,
            command=" ".join(shlex.quote(c) for c in self.command) if self.command else None,
            args=list(args or []),
        )
        self._start(spec, wait, show_logs)
