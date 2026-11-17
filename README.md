# gcejob

Run training and evaluation jobs on **ephemeral Google Cloud VMs** with a small Python API.

You give it a script or a container image. `gcejob` asks Cloud Batch for a VM, runs your job, streams
the logs, saves outputs to Cloud Storage, and the VM and its boot disk are deleted when the job ends.
You pay only while the job runs.

Use it when you want machine shapes that managed training services don't offer, for example small
or fractional GPUs on Compute Engine.

> API reference: [docs/API.md](docs/API.md) · Runnable example: [examples/EXAMPLE_USAGE.md](examples/EXAMPLE_USAGE.md)

---

## Contents

1. [How it works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Install](#install)
4. [Quickstart](#quickstart)
5. [Usage](#usage)
6. [The `/job` data contract](#the-job-data-contract)
7. [Command line](#command-line)
8. [Cost and safety](#cost-and-safety)
9. [Troubleshooting](#troubleshooting)
10. [Status and limitations](#status-and-limitations)
11. [Development](#development)

---

## How it works

```
your laptop                           Google Cloud
-----------                           ------------
gcejob.ScriptRun(...).start()
  1. upload code           ───────►   Cloud Storage (staging)
  2. submit job            ───────►   Cloud Batch
                                        3. creates a VM (+ GPU driver) with your service account
                                        4. mounts your buckets at /job/input, /job/output, ...
                                        5. runs your command (on the VM, or in your container)
  6. wait, print status    ◄───────      status events
  7. print logs            ◄───────   Cloud Logging
                                        8. outputs are already in Cloud Storage
                                        9. VM and boot disk are deleted
```

Batch owns the VM lifecycle, so there is nothing to clean up by hand when a job finishes or fails.

## Prerequisites

### On your machine
- Python **3.10 or newer**.
- The [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), signed in with application
  default credentials:
  ```
  gcloud auth application-default login
  ```

### In Google Cloud
1. **A project with billing enabled.**
2. **APIs enabled:** Batch, Compute Engine, Cloud Logging, Cloud Storage
   (plus Artifact Registry if you run private images).
   ```
   gcloud services enable batch.googleapis.com compute.googleapis.com \
       logging.googleapis.com storage.googleapis.com artifactregistry.googleapis.com
   ```
3. **A service account for the VMs** (the "runner"). It needs:

   | Role | Why |
   |---|---|
   | `roles/batch.agentReporter` | Lets the Batch agent on the VM report status. **Without it the job stays in `SCHEDULED` forever while the VM keeps billing.** |
   | `roles/logging.logWriter` | Lets the VM write logs. |
   | `roles/artifactregistry.reader` | Only if you pull private container images. |
   | Storage access on your buckets | `objectViewer` on input buckets, `objectAdmin` on output, checkpoint and staging buckets. |

   ```
   gcloud iam service-accounts create sa-gcejob-runner --project=PROJECT
   gcloud projects add-iam-policy-binding PROJECT \
       --member=serviceAccount:sa-gcejob-runner@PROJECT.iam.gserviceaccount.com --role=roles/batch.agentReporter
   gcloud projects add-iam-policy-binding PROJECT \
       --member=serviceAccount:sa-gcejob-runner@PROJECT.iam.gserviceaccount.com --role=roles/logging.logWriter
   ```
4. **Permissions for the person running jobs:**

   | Role | Why |
   |---|---|
   | `roles/batch.jobsEditor` | Create, cancel and delete Batch jobs. |
   | `roles/iam.serviceAccountUser` (on the runner service account) | Run jobs as that account. |
   | `roles/logging.viewer` | Read job logs. |
   | Write access to the staging bucket | Upload your code. |

5. **GPU quota and capacity** in your region, if you use GPUs.
   - Quota is permission (check the GPU quotas for your GPU family in your region).
   - Capacity is whether Google has free hardware. On-demand GPU capacity can run out in a zone even
     when you have quota. `gcejob status` reports this as `capacity`.
6. **Buckets in the same region as the VMs.** Reading data across regions is billed per GB.
7. **Network:** the default VPC works. With `no_external_ip` you need Cloud NAT so the VM can
   `pip install` and pull images.

## Install

```
git clone <this repo> && cd gcejob-runner
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Quickstart

### 1. Check your setup with a CPU job (costs well under a cent)

`examples/cpu_smoke.yaml`:
```yaml
name: cpu-smoke
project: my-project
region: us-west1
service_account: sa-gcejob-runner@my-project.iam.gserviceaccount.com
machine: e2-standard-4
max_run: 5m
command: |
  echo CPU_SMOKE_OK
  hostname
```
```
gcejob validate examples/cpu_smoke.yaml          # checks the file, no GCP calls
gcejob run examples/cpu_smoke.yaml --dry-run     # prints the Batch job it would create
gcejob run examples/cpu_smoke.yaml               # runs it for real
```

### 2. Run a script in Python

```python
import gcejob

gcejob.configure(
    project="my-project",
    region="us-west1",
    staging_bucket="gs://my-bucket",
    service_account="sa-gcejob-runner@my-project.iam.gserviceaccount.com",
)

run = gcejob.ScriptRun("hello", script="train.py", requirements=["numpy"])
run.start(machine="e2-standard-4", args=["--epochs", "1"])
```
`start()` waits for the run, prints status changes, then prints the logs. It raises
`gcejob.JobError` if the job fails and `gcejob.StartupTimeout` if the VM never starts.

## Usage

### A. Run your code (script)

```python
run = gcejob.ScriptRun(
    "finetune-v1",
    script="train.py",                       # the folder containing it is uploaded
    requirements=["torch", "transformers"],  # pip-installed in a fresh virtualenv on the VM
    # source_dir="src/",                     # upload a different folder (script must be inside)
    # image="pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime",  # run inside this image instead
)
run.start(
    machine="g2-standard-4",                 # 1x NVIDIA L4
    args=["--epochs", "5"],
    env={"LR": "0.0003"},
    inputs={"data": "gs://my-bucket/datasets/v1/"},
    output_dir="gs://my-bucket/runs/finetune-v1",
    buckets=["my-bucket"],                   # optional: also mount whole buckets at /gcs/<bucket>
    max_run="10h",
)
```

### B. Run a container image

```python
run = gcejob.ContainerRun(
    "eval-v1",
    image="us-west1-docker.pkg.dev/my-project/repo/train:latest",
    command=["python", "-m", "train"],       # replaces the image ENTRYPOINT
)
run.start(machine="g2-standard-4", args=["--split", "val"],
          inputs={"data": "gs://my-bucket/datasets/v1/"},
          output_dir="gs://my-bucket/runs/eval-v1")
```

### C. Don't wait; check on it later

```python
run.start(wait=False)           # returns right after submit
print(run.resource_name)        # projects/<p>/locations/<r>/jobs/<id>

# later, even from another process:
run = gcejob.ScriptRun.attach("finetune-v1-20261117-104509", project="my-project", region="us-west1")
print(run.state)                # JobState.RUNNING
for line in run.logs(follow=True):
    print(line)
run.cancel()
```

### D. From a YAML file

```yaml
name: finetune-v1
project: my-project
region: us-west1
service_account: sa-gcejob-runner@my-project.iam.gserviceaccount.com

machine: g2-standard-4
provisioning: standard          # standard | spot | flex_start
disk_gb: 200
max_run: 10h
startup_timeout: 20m            # how long to wait for the VM to reach RUNNING

code: ./my_project
staging: gs://my-bucket/gcejob/staging/
setup: pip install -r requirements.txt
command: python train.py --epochs 5

inputs:
  data: gs://my-bucket/datasets/v1/
output: gs://my-bucket/runs/finetune-v1/
checkpoints: gs://my-bucket/runs/finetune-v1/ckpt/
env:
  LR: "0.0003"
```
```python
from gcejob import Job
Job.from_yaml("job.yaml").run()
```
or `gcejob run job.yaml`.

### What to put in your training code

```python
import os, pathlib

data = pathlib.Path("/job/input/data")                 # your `inputs={"data": ...}`
out = pathlib.Path(os.environ["GCEJOB_OUTPUT_DIR"])    # /job/output       -> saved to GCS
ckpt = pathlib.Path(os.environ["GCEJOB_CHECKPOINT_DIR"])  # /job/checkpoints -> saved to GCS
```
Anything you print goes to the job logs.

## The `/job` data contract

| Path in the job | Comes from | Notes |
|---|---|---|
| `/job/input/<name>` | `inputs={"<name>": "gs://..."}` | Read data from here. |
| `/job/output` | `output_dir/model` (or `output:` in YAML) | Write results here; they are in GCS when the job ends. |
| `/job/checkpoints` | `output_dir/checkpoints` (or `checkpoints:`) | Write checkpoints here so a rerun can resume. |
| `/job/code` | `script` / `code` folder | Your code, unpacked here; the job starts in this directory. |
| `/gcs/<bucket>/...` | `buckets=[...]`, and every bucket used by `inputs` / `output_dir` | The whole bucket, read and write. Use it when your code expects plain `/gcs/<bucket>/path` files. |

Environment variables set for you: `GCEJOB_NAME`, `GCEJOB_JOB_ID`, `GCEJOB_INPUT_DIR`,
`GCEJOB_OUTPUT_DIR`, `GCEJOB_CHECKPOINT_DIR`, plus yours.

Buckets are mounted with Cloud Storage FUSE, which is fine for large sequential files (such as
512 MB shards) but slower than a local disk for many small files or random access.

## Command line

```
gcejob validate job.yaml [--show-config]    check a spec; print the Batch job it would create
gcejob run job.yaml [--no-wait] [--dry-run] submit; by default wait, print logs, exit 1 on failure
gcejob status <job-id>                      state, and the cause if it is stuck or failed
gcejob logs <job-id> [-f]                   print or follow logs
gcejob cancel <job-id>                      cancel; Batch removes the VM and disk
gcejob list                                 list gcejob jobs in the region
gcejob cleanup [--older-than 24h] [--include-active] [--stuck] [--yes]   dry run unless --yes
```
Commands that take a job id need `--project` and `--region` (or `GCEJOB_PROJECT` and `GCEJOB_REGION`).

## Cost and safety

- **Billing starts when the VM is created**, not when your code starts. Boot and GPU driver install
  (several minutes on a fresh VM) are billed.
- **On-demand by default.** `spot` and `flex_start` are opt-in (`provisioning=`). With Spot, a preempted
  task is re-run up to 3 times (`max_retries`); your script must resume from its checkpoints. Set
  `startup_timeout` to how long you will wait for Spot capacity (for example `"6h"`).
- **Time limits:** `max_run` caps the task (default 24 hours); `startup_timeout` cancels a job that
  never reaches `RUNNING` (default 15 minutes).
- **Labels:** every job is labelled `gcejob=true` and `gcejob-name=<name>`, so you can find it in
  billing and run `gcejob cleanup`.
- **Data transfer:** keep buckets and VMs in one region; cross-region reads are billed per GB.
- **Stuck jobs:** while you wait, a job that never starts is cancelled (`startup_timeout`). If you
  don't wait (`wait=False`), nothing is watching, so run this on a schedule (cron, CI, Cloud
  Scheduler); it only touches jobs still `QUEUED`/`STARTING`, never running or finished ones:
  ```
  gcejob cleanup --stuck --older-than 30m --yes --project PROJECT --region REGION
  ```
  GPU driver install takes several minutes, so don't set it much below 30m.
- **Also do once:** a billing budget alert on the project, and a lifecycle rule on the staging
  bucket (delete `gcejob/staging/` objects after 7 days).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Job stays `SCHEDULED`, VM exists and is billing | Runner service account lacks `batch.agentReporter` or `logging.logWriter` | Grant the roles (see Prerequisites). |
| `status` shows `capacity` / `ZONE_RESOURCE_POOL_EXHAUSTED` | The zone has no free GPUs or machines of that type | Retry later, allow more zones (`zones=[...]`), try another region, or use a reservation or `flex_start`. |
| `status` shows `quota` | Not enough GPU quota in the region | Request a quota increase for that GPU family. |
| `PERMISSION_DENIED` on submit | You lack `batch.jobsEditor` or `serviceAccountUser` | Grant the role to yourself. |
| `API [batch.googleapis.com] not enabled` | Batch API is off | `gcloud services enable batch.googleapis.com`. (It can take a minute to propagate.) |
| `StartupTimeout` | VM didn't reach `RUNNING` in `startup_timeout` | GPU driver installs are slow; raise `startup_timeout` (for example `20m`), or check `status` for the cause. |
| Job fails instantly, logs show missing `/job/input/...` | Wrong `gs://` path, or no read access | Check the path and the service account's bucket access. |

## Status and limitations

- Version 0.1, Cloud Batch backend only. Unit-tested with mocks; the CPU path was proven against
  real Batch in manual tests.
- **Not yet proven live:** GPU jobs, the GCS-mount layout, container jobs, running a script inside a
  container with uploaded code, the host virtualenv setup, and `flex_start`.
- One VM per job; no multi-node training.
- No automatic retry across zones or regions yet.
- On-demand GPU capacity can run out; this package reports it but can't create capacity.

## Development

```
pip install -e ".[dev]"
pytest                                   # unit tests, no GCP access needed
ruff check .
```
Layout:
```
src/gcejob/
  runs.py          Python API: configure, ScriptRun, ContainerRun
  job.py           Job: submit, wait, startup timeout
  spec.py          JobSpec: the validated job description
  backends/        Backend interface and the Batch implementation
  storage.py       code archive and upload
  cleanup.py       find old or stuck jobs
  cli.py           the `gcejob` command
docs/API.md        API reference
examples/          runnable examples and EXAMPLE_USAGE.md
```
