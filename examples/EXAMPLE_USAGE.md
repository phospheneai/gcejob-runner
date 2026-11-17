# Example: say hi from a CPU VM

Runs `hello_cpu/job/hello.py` on a small CPU VM, prints its output here, then deletes the VM.
Cost: well under one cent.

## 1. Edit the variables

Open `examples/hello_cpu/run.py` and replace the placeholders at the top:

```python
PROJECT = "your-project-id"
REGION = "us-west1"
STAGING_BUCKET = "gs://your-bucket"
SERVICE_ACCOUNT = "sa-gcejob-runner@your-project-id.iam.gserviceaccount.com"
```

Setup needed first (APIs, service account, roles): see the [README](../README.md#setup).

## 2. Run

```
python examples/hello_cpu/run.py
```

## 3. Expected output

```
submitted projects/your-project-id/locations/us-west1/jobs/hello-cpu-20261117-104509
hello-cpu-20261117-104509: QUEUED
hello-cpu-20261117-104509: STARTING
hello-cpu-20261117-104509: RUNNING
hello-cpu-20261117-104509: SUCCEEDED
hi from gcejob
job id : hello-cpu-20261117-104509
host   : <vm-name>
python : 3.x.y
final state: SUCCEEDED
```

Takes 1 to 5 minutes. Job id, timestamp and VM name will differ.

## Same thing with YAML

```
gcejob run examples/cpu_smoke.yaml
```

Expected output ends with:

```
CPU_SMOKE_OK
<vm-name>
name=cpu-smoke job=cpu-smoke-20261117-104509
done: cpu-smoke-20261117-104509
```

## Check nothing is left running

```
gcloud compute instances list --project your-project-id    # Listed 0 items.
```

Stuck or failing? Run `gcejob status <job-id>` and see the README's
[Troubleshooting](../README.md#troubleshooting).
