"""Python API: configure + ScriptRun / ContainerRun."""

import pytest
from pydantic import ValidationError

import gcejob
from gcejob import runs
from gcejob.backends.base import Backend
from gcejob.backends.batch import build_job_config
from gcejob.states import JobInfo, JobState


class Recorder(Backend):
    """Records what would be submitted; reports SUCCEEDED so waiting runs finish."""

    def __init__(self):
        self.spec = self.job_id = self.code_uri = None

    def submit(self, spec, job_id, code_uri=None):
        self.spec, self.job_id, self.code_uri = spec, job_id, code_uri
        return JobInfo(job_id, JobState.QUEUED)

    def get(self, job_id):
        return JobInfo(job_id, JobState.SUCCEEDED)

    def cancel(self, job_id): ...
    def delete(self, job_id): ...
    def list(self, label_filter=None): return []
    def logs(self, job_id, follow=False): return iter(["hello from job"])


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(runs, "_config", runs._Config())
    monkeypatch.setattr("gcejob.job.upload_code", lambda d, s, jid, p: f"{s.rstrip('/')}/{jid}-code.tar.gz")
    gcejob.configure(project="proj", region="us-west1", staging_bucket="gs://bkt",
                     service_account="sa@proj.iam.gserviceaccount.com")


@pytest.fixture
def script(tmp_path):
    (tmp_path / "train.py").write_text("print('hi')")
    return tmp_path / "train.py"


