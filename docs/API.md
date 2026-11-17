# gcejob API reference

Version 0.0.1. For setup, prerequisites and examples, see the [README](../README.md) and the [examples](../examples/EXAMPLE_USAGE.md).

```python
import gcejob
```

Public names: `configure`, `ScriptRun`, `ContainerRun`, `Job`, `JobSpec`, `JobInfo`, `JobState`,
`FailureKind`, `JobError`, `StartupTimeout`.

- [Python API](#python-api): `configure`, `ScriptRun`, `ContainerRun`
- [Start options](#start-options)
- [Run members](#run-members)
- [Lower-level API](#lower-level-api): `Job`, `JobSpec`
- [Types](#types): `JobState`, `FailureKind`, `JobInfo`, exceptions
- [Backends](#backends)
- [Command line](#command-line)
- [Conventions](#conventions)

---

## Python API

### `gcejob.configure(...)`

Sets process-wide defaults. Every argument is optional; only the ones you pass change.

```python
gcejob.configure(*, project=None, region=None, staging_bucket=None, service_account=None,
                 network=None, subnetwork=None, no_external_ip=None, zones=None, labels=None)
```

| Argument | Default | Description |
|---|---|---|
| `project` | `$GCEJOB_PROJECT`, then `$GOOGLE_CLOUD_PROJECT` | GCP project. Required (here or by environment). |
| `region` | `us-west1` | GCP region where jobs run. |
| `staging_bucket` | none | `gs://bucket` where code is uploaded (under `gcejob/staging/`). Required for code runs. |
| `service_account` | none | Email of the service account the VM runs as. |
| `network`, `subnetwork` | none | VPC network and subnetwork. |
| `no_external_ip` | `False` | Give VMs no public IP (needs Cloud NAT). |
| `zones` | `[]` | Allowed zones; empty means any zone in the region. |
| `labels` | `{}` | Labels added to every job. |

---

### `gcejob.ScriptRun`

Run a local Python script.

```python
ScriptRun(name, script, image=None, requirements=None, source_dir=None, *,
          project=None, region=None, staging_bucket=None, labels=None, backend=None)
```

| Argument | Description |
|---|---|
| `name` | Run name. Converted to a valid id: lowercase, hyphens, max 42 chars. A timestamp is appended for uniqueness. |
| `script` | Path to your script. Raises `FileNotFoundError` if it doesn't exist. |
| `image` | Optional container image to run the script in. Without it the script runs directly on the VM. |
| `requirements` | List of pip requirements, installed before the script runs. |
| `source_dir` | Folder to upload. Default: the script's folder. The script must be inside it. Archives over 500 MB are rejected. |
| `project`, `region`, `staging_bucket`, `labels` | Override the `configure()` defaults for this run. |
| `backend` | Advanced: a custom `Backend` (used in tests). |

`start(*, args=None, wait=True, show_logs=True, **options)`

Runs `python3 <script> <args>` (or `python` inside an image) from the unpacked code directory.

---

### `gcejob.ContainerRun`

Run a container image.

```python
ContainerRun(name, image, command=None, *, project=None, region=None,
             staging_bucket=None, labels=None, backend=None)
```

| Argument | Description |
|---|---|
| `image` | Image to run, e.g. `us-west1-docker.pkg.dev/proj/repo/train:latest`. |
| `command` | List replacing the image ENTRYPOINT (and CMD). Omit to use the image default. |

`start(*, args=None, wait=True, show_logs=True, **options)`

`args` are appended to `command` (or become the image's CMD arguments if `command` is omitted).

---

## Start options

Passed to `start()` of `ScriptRun` and `ContainerRun`. An unknown option raises `TypeError`.

| Option | Default | Description |
|---|---|---|
| `args` | `[]` | Arguments for your script or container. |
| `env` | `{}` | Extra environment variables. |
| `machine` | `e2-standard-4` | Compute Engine machine type, e.g. `e2-standard-4`, `g2-standard-4`, `g4-standard-6`. |
| `accelerator` | `None` | For `n1-*` machines, e.g. `NVIDIA_TESLA_T4` (attached as `nvidia-tesla-t4`). For `g2-`, `g4-`, `a2-`, `a3-`, `a4-` machines the GPU is part of the machine type and this is ignored. Other machine families raise `ValueError`. |
| `accelerator_count` | `1` | Number of N1 accelerators. |
| `disk_type` | provider default | Boot disk type, e.g. `pd-balanced`, `pd-ssd`, `hyperdisk-balanced`. |
| `disk_gb` | `100` | Boot disk size in GB (minimum 30: the default Batch image is 30 GB). |
| `output_dir` | `None` | `gs://bucket/prefix`. Output goes to `<prefix>/model` and checkpoints to `<prefix>/checkpoints`. |
| `inputs` | `{}` | `{"name": "gs://bucket/prefix/"}`, mounted at `/job/input/<name>`. |
| `buckets` | `[]` | Buckets (names or `gs://` URIs) mounted whole at `/gcs/<bucket>`. Buckets used by `inputs`, `output_dir` and code are mounted automatically. Only listed buckets exist in the VM. |
| `service_account` | from `configure` | VM service account. |
| `network` | from `configure` | VPC network. |
| `max_run` | `"24h"` | Maximum task run time: seconds, or `"90s"`, `"15m"`, `"10h"`, `"2d"`. |
| `provisioning` | `"standard"` | `"standard"` (on-demand), `"spot"`, or `"flex_start"`. |
| `zones` | from `configure` | Restrict to these zones (must be in the region). |
| `startup_timeout` | `"15m"` | Cancel if the job isn't `RUNNING` by then. For Spot, set it to how long you're willing to wait for capacity, e.g. `"6h"`. |
| `max_retries` | `3` for Spot, `0` otherwise | Re-run the task up to this many times (max 10) if the Spot VM is preempted. Your code failing is never retried. Your script must resume from its own checkpoints. |
| `wait` | `True` | Wait for completion. `False` returns after submitting. |
| `show_logs` | `True` | With `wait=True`, stream the job logs live while it runs (a few seconds behind, via Cloud Logging). Also on failure. |

`start()` returns `None`. It raises `JobError` if the job fails, `StartupTimeout` if it never starts,
and `ValueError` / `TypeError` for invalid options.

Environment set for your code: `GCEJOB_NAME`, `GCEJOB_JOB_ID`, `GCEJOB_INPUT_DIR` (`/job/input`),
`GCEJOB_OUTPUT_DIR` (`/job/output`), `GCEJOB_CHECKPOINT_DIR` (`/job/checkpoints`).

---

## Run members

Available on `ScriptRun` and `ContainerRun` after `start()`.

| Member | Description |
|---|---|
| `run.state` | Current `JobState` (queries the service). |
| `run.job_id` | The Batch job id (`<name>-<YYYYMMDD-HHMMSS>`). |
| `run.resource_name` | `projects/<p>/locations/<r>/jobs/<id>`. |
| `run.model_uri` | `<output_dir>/model`, or `None`. |
| `run.wait(show_logs=True)` | Block until finished; returns `JobInfo`. Raises `JobError` / `StartupTimeout`. |
| `run.logs(follow=False)` | Iterator of log lines. With `follow=True`, keeps yielding until the job ends. |
| `run.cancel()` | Ask Batch to cancel the job; it removes the VM and disk. |
| `run.delete()` | Delete the job record (and its VM if still attached). |
| `Class.attach(job_id, *, project=None, region=None)` | Handle for an existing job, by id or full resource name. |

Calling these before `start()` raises `RuntimeError("run has not been started")`.

---

## Lower-level API

### `gcejob.JobSpec`

A validated description of one job (pydantic model; unknown fields are rejected).

```python
JobSpec.from_yaml(path, **overrides)    # relative `code` paths resolve against the YAML file
JobSpec.from_dict(data)
```

| Field | Default | Description |
|---|---|---|
| `name` | required | Lowercase letters, digits, hyphens; starts with a letter; max 42 chars. |
| `project` | required | GCP project. |
| `region` | `us-west1` | GCP region. |
| `zones` | `[]` | Allowed zones (must start with the region). |
| `service_account` | `None` | VM service account email. |
| `machine` | `e2-standard-4` | Machine type. |
| `gpu_drivers` | auto | Install NVIDIA drivers. Auto = true for `g2-`, `g4-`, `a2-`, `a3-`, `a4-` machines or when an accelerator is set. |
| `accelerator_type`, `accelerator_count` | `None`, `1` | N1 accelerators only, e.g. `nvidia-tesla-t4`. |
| `provisioning` | `standard` | `standard`, `spot`, or `flex_start`. |
| `disk_gb`, `disk_type` | `100`, `None` | Boot disk. |
| `network`, `subnetwork`, `no_external_ip` | `None`, `None`, `False` | Networking. |
| `max_run` | `1h` | Max task run time (`90s`, `15m`, `10h`, `2d`, or seconds). |
| `startup_timeout` | `15m` | Max wait for `RUNNING`. |
| `max_retries` | `0` | Re-run the task after a Spot preemption, up to this many times (0 to 10). |
| `command` | `None` | Shell string, or list of arguments. Required unless `image` is set. |
| `args` | `[]` | Appended to the command (quoted). |
| `setup` | `None` | Shell run before the command (e.g. `pip install -r requirements.txt`). |
| `code` | `None` | Local folder to upload (needs `staging`). |
| `image` | `None` | Container image. May be combined with `code`. |
| `env` | `{}` | Environment variables. |
| `inputs` | `{}` | `{name: gs://...}` mounted at `/job/input/<name>`. |
| `buckets` | `[]` | Buckets mounted whole at `/gcs/<bucket>` (names or `gs://` URIs). |
| `output`, `checkpoints` | `None` | `gs://` prefixes mounted at `/job/output`, `/job/checkpoints`. |
| `staging` | `None` | `gs://` prefix for code uploads. |
| `labels` | `{}` | Added to the job; `gcejob=true` and `gcejob-name` are always set. |

Validation rules: needs `command` or `image`; `code` needs `staging`; `accelerator_type` only with
`n1-*` machines; zones must be in the region; `inputs`, `output`, `checkpoints`, `staging` must be
`gs://` URIs. Note: `JobSpec` and YAML default `max_run` to `1h`; `ScriptRun` and `ContainerRun`
default to `24h`.

### `gcejob.Job`

```python
Job(spec, backend=None, unique=True)
Job.from_yaml(path, backend=None, **overrides)
Job.attach(job_id, project, region, backend=None)   # handle for an existing job
```

| Member | Description |
|---|---|
| `submit()` | Upload code (if any) and submit. Returns the `Job`. |
| `wait(poll=10.0, on_update=None)` | Poll until finished. Raises `JobError` on failure, `StartupTimeout` (after cancelling) if not `RUNNING` within `startup_timeout`. The timeout only applies until the job has been `RUNNING` once. `on_update(info)` is called on each state change. |
| `wait_and_stream(emit=print, **wait_kwargs)` | Like `wait()`, but prints log lines live on a background thread. If following the logs fails part-way, the rest is read once at the end without repeats. Raises what `wait()` raises, after the logs are drained. |
| `run(**wait_kwargs)` | `submit()` then `wait()`. |
| `refresh()` | Fetch and return the current `JobInfo`. |
| `state`, `job_id`, `info` | Current state, id, last fetched info. |
| `logs(follow=False)`, `cancel()`, `delete()` | As on the run classes. |

With `unique=True` (default) the job id is `<name>-<UTC YYYYMMDD-HHMMSS>`, truncated to 63 chars.

---

## Types

### `JobState`

| Value | Meaning | Terminal |
|---|---|---|
| `QUEUED` | Accepted, waiting to be scheduled | no |
| `STARTING` | VM being created, booting, installing drivers | no |
| `RUNNING` | Your command is running | no |
| `SUCCEEDED` | Finished successfully | yes |
| `FAILED` | Failed (command error, timeout, or startup failure) | yes |
| `CANCELLING` | Cancel requested | no |
| `CANCELLED` | Cancelled | yes |
| `DELETING` | Being deleted | yes |
| `UNKNOWN` | State the package doesn't recognise | no |

`state.terminal` is `True` for the terminal states.

### `FailureKind`

Derived from the job's status events and exposed on `JobInfo.failure`:
`NONE`, `CAPACITY` (zone has no free resources), `QUOTA`, `PERMISSION`, `TASK` (your command
failed), `OTHER`.

### `JobInfo`

Dataclass: `job_id`, `state`, `uid`, `events` (list of status event texts), `labels`, `created`,
`failure` (`FailureKind`), `reason` (short human-readable cause), and `terminal` (property).

### Exceptions

| Exception | Raised when |
|---|---|
| `JobError` | A job ends `FAILED`. `.info` holds the last `JobInfo`. |
| `StartupTimeout` (subclass of `JobError`) | The job isn't `RUNNING` within `startup_timeout`; the job is cancelled first. The message includes the cause if known (for example capacity). |

---

## Backends

`gcejob.backends.Backend` is the interface a service implements:

```python
submit(spec, job_id, code_uri=None) -> JobInfo
get(job_id) -> JobInfo
cancel(job_id) -> None
delete(job_id) -> None
list(label_filter=None) -> list[JobInfo]
logs(job_id, follow=False) -> Iterator[str]
```

`BatchBackend(project, region)` is the Cloud Batch implementation (the default). A custom
backend can be passed as `backend=` to any run class, which is how the tests run without GCP.
`gcejob.backends.batch.build_job_config(spec, job_id, code_uri=None)` returns the Batch job as a
dict without calling GCP; use it to inspect exactly what will be created.

---

## Command line

| Command | Description |
|---|---|
| `gcejob validate FILE [--show-config]` | Validate a spec; print the Batch config with `--show-config`. No GCP calls. |
| `gcejob run FILE [--wait/--no-wait] [--dry-run]` | Submit. By default waits, prints state changes and streams logs live, and exits 1 on failure. `--dry-run` prints the config and exits. |
| `gcejob status JOB_ID` | Print state, and `kind: reason` if known. |
| `gcejob logs JOB_ID [-f]` | Print or follow logs. |
| `gcejob cancel JOB_ID` | Cancel a job. |
| `gcejob list` | List `gcejob` jobs in the region. |
| `gcejob cleanup [--older-than 24h] [--include-active] [--stuck] [--yes]` | Delete finished jobs older than the age. `--include-active` also removes queued/running jobs. `--stuck` only cancels jobs still `QUEUED`/`STARTING` after the age (never running or finished ones), safe to schedule. Dry run unless `--yes`. |

Options for commands that take a job id: `--project` (env `GCEJOB_PROJECT`), `--region`
(env `GCEJOB_REGION`, default `us-west1`).

---

## Conventions

| Path in the job | Meaning |
|---|---|
| `/job/input/<name>` | Mounted input prefixes |
| `/job/output` | Output prefix (persists to GCS) |
| `/job/checkpoints` | Checkpoint prefix (persists to GCS) |
| `/job/code` | Unpacked code; working directory for code runs |
| `/gcs/<bucket>` | A whole bucket (read and write), for each bucket in `buckets` and each bucket used by `inputs` / `output_dir` |

| Label | Value |
|---|---|
| `gcejob` | `true` on every job |
| `gcejob-name` | The job `name` |

Behaviour to be aware of:
- Billing starts when the VM is created; boot and driver install are billed.
- Defaults are conservative: on-demand provisioning, 24 h task timeout, 15 min startup timeout.
- `command` with an image replaces the image ENTRYPOINT; `args` are appended.
- Code is archived without `.git`, `__pycache__`, `.venv`, `venv`, `node_modules` and caches.
