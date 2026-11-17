"""Google Cloud Batch backend.

Batch creates the VM, runs the task, streams logs to Cloud Logging and removes the VM and its
boot disk when the job ends. This module translates a JobSpec into a Batch job and back.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Iterator
from typing import Any

from gcejob.backends.base import Backend
from gcejob.spec import JobSpec, split_gcs_uri
from gcejob.states import JobInfo, JobState, classify_events

LABEL_MARK = "gcejob"
MOUNT_ROOT = "/mnt/disks/gcejob"
SHEBANG = "#!/bin/bash"  # Batch runs scripts with sh unless told otherwise; we need bash
PREEMPTED_EXIT_CODE = 50001  # Batch reports a preempted Spot VM with this task exit code
# NOTE: mount options for the Cloud Storage FUSE volume; validated live in the smoke test.
GCS_MOUNT_OPTIONS = ["-o rw", "-implicit-dirs"]

_PROVISIONING = {"standard": "STANDARD", "spot": "SPOT", "flex_start": "FLEX_START"}
_STATE_MAP = {
    "QUEUED": JobState.QUEUED,
    "SCHEDULED": JobState.STARTING,
    "RUNNING": JobState.RUNNING,
    "SUCCEEDED": JobState.SUCCEEDED,
    "FAILED": JobState.FAILED,
    "CANCELLATION_IN_PROGRESS": JobState.CANCELLING,
    "CANCELLED": JobState.CANCELLED,
    "DELETION_IN_PROGRESS": JobState.DELETING,
}


def _bucket_mounts(spec: JobSpec, code_uri: str | None) -> dict[str, str]:
    """bucket name -> mount path (one FUSE mount per bucket)."""
    mounts: dict[str, str] = {}
    for bucket in spec.buckets:
        mounts.setdefault(bucket, f"{MOUNT_ROOT}/{bucket}")
    uris = list(spec.inputs.values())
    uris += [u for u in (spec.output, spec.checkpoints, code_uri) if u]
    for uri in uris:
        bucket, _ = split_gcs_uri(uri)
        mounts.setdefault(bucket, f"{MOUNT_ROOT}/{bucket}")
    return mounts


def build_prelude(spec: JobSpec, code_uri: str | None, mounts: dict[str, str]) -> str:
    """Shell that lays out /job/{input,output,checkpoints,code} on top of the bucket mounts."""
    # every mounted bucket is also reachable as /gcs/<bucket>
    lines = [SHEBANG, "set -euo pipefail", "mkdir -p /job/input", f"ln -sfn {MOUNT_ROOT} /gcs"]

    def target(uri: str) -> str:
        bucket, prefix = split_gcs_uri(uri)
        return f"{mounts[bucket]}/{prefix}".rstrip("/")

    for name, uri in spec.inputs.items():
        lines.append(f"ln -sfn {shlex.quote(target(uri))} {shlex.quote('/job/input/' + name)}")
    if spec.output:
        lines.append(f"mkdir -p {shlex.quote(target(spec.output))}")
        lines.append(f"ln -sfn {shlex.quote(target(spec.output))} /job/output")
    if spec.checkpoints:
        lines.append(f"mkdir -p {shlex.quote(target(spec.checkpoints))}")
        lines.append(f"ln -sfn {shlex.quote(target(spec.checkpoints))} /job/checkpoints")
    if code_uri:
        bucket, obj = split_gcs_uri(code_uri)
        lines.append("mkdir -p /job/code")
        lines.append(f"tar -xzf {shlex.quote(mounts[bucket] + '/' + obj)} -C /job/code")
        lines.append("cd /job/code")
    return "\n".join(lines)


def build_job_config(spec: JobSpec, job_id: str, code_uri: str | None = None) -> dict[str, Any]:
    """Pure function: JobSpec -> Batch Job as a JSON-style dict (camelCase)."""
    mounts = _bucket_mounts(spec, code_uri)

    env = {
        "GCEJOB_NAME": spec.name,
        "GCEJOB_JOB_ID": job_id,
        "GCEJOB_INPUT_DIR": "/job/input",
        "GCEJOB_OUTPUT_DIR": "/job/output",
        "GCEJOB_CHECKPOINT_DIR": "/job/checkpoints",
        **spec.env,
    }

    if spec.image:
        runnables: list[dict[str, Any]] = []
        if mounts:
            runnables.append({"script": {"text": build_prelude(spec, code_uri, mounts)}})
        container: dict[str, Any] = {
            "imageUri": spec.image,
            "volumes": (
                [f"{MOUNT_ROOT}:{MOUNT_ROOT}", f"{MOUNT_ROOT}:/gcs", "/job:/job"] if mounts else []
            ),
        }
        if spec.code or spec.setup:
            # run inside the image via bash so we can cd into the code and run setup first
            inner = ["set -e"]
            if spec.code:
                inner.append("cd /job/code")
            if spec.setup:
                inner.append(spec.setup)
            if spec.script_text:
                inner.append(spec.script_text)
            container["entrypoint"] = "/bin/bash"
            container["commands"] = ["-c", "\n".join(inner)]
        elif spec.command:
            # `command` replaces the image ENTRYPOINT, `args` are its arguments
            tokens = shlex.split(spec.command_text)
            container["entrypoint"] = tokens[0]
            container["commands"] = tokens[1:] + list(spec.args)
        elif spec.args:
            container["commands"] = list(spec.args)
        if spec.install_gpu_drivers:
            # NVIDIA driver/libs installed by Batch live here on the host.
            container["volumes"] += [
                "/var/lib/nvidia/lib64:/usr/local/nvidia/lib64",
                "/var/lib/nvidia/bin:/usr/local/nvidia/bin",
            ]
            container["options"] = "--privileged"
        runnables.append({"container": container})
    else:
        body = [build_prelude(spec, code_uri, mounts)] if mounts else [SHEBANG, "set -euo pipefail"]
        if spec.setup:
            body.append(spec.setup)
        body.append(spec.script_text)
        runnables = [{"script": {"text": "\n".join(body)}}]

    task_spec: dict[str, Any] = {
        "runnables": runnables,
        "maxRunDuration": f"{spec.max_run_seconds}s",
        "maxRetryCount": spec.max_retries,
        "environment": {"variables": env},
    }
    if spec.max_retries:
        # retry only when the VM was preempted (exit code 50001), not when your code fails
        task_spec["lifecyclePolicies"] = [
            {"action": "RETRY_TASK", "actionCondition": {"exitCodes": [PREEMPTED_EXIT_CODE]}}
        ]
    if mounts:
        task_spec["volumes"] = [
            {"gcs": {"remotePath": bucket}, "mountPath": path, "mountOptions": GCS_MOUNT_OPTIONS}
            for bucket, path in mounts.items()
        ]

    policy: dict[str, Any] = {
        "machineType": spec.machine,
        "provisioningModel": _PROVISIONING[spec.provisioning],
        "bootDisk": {"sizeGb": str(spec.disk_gb)},
    }
    if spec.disk_type:
        policy["bootDisk"]["type"] = spec.disk_type
    if spec.accelerator_type:
        policy["accelerators"] = [{"type": spec.accelerator_type, "count": str(spec.accelerator_count)}]

    labels = {LABEL_MARK: "true", "gcejob-name": spec.name, **spec.labels}

    allocation: dict[str, Any] = {
        "instances": [{"installGpuDrivers": spec.install_gpu_drivers, "policy": policy}],
        "labels": labels,
    }
    if spec.zones:
        allocation["location"] = {"allowedLocations": [f"zones/{z}" for z in spec.zones]}
    if spec.service_account:
        allocation["serviceAccount"] = {"email": spec.service_account}
    if spec.network or spec.subnetwork or spec.no_external_ip:
        nic: dict[str, Any] = {"noExternalIpAddress": spec.no_external_ip}
        if spec.network:
            nic["network"] = spec.network
        if spec.subnetwork:
            nic["subnetwork"] = spec.subnetwork
        allocation["network"] = {"networkInterfaces": [nic]}

    return {
        "taskGroups": [{"taskCount": "1", "taskSpec": task_spec}],
        "allocationPolicy": allocation,
        "labels": labels,
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


class BatchBackend(Backend):
    def __init__(self, project: str, region: str, client: Any = None, logging_client: Any = None):
        self.project = project
        self.region = region
        self._client = client
        self._logging = logging_client

    # lazy clients so the module imports (and tests run) without credentials
    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import batch_v1

            from gcejob.auth import credentials

            self._client = batch_v1.BatchServiceClient(credentials=credentials(self.project))
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.region}"

    def _name(self, job_id: str) -> str:
        return f"{self.parent}/jobs/{job_id}"

    def submit(self, spec: JobSpec, job_id: str, code_uri: str | None = None) -> JobInfo:
        from google.cloud import batch_v1

        config = build_job_config(spec, job_id, code_uri)
        job = batch_v1.Job.from_json(json.dumps(config))
        created = self.client.create_job(
            request={"parent": self.parent, "job_id": job_id, "job": job}
        )
        return self._to_info(created)

    def get(self, job_id: str) -> JobInfo:
        return self._to_info(self.client.get_job(name=self._name(job_id)))

    def cancel(self, job_id: str) -> None:
        self.client.cancel_job(name=self._name(job_id))

    def delete(self, job_id: str) -> None:
        self.client.delete_job(name=self._name(job_id))

    def list(self, label_filter: dict[str, str] | None = None) -> list[JobInfo]:
        flt = {LABEL_MARK: "true", **(label_filter or {})}
        expr = " AND ".join(f'labels.{k}="{v}"' for k, v in flt.items())
        jobs = self.client.list_jobs(request={"parent": self.parent, "filter": expr})
        return [self._to_info(j) for j in jobs]

    def logs(self, job_id: str, follow: bool = False) -> Iterator[str]:
        info = self.get(job_id)
        seen: set[str] = set()
        while True:
            for entry in self._log_entries(info.uid):
                if entry["id"] not in seen:
                    seen.add(entry["id"])
                    yield entry["text"]
            if not follow or self.get(job_id).terminal:
                # one last drain so the final lines aren't lost
                if follow:
                    time.sleep(3)
                    for entry in self._log_entries(info.uid):
                        if entry["id"] not in seen:
                            seen.add(entry["id"])
                            yield entry["text"]
                return
            time.sleep(5)

    # ---- helpers ---------------------------------------------------------
    def _log_entries(self, uid: str) -> list[dict[str, str]]:
        if self._logging is None:
            from google.cloud import logging as cloud_logging

            from gcejob.auth import credentials

            self._logging = cloud_logging.Client(
                project=self.project, credentials=credentials(self.project)
            )
        flt = (
            f'logName="projects/{self.project}/logs/batch_task_logs" '
            f'AND labels.job_uid="{uid}"'
        )
        out = []
        for e in self._logging.list_entries(filter_=flt, order_by="timestamp asc"):
            text = e.payload if isinstance(e.payload, str) else str(e.payload)
            out.append({"id": getattr(e, "insert_id", None) or f"{e.timestamp}-{text[:20]}", "text": text})
        return out

    @staticmethod
    def _to_info(job: Any) -> JobInfo:
        state_name = getattr(job.status.state, "name", str(job.status.state))
        events = [ev.description for ev in job.status.status_events]
        failure, reason = classify_events(events)
        created = getattr(job, "create_time", None)
        return JobInfo(
            job_id=job.name.rsplit("/", 1)[-1],
            uid=job.uid,
            state=_STATE_MAP.get(state_name, JobState.UNKNOWN),
            events=events,
            labels=dict(job.labels),
            created=created if created else None,
            failure=failure,
            reason=reason,
        )