def test_configure_requires_project(monkeypatch):
    monkeypatch.setattr(runs, "_config", runs._Config())
    monkeypatch.delenv("GCEJOB_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(ValueError, match="no project"):
        gcejob.ContainerRun("x", image="img")


def test_configure_only_changes_what_is_passed():
    gcejob.configure(region="us-central1")
    assert runs._config.region == "us-central1" and runs._config.project == "proj"


# ---- from code --------------------------------------------------------------------------
def test_script_run_on_host(script, capsys):
    be = Recorder()
    run = gcejob.ScriptRun("My Finetune #1", script=str(script),
                           requirements=["torch==2.4", "transformers"], backend=be)
    run.start(machine="g2-standard-4", args=["--epochs", "5"], output_dir="gs://bkt/runs/1",
              env={"LR": "0.1"})
    s = be.spec
    assert s.name == "my-finetune-1"
    assert s.image is None and s.code == str(script.parent)
    assert s.command == "python3 train.py" and s.args == ["--epochs", "5"]
    assert "python3 -m pip install -q torch==2.4 transformers" in s.setup and "venv" in s.setup
    assert s.machine == "g2-standard-4" and s.install_gpu_drivers
    assert s.staging == "gs://bkt/gcejob/staging"
    assert s.service_account == "sa@proj.iam.gserviceaccount.com"
    assert s.output == "gs://bkt/runs/1/model" and s.checkpoints == "gs://bkt/runs/1/checkpoints"
    assert s.env == {"LR": "0.1"}
    assert s.provisioning == "standard"
    assert s.max_run_seconds == 24 * 3600
    assert be.code_uri.endswith("-code.tar.gz")
    assert "hello from job" in capsys.readouterr().out
    assert run.state == JobState.SUCCEEDED
    assert run.model_uri == "gs://bkt/runs/1/model"
    assert run.resource_name == f"projects/proj/locations/us-west1/jobs/{be.job_id}"
    build_job_config(s, be.job_id, be.code_uri)


def test_script_run_inside_image(script):
    be = Recorder()
    gcejob.ScriptRun("t", script=str(script), image="pytorch/pytorch:2.4",
                     requirements=["numpy"], backend=be).start(machine="g2-standard-4")
    s = be.spec
    assert s.image == "pytorch/pytorch:2.4" and s.command == "python train.py"
    assert "venv" not in s.setup and "python -m pip install -q numpy" in s.setup
    c = build_job_config(s, be.job_id, be.code_uri)["taskGroups"][0]["taskSpec"]["runnables"][-1]["container"]
    assert c["entrypoint"] == "/bin/bash"


def test_script_in_subfolder(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "run.py").write_text("")
    be = Recorder()
    gcejob.ScriptRun("t", script=str(tmp_path / "pkg" / "run.py"), source_dir=str(tmp_path),
                     backend=be).start()
    assert be.spec.command == "python3 pkg/run.py" and be.spec.code == str(tmp_path)


def test_bad_script_paths(tmp_path, script):
    with pytest.raises(FileNotFoundError):
        gcejob.ScriptRun("t", script=str(tmp_path / "nope.py"))
    with pytest.raises(ValueError, match="inside source_dir"):
        gcejob.ScriptRun("t", script=str(script), source_dir=str(tmp_path / "other"))


# ---- from an image ----------------------------------------------------------------------
def test_container_run(capsys):
    be = Recorder()
    run = gcejob.ContainerRun("eval", image="us-west1-docker.pkg.dev/proj/r/train:1",
                              command=["python", "-m", "train"], backend=be)
    run.start(machine="g2-standard-4", args=["--split", "val"], inputs={"data": "gs://bkt/ds/"},
              zones=["us-west1-a"], wait=False)
    s = be.spec
    assert s.image.endswith("train:1") and s.code is None
    assert s.command == "python -m train" and s.args == ["--split", "val"]
    assert s.inputs == {"data": "gs://bkt/ds/"} and s.zones == ["us-west1-a"]
    c = build_job_config(s, be.job_id)["taskGroups"][0]["taskSpec"]["runnables"][-1]["container"]
    assert c["entrypoint"] == "python" and c["commands"] == ["-m", "train", "--split", "val"]
    assert capsys.readouterr().out == ""  # wait=False: returns after submit, no logs


def test_container_run_default_entrypoint():
    be = Recorder()
    gcejob.ContainerRun("e", image="img", backend=be).start(wait=False)
    assert be.spec.command is None and be.spec.image == "img"


# ---- options ----------------------------------------------------------------------------
def test_unknown_option_is_an_error():
    with pytest.raises(TypeError, match="unexpected options"):
        gcejob.ContainerRun("x", image="i", backend=Recorder()).start(machin="x")


def test_n1_accelerator_and_bad_machine():
    be = Recorder()
    gcejob.ContainerRun("x", image="i", backend=be).start(
        machine="n1-standard-8", accelerator="NVIDIA_TESLA_T4", accelerator_count=2, wait=False)
    assert be.spec.accelerator_type == "nvidia-tesla-t4" and be.spec.accelerator_count == 2
    with pytest.raises(ValueError, match="can't take a GPU"):
        gcejob.ContainerRun("x", image="i", backend=be).start(
            machine="e2-standard-4", accelerator="NVIDIA_L4", wait=False)


def test_gpu_machine_ignores_accelerator():
    be = Recorder()
    gcejob.ContainerRun("x", image="i", backend=be).start(
        machine="g2-standard-4", accelerator="NVIDIA_L4", wait=False)
    assert be.spec.accelerator_type is None and be.spec.install_gpu_drivers


def test_provisioning_default_is_on_demand():
    be = Recorder()
    r = gcejob.ContainerRun("x", image="i", backend=be)
    r.start(wait=False)
    assert be.spec.provisioning == "standard"
    r.start(provisioning="FLEX_START", wait=False)
    assert be.spec.provisioning == "flex_start"
    r.start(provisioning="spot", wait=False)
    assert be.spec.provisioning == "spot"


def test_max_run_accepts_seconds_or_duration():
    be = Recorder()
    r = gcejob.ContainerRun("x", image="i", backend=be)
    r.start(max_run=3600, wait=False)
    assert be.spec.max_run_seconds == 3600
    r.start(max_run="10h", wait=False)
    assert be.spec.max_run_seconds == 36000


def test_attach_to_existing_job():
    run = gcejob.ScriptRun.attach("projects/proj/locations/us-west1/jobs/abc-123", backend=Recorder())
    assert run.job_id == "abc-123" and run.state == JobState.SUCCEEDED
    run2 = gcejob.ContainerRun.attach("abc-9", backend=Recorder())
    assert run2.job_id == "abc-9"


def test_methods_before_start_raise():
    with pytest.raises(RuntimeError, match="not been started"):
        _ = gcejob.ContainerRun("x", image="i", backend=Recorder()).state


def test_slug():
    assert runs._slug("My Job_v2") == "my-job-v2"
    assert runs._slug("123") == "job-123"
    assert runs._slug("!!!") == "job"
    assert len(runs._slug("a" * 100)) <= 42


def test_start_buckets_option_reaches_the_spec():
    be = Recorder()
    gcejob.ContainerRun("x", image="i", backend=be).start(
        buckets=["gs://data-bkt/datasets", "model-bkt"], wait=False)
    assert be.spec.buckets == ["data-bkt", "model-bkt"]
    cfg = build_job_config(be.spec, be.job_id)
    assert len(cfg["taskGroups"][0]["taskSpec"]["volumes"]) == 2


def test_spot_retries_by_default_and_on_demand_does_not():
    be = Recorder()
    r = gcejob.ContainerRun("x", image="i", backend=be)
    r.start(wait=False)
    assert be.spec.max_retries == 0                       # on-demand: never re-run
    r.start(provisioning="spot", wait=False)
    assert be.spec.max_retries == 3                       # spot: re-run after preemption
    r.start(provisioning="spot", max_retries=0, wait=False)
    assert be.spec.max_retries == 0
    r.start(provisioning="spot", max_retries=5, wait=False)
    assert be.spec.max_retries == 5
    with pytest.raises(ValidationError):
        r.start(max_retries=11, wait=False)               # capped at 10


def test_long_spot_wait_is_just_startup_timeout():
    be = Recorder()
    gcejob.ContainerRun("x", image="i", backend=be).start(
        provisioning="spot", startup_timeout="6h", wait=False)
    assert be.spec.startup_timeout_seconds == 6 * 3600
