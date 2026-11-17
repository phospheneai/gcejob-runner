# gcejob

[![PyPI](https://img.shields.io/pypi/v/gcejob)](https://pypi.org/project/gcejob/)
[![License](https://img.shields.io/pypi/l/gcejob)](https://github.com/phospheneai/gcejob-runner/blob/main/LICENSE)

Run a script or a container on a short-lived Google Cloud VM. Logs stream back, outputs go to
Cloud Storage, and the VM and its disk are deleted when the job ends. You pay only while it runs.

Good for training and evaluation jobs that need machine shapes (like small GPUs) that managed
training services don't offer.

Small by design: about 1,300 lines of Python, no server or daemon, and no cluster to manage. It
only wraps Google's own client libraries around Cloud Batch.

- [API reference](https://github.com/phospheneai/gcejob-runner/blob/main/docs/API.md)
- [Run the example](https://github.com/phospheneai/gcejob-runner/blob/main/examples/EXAMPLE_USAGE.md)

## Quickstart

```
pip install gcejob
gcloud auth application-default login
```

```python
import gcejob

gcejob.configure(project="my-project", region="us-west1",
                 staging_bucket="gs://my-bucket",
                 service_account="sa-gcejob-runner@my-project.iam.gserviceaccount.com")

gcejob.ScriptRun("hello", script="train.py").start(machine="e2-standard-2")
```

This needs a GCP project, a service account and a bucket. See [Setup](#setup) first.

Example output (your script's output streams in while the job is `RUNNING`):

```
submitted projects/my-project/locations/us-west1/jobs/hello-20261117-104509
hello-20261117-104509: QUEUED
hello-20261117-104509: STARTING
hello-20261117-104509: RUNNING
<your script's output, line by line as it runs>
hello-20261117-104509: SUCCEEDED
```

Status lines and log lines are fetched separately, so the last few log lines can appear just
before or after `SUCCEEDED`.

## How it works

```
your laptop                           Google Cloud
-----------                           ------------
gcejob.ScriptRun(...).start()
  1. upload code           ───────►   Cloud Storage (staging)
  2. submit job            ───────►   Cloud Batch
                                        3. creates a VM (+ GPU driver) with your service account
                                        4. mounts your buckets at /gcs/<bucket> and /job/...
                                        5. runs your command (on the VM, or in your container)
  6. wait, print status    ◄───────      status events
  7. print logs            ◄───────   Cloud Logging
                                        8. outputs are already in Cloud Storage
                                        9. VM and boot disk are deleted
```

Batch owns the VM lifecycle, so there is nothing to clean up by hand when a job finishes or fails.

## Setup

You need:

1. **Python 3.10+** and the [gcloud CLI](https://cloud.google.com/sdk/docs/install).
2. **A GCP project** with billing and these APIs enabled:
   ```
   gcloud services enable batch.googleapis.com compute.googleapis.com \
       logging.googleapis.com storage.googleapis.com
   ```
3. **A service account for the VMs** with these roles:
   - `roles/batch.agentReporter` (without it, jobs hang while the VM bills)
   - `roles/logging.logWriter`
   - read/write access to your bucket
   - `roles/artifactregistry.reader` (only for private images)
4. **A bucket** in the same region as the VM (your code is uploaded there).
5. **For you:** `roles/batch.jobsEditor`, `roles/logging.viewer`, and `roles/iam.serviceAccountUser`
   on the service account.
6. **GPU quota and free capacity**, if you use GPUs.

Once that is done, install and log in as shown in the [Quickstart](#quickstart). From a source
checkout, use `pip install -e .` instead of `pip install gcejob`.

## Run a script

```python
import gcejob

gcejob.configure(
    project="my-project",
    region="us-west1",
    staging_bucket="gs://my-bucket",
    service_account="sa-gcejob-runner@my-project.iam.gserviceaccount.com",
)

run = gcejob.ScriptRun("hello", script="train.py", requirements=["numpy"])
run.start(machine="e2-standard-2", args=["--epochs", "1"])
```

`start()` waits, prints the status changes, then prints your script's output.

## Run a container

```python
run = gcejob.ContainerRun(
    "eval",
    image="us-west1-docker.pkg.dev/my-project/repo/train:latest",
    command=["python", "-m", "train"],
)
run.start(machine="g2-standard-4", args=["--split", "val"], buckets=["my-bucket"])
```

## Your data

Buckets are mounted as files. Your code reads and writes them like normal folders:

| Path in the job | What it is |
|---|---|
| `/gcs/<bucket>/...` | A whole bucket (list it in `buckets=[...]`) |
| `/job/input/<name>` | A path you pass in `inputs={"name": "gs://..."}` |
| `/job/output` | Saved to `<output_dir>/model` when the job ends |
| `/job/checkpoints` | Saved to `<output_dir>/checkpoints` |

## Command line

Describe a job in YAML and run it without writing Python:

```yaml
# job.yaml
name: cpu-smoke
project: my-project
region: us-west1
service_account: sa-gcejob-runner@my-project.iam.gserviceaccount.com
machine: e2-standard-4
max_run: 5m
command: |
  echo CPU_SMOKE_OK
```

More in [examples/](https://github.com/phospheneai/gcejob-runner/blob/main/examples/).

```
gcejob run job.yaml          # run a job from a YAML file
gcejob status <job-id>       # state, and why if it is stuck
gcejob logs <job-id> -f      # follow logs
gcejob cancel <job-id>
gcejob list                  # job id, name, state
gcejob cleanup --stuck --older-than 30m --yes    # cancel jobs that never started
```
Commands that take a job id need `--project` and `--region` (or `GCEJOB_PROJECT`, `GCEJOB_REGION`).

## Cost safety

- Billing starts when the VM is created, so boot time counts.
- Jobs are on-demand by default. Spot is opt-in: `provisioning="spot"`.
- `max_run` (default 24h) caps the job. `startup_timeout` (default 15m) cancels a job that never starts.
- If you submit with `wait=False`, run `gcejob cleanup --stuck` on a schedule.
- Keep buckets and VMs in the same region, or reads are billed per GB.
- Set a billing budget alert on the project.

## Good to know

**Jobs**
- Every job gets a fresh VM, so nothing carries over between jobs. Save anything you need to
  `/job/output`, `/job/checkpoints` or a bucket.
- Your script runs as root, in `/job/code`, with bash. The job id is `<name>-<timestamp>`.
- Logs stream live while the job runs (a few seconds behind, since they go through Cloud Logging).
  To follow a job from another terminal, use `gcejob logs <job-id> -f`.
- One VM per job. There is no multi-node training.
- On Spot, a preempted job is re-run from the start (up to `max_retries`, default 3). Your script
  must resume from its own checkpoints.

**Machines**
- The boot disk must be at least **30 GB** (`disk_gb`, default 100).
- Allow 3 to 10 minutes before your code starts: VM boot, plus GPU driver install on GPU machines.
  Set `startup_timeout` to match (for example `"20m"`).
- G2, G4, A2 and A3 machines include their GPU. Other families need an N1 machine plus
  `accelerator=...`.
- Some newer machine families only accept Hyperdisk boot disks. Set `disk_type` if the job is rejected.
- Quota is not capacity. Having GPU quota doesn't mean Google has free GPUs in the zone.

**Data**
- Only buckets you list (`buckets`, `inputs`, `output_dir`) are mounted. Anything else is missing
  from the VM.
- Buckets are mounted with Cloud Storage FUSE: fast for large sequential files (such as 512 MB
  shards), slow for many small files and random access.
- Your uploaded code stays in `<staging_bucket>/gcejob/staging/`. Add a lifecycle rule to expire it.

**Access**
- Your login needs a quota project. gcejob uses the job's project automatically when yours has none.
- The VMs run as the service account you pass, not as you. Give it access to every bucket and image
  the job uses.

## Troubleshooting

| You see | Do this |
|---|---|
| Job stays `SCHEDULED` | The service account is missing `batch.agentReporter` or `logging.logWriter`. |
| `capacity` in `gcejob status` | Google has no free machines of that type in the zone. Retry later or try another region. |
| `PERMISSION_DENIED` on submit | Grant yourself `batch.jobsEditor` and `serviceAccountUser`. |
| `Disk cannot be smaller than the chosen image` | Use `disk_gb` of 30 or more. |
| `Quota exceeded ... project_number:<number>` | Run `gcloud auth application-default set-quota-project <your-project>`. |
| Missing `/gcs/<bucket>` or `/job/input/...` | The bucket isn't in `buckets`, `inputs` or `output_dir`, or the service account can't read it. |

## Footprint

gcejob is a thin layer, so its size is mostly Google's client libraries. Installed into a clean
virtual environment (pip's own files excluded, measured 2026-11-17):

| Package | Install size |
|---|---|
| `google-cloud-batch` alone (raw client) | 73 MB |
| **gcejob** | **106 MB** |
| `dstack` (client only) | 121 MB |
| `google-cloud-aiplatform` (Vertex AI) | 282 MB |
| `skypilot[gcp]` | 481 MB |

These tools do different things (SkyPilot and dstack also cover other clouds and cluster
management), so this compares install weight, not features. Sizes change with each release.

## Status

**Experimental (0.0.1), Cloud Batch only.** Verified on real GCP: CPU jobs, code upload and the
startup timeout. Not yet verified on real hardware: live log streaming, bucket mounts, container
jobs, GPU jobs and Spot retry. Expect rough edges and report them as issues.

## Development

```
pip install -e ".[dev]"
pytest
```

## License

Apache-2.0. See [LICENSE](https://github.com/phospheneai/gcejob-runner/blob/main/LICENSE).
