# Testing status

What has been tested, and what is left for later. Update this file when you test something new.

## Unit tests (no GCP needed)

```
pip install -e ".[dev]"
pytest
```

95 tests cover the job spec, the generated Batch config, the Python API, the CLI, cleanup selection,
and the example files. Cloud calls are replaced by fakes.

A unit test only checks what we *send* to Batch. It can't tell whether Batch accepts it, so the real
runs below matter.

## Tested on real GCP

All on small CPU VMs (`e2-small`, 30 GB disk, on-demand, `us-west1`).

| Area | Result |
|---|---|
| `ScriptRun`: code upload, logs, `output_dir`, `inputs` | Works |
| `/gcs/<bucket>` mount, read and write, from a script | Works |
| `ContainerRun` with `/gcs` visible inside the container | Works |
| `ScriptRun(requirements=[...])` (pip installed on the VM) | Works |
| `status`, `logs`, `attach` by job id | Works |
| `StartupTimeout` cancels a job that never reaches `RUNNING` | Works |
| `gcejob cleanup --stuck` (dry run, then `--yes`) | Works |
| Nothing left behind (jobs, VMs, disks) after each run | Confirmed |

Bugs these runs found and fixed: scripts need a `#!/bin/bash` first line; the Batch list filter needs
the label value quoted; the boot disk must be at least 30 GB; the VM image has no pip; API quota must
be billed to the job's project.

## Not tested yet

| Area | Why it matters | How to test it |
|---|---|---|
| **GPU jobs** (driver install, `nvidia-smi`, CUDA) | The main use case. Never ran: G4 had no capacity, L4 hit capacity once. | One `g2-standard-4` (L4) job that runs `nvidia-smi`. Set `startup_timeout="20m"`. Needs GPU quota and free capacity. |
| **GPU in a container** | The `--privileged` flag and NVIDIA volume mounts are a best guess. | `ContainerRun` on a GPU machine with a CUDA image that runs `nvidia-smi`. |
| **Fractional GPU (`g4-standard-6`)** | Never got a VM. Unknown whether CUDA works on the slice, and whether the disk type must be Hyperdisk. | Same job as above on `g4-standard-6`; try `disk_type="hyperdisk-balanced"` if rejected. |
| **Spot retry after preemption** | The retry rule assumes preemption exit code `50001`. Not checked on a real preempted VM. | Run a long job with `provisioning="spot"` and preempt the VM (`gcloud compute instances simulate-maintenance-event` or `stop`), then check it is re-run. |
| **Flex-start** (`provisioning="flex_start"`) | Not confirmed that Batch accepts it for the machine type. | Submit a CPU job with `provisioning="flex_start"` and a long `startup_timeout`. |
| **`wait=False` plus scheduled `cleanup --stuck`** | Each part works, the combined pattern wasn't run. | Submit with `wait=False`, kill your shell, and check the scheduled cleanup cancels a job that can't start. |
| **Other machine families** (Arm, Hyperdisk-only, C3/C4/N4, local SSD) | Boot image and disk type may differ. | Try one job per family you plan to use. |
| **`no_external_ip` with Cloud NAT** | pip and image pulls need NAT. | Run `ScriptRun(requirements=...)` with `no_external_ip` set in `configure()`. |
| **Large inputs** (many GB of 512 MB shards) | FUSE throughput is unmeasured. | Read a real shard set and time it. |
| **Multiple buckets** (`buckets=[a, b]`) | Only one bucket was mounted in the real runs. | Mount two buckets and read from both. |
| **Windows images, TPUs, Confidential VMs** | Not supported. | Not planned. |

## Running a live test

1. Use a **small CPU VM** and a throwaway prefix in a bucket (for example `gs://your-bucket/test/gcejob-smoke/`).
2. Keep jobs short: `max_run="5m"`, `startup_timeout="15m"`, disk 30 GB.
3. After every run, confirm nothing is left:
   ```
   gcloud batch jobs list --location REGION --project PROJECT
   gcloud compute instances list --project PROJECT
   gcloud compute disks list --project PROJECT
   ```
4. Delete the test objects you created.

Cost for the CPU tests above was well under a cent each. A GPU test is a few cents.
