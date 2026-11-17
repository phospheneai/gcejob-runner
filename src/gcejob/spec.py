"""Job specification: one declarative description of a job, independent of the backend."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,40}[a-z0-9])?$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_GPU_FAMILIES = ("g2-", "g4-", "a2-", "a3-", "a4-")


def parse_duration(value: str | int) -> int:
    """'90s', '15m', '10h', '2d' or a bare number of seconds -> seconds."""
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("duration must be positive")
        return value
    match = _DURATION_RE.match(value.strip().lower())
    if match:
        seconds = int(match.group(1)) * _UNITS[match.group(2)]
    elif value.strip().isdigit():
        seconds = int(value)
    else:
        raise ValueError(f"invalid duration {value!r}; use e.g. 90s, 15m, 10h, 2d")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def split_gcs_uri(uri: str) -> tuple[str, str]:
    """gs://bucket/some/prefix/ -> ('bucket', 'some/prefix')."""
    if not uri.startswith("gs://"):
        raise ValueError(f"{uri!r} must start with gs://")
    rest = uri[len("gs://"):].strip("/")
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"{uri!r} has no bucket")
    return bucket, prefix.strip("/")


class JobSpec(BaseModel):
    """Everything needed to run one job. Load from YAML with ``JobSpec.from_yaml``."""

    model_config = ConfigDict(extra="forbid")

    # identity / placement
    name: str
    project: str
    region: str = "us-west1"
    zones: list[str] = Field(default_factory=list, description="Allowed zones; empty = any in region")
    service_account: str | None = None

    # machine
    machine: str = "e2-standard-4"
    gpu_drivers: bool | None = Field(
        default=None, description="Install NVIDIA drivers. None = auto (G2/G4/A2/A3/A4 or accelerator set)"
    )
    accelerator_type: str | None = Field(default=None, description="For N1 machines, e.g. nvidia-tesla-t4")
    accelerator_count: int = 1
    provisioning: Literal["standard", "spot", "flex_start"] = "standard"
    disk_gb: int = Field(default=100, ge=30, description="Boot disk; the default Batch image is 30 GB")
    disk_type: str | None = Field(default=None, description="e.g. pd-balanced, hyperdisk-balanced")
    network: str | None = None
    subnetwork: str | None = None
    no_external_ip: bool = False

    # time limits
    max_run: str | int = "1h"
    startup_timeout: str | int = "15m"
    max_retries: int = Field(
        default=0, ge=0, le=10,
        description="Re-run the task this many times if the VM is preempted (Spot). 0 = never.",
    )

    # what to run: a command (optionally with code + setup) OR a container image
    command: str | list[str] | None = None
    args: list[str] = Field(default_factory=list, description="Extra arguments appended to the command")
    setup: str | None = None
    code: str | None = Field(default=None, description="Local directory uploaded to `staging` and unpacked at /job/code")
    image: str | None = Field(default=None, description="Container image; with `code`, code is mounted at /job/code")
    env: dict[str, str] = Field(default_factory=dict)

    # data contract: /job/input/<name>, /job/output, /job/checkpoints
    inputs: dict[str, str] = Field(default_factory=dict)
    buckets: list[str] = Field(
        default_factory=list,
        description="Extra buckets to mount at /gcs/<bucket> (names or gs:// URIs)",
    )
    output: str | None = None
    checkpoints: str | None = None
    staging: str | None = Field(default=None, description="gs:// prefix for code uploads")

    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                "name must be lowercase letters, digits, hyphens; start with a letter; max 42 chars"
            )
        return v

    @field_validator("max_run", "startup_timeout")
    @classmethod
    def _check_duration(cls, v: str | int) -> str | int:
        parse_duration(v)
        return v

    @field_validator("buckets")
    @classmethod
    def _check_buckets(cls, v: list[str]) -> list[str]:
        names = []
        for item in v:
            name = split_gcs_uri(item)[0] if item.startswith("gs://") else item.strip("/")
            if not _BUCKET_RE.match(name):
                raise ValueError(f"{item!r} is not a valid bucket name")
            if name not in names:
                names.append(name)
        return names

    @field_validator("inputs")
    @classmethod
    def _check_inputs(cls, v: dict[str, str]) -> dict[str, str]:
        for key, uri in v.items():
            if not re.match(r"^[A-Za-z0-9_.-]+$", key):
                raise ValueError(f"input name {key!r} must be letters, digits, '_', '.', '-'")
            split_gcs_uri(uri)
        return v

    @field_validator("output", "checkpoints", "staging")
    @classmethod
    def _check_uri(cls, v: str | None) -> str | None:
        if v is not None:
            split_gcs_uri(v)
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> JobSpec:
        if not self.command and not self.image:
            raise ValueError("set `command` and/or `image`")
        if self.code and not self.staging:
            raise ValueError("`code` needs `staging` (a gs:// prefix to upload it to)")
        if self.accelerator_type and not self.machine.startswith("n1-"):
            raise ValueError("`accelerator_type` is only for n1-* machines; G2/G4/A2/A3 include the GPU")
        for z in self.zones:
            if not z.startswith(self.region + "-"):
                raise ValueError(f"zone {z!r} is not in region {self.region!r}")
        return self

    # ---- derived helpers -------------------------------------------------
    @property
    def install_gpu_drivers(self) -> bool:
        if self.gpu_drivers is not None:
            return self.gpu_drivers
        return bool(self.accelerator_type) or self.machine.startswith(_GPU_FAMILIES)

    @property
    def max_run_seconds(self) -> int:
        return parse_duration(self.max_run)

    @property
    def startup_timeout_seconds(self) -> int:
        return parse_duration(self.startup_timeout)

    @property
    def command_text(self) -> str:
        if isinstance(self.command, list):
            return " ".join(_shell_quote(p) for p in self.command)
        return self.command or ""

    @property
    def script_text(self) -> str:
        """Command plus quoted args, as one shell line."""
        parts = [self.command_text] if self.command_text else []
        parts += [_shell_quote(a) for a in self.args]
        return " ".join(parts)

    # ---- loading ---------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobSpec:
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | Path, **overrides: Any) -> JobSpec:
        data = yaml.safe_load(Path(path).read_text()) or {}
        data.update({k: v for k, v in overrides.items() if v is not None})
        spec = cls.model_validate(data)
        # a relative `code` directory is relative to the YAML file
        if spec.code and not Path(spec.code).is_absolute():
            spec = spec.model_copy(update={"code": str((Path(path).parent / spec.code).resolve())})
        return spec


def _shell_quote(part: str) -> str:
    import shlex

    return shlex.quote(part)
