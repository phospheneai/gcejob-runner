"""gcejob: run training and evaluation jobs on ephemeral Google Cloud VMs."""

from gcejob.job import Job, JobError, StartupTimeout
from gcejob.runs import ContainerRun, ScriptRun, configure
from gcejob.spec import JobSpec
from gcejob.states import FailureKind, JobInfo, JobState

__all__ = [
    "ContainerRun", "FailureKind", "Job", "JobError", "JobInfo", "JobSpec", "JobState",
    "ScriptRun", "StartupTimeout", "configure",
]
# Bumped automatically by .github/workflows/release.yml; do not edit for patch releases.
__version__ = "0.0.1"
