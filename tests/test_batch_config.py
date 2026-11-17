from gcejob.backends.batch import MOUNT_ROOT, build_job_config
from gcejob.spec import JobSpec


def spec(**kw):
    d = dict(name="j", project="p", command="python train.py")
    d.update(kw)
    return JobSpec(**d)


def task(cfg):
    return cfg["taskGroups"][0]["taskSpec"]


def policy(cfg):
    return cfg["allocationPolicy"]["instances"][0]["policy"]


def test_cpu_job_basics():
    cfg = build_job_config(spec(machine="e2-standard-4", max_run="5m"), "j-1")
    assert policy(cfg)["machineType"] == "e2-standard-4"
    assert policy(cfg)["provisioningModel"] == "STANDARD"
    assert cfg["allocationPolicy"]["instances"][0]["installGpuDrivers"] is False
    assert task(cfg)["maxRunDuration"] == "300s"
    assert task(cfg)["maxRetryCount"] == 0
    assert "volumes" not in task(cfg)
    assert task(cfg)["runnables"][0]["script"]["text"].endswith("python train.py")
    assert cfg["logsPolicy"]["destination"] == "CLOUD_LOGGING"


def test_gpu_job_installs_drivers():
    cfg = build_job_config(spec(machine="g2-standard-4"), "j-1")
    assert cfg["allocationPolicy"]["instances"][0]["installGpuDrivers"] is True


def test_never_spot_unless_asked():
    assert policy(build_job_config(spec(), "j"))["provisioningModel"] == "STANDARD"
    assert policy(build_job_config(spec(provisioning="spot"), "j"))["provisioningModel"] == "SPOT"
    assert policy(build_job_config(spec(provisioning="flex_start"), "j"))["provisioningModel"] == "FLEX_START"


def test_service_account_zones_labels_network():
    cfg = build_job_config(
        spec(service_account="sa@p.iam.gserviceaccount.com", zones=["us-west1-a", "us-west1-c"],
             labels={"team": "r"}, no_external_ip=True, subnetwork="sub"), "j")
    ap = cfg["allocationPolicy"]
    assert ap["serviceAccount"]["email"] == "sa@p.iam.gserviceaccount.com"
    assert ap["location"]["allowedLocations"] == ["zones/us-west1-a", "zones/us-west1-c"]
    assert ap["labels"]["gcejob"] == "true" and ap["labels"]["team"] == "r"
    assert ap["network"]["networkInterfaces"][0]["noExternalIpAddress"] is True


def test_env_includes_contract_vars_and_user_env():
    cfg = build_job_config(spec(env={"LR": "0.1"}), "j-9")
    env = task(cfg)["environment"]["variables"]
    assert env["LR"] == "0.1" and env["GCEJOB_JOB_ID"] == "j-9"
    assert env["GCEJOB_OUTPUT_DIR"] == "/job/output"


def test_data_contract_mounts_and_links():
    cfg = build_job_config(spec(
        inputs={"data": "gs://raw/ds/v1/"},
        output="gs://out/runs/r1/",
        checkpoints="gs://out/runs/r1/ckpt/"), "j")
    vols = task(cfg)["volumes"]
    assert {v["gcs"]["remotePath"] for v in vols} == {"raw", "out"}  # one mount per bucket
    assert all(v["mountPath"].startswith(MOUNT_ROOT) for v in vols)
    script = task(cfg)["runnables"][0]["script"]["text"]
    assert f"ln -sfn {MOUNT_ROOT}/raw/ds/v1 /job/input/data" in script
    assert f"ln -sfn {MOUNT_ROOT}/out/runs/r1 /job/output" in script
    assert f"ln -sfn {MOUNT_ROOT}/out/runs/r1/ckpt /job/checkpoints" in script
    assert script.rstrip().endswith("python train.py")


def test_code_and_setup_run_in_order():
    cfg = build_job_config(spec(code="/tmp/x", staging="gs://stg/c", setup="pip install -r r.txt"),
                           "j", code_uri="gs://stg/c/j-code.tar.gz")
    script = task(cfg)["runnables"][0]["script"]["text"]
    assert script.index("tar -xzf") < script.index("pip install") < script.index("python train.py")
    assert "cd /job/code" in script


def test_container_job():
    cfg = build_job_config(spec(image="us-docker.pkg.dev/p/r/img:1", command="python t.py",
                                machine="g2-standard-4"), "j")
    rn = task(cfg)["runnables"][-1]["container"]
    assert rn["imageUri"].endswith("img:1")
    assert rn["entrypoint"] == "python"          # `command` replaces the ENTRYPOINT (entrypoint override)
    assert rn["commands"] == ["t.py"]
    assert rn["options"] == "--privileged"
    assert any("nvidia" in v for v in rn["volumes"])


def test_container_args_only_keeps_image_entrypoint():
    cfg = build_job_config(spec(image="img", command=None, args=["--epochs", "5"]), "j")
    rn = task(cfg)["runnables"][-1]["container"]
    assert "entrypoint" not in rn and rn["commands"] == ["--epochs", "5"]


def test_container_with_command_and_args():
    cfg = build_job_config(spec(image="img", command="python train.py", args=["--lr", "0.1"]), "j")
    rn = task(cfg)["runnables"][-1]["container"]
    assert rn["entrypoint"] == "python" and rn["commands"] == ["train.py", "--lr", "0.1"]


def test_container_with_code_runs_through_bash_in_code_dir():
    cfg = build_job_config(
        spec(image="img", code="/x", staging="gs://stg/c", setup="pip install -q torch",
             command="python train.py", args=["--e", "5"]), "j", code_uri="gs://stg/c/j.tar.gz")
    rn = task(cfg)["runnables"][-1]["container"]
    assert rn["entrypoint"] == "/bin/bash" and rn["commands"][0] == "-c"
    body = rn["commands"][1]
    assert body.index("cd /job/code") < body.index("pip install") < body.index("python train.py --e 5")
    assert any(v.startswith("/job:") for v in rn["volumes"])
    # the code archive is unpacked on the host before the container starts
    assert "tar -xzf" in task(cfg)["runnables"][0]["script"]["text"]


def test_script_mode_appends_quoted_args():
    cfg = build_job_config(spec(command="python train.py", args=["--name", "a b"]), "j")
    assert task(cfg)["runnables"][0]["script"]["text"].endswith("python train.py --name 'a b'")


def test_n1_accelerator():
    cfg = build_job_config(spec(machine="n1-standard-8", accelerator_type="nvidia-tesla-t4"), "j")
    assert policy(cfg)["accelerators"] == [{"type": "nvidia-tesla-t4", "count": "1"}]
    assert cfg["allocationPolicy"]["instances"][0]["installGpuDrivers"] is True


def test_config_parses_as_batch_job():
    """The dict must be loadable into the real Batch protobuf (catches typo'd field names)."""
    import json

    from google.cloud import batch_v1

    cfg = build_job_config(spec(
        machine="g2-standard-4", zones=["us-west1-a"], service_account="sa@p.iam.gserviceaccount.com",
        inputs={"d": "gs://b/x"}, output="gs://b/o", env={"A": "1"}), "j")
    job = batch_v1.Job.from_json(json.dumps(cfg))
    assert job.allocation_policy.instances[0].policy.machine_type == "g2-standard-4"
    assert job.task_groups[0].task_spec.volumes[0].gcs.remote_path == "b"


def test_buckets_mount_at_gcs_without_other_gcs_paths():
    cfg = build_job_config(spec(buckets=["data-bkt", "model-bkt"]), "j")
    vols = task(cfg)["volumes"]
    assert {v["gcs"]["remotePath"] for v in vols} == {"data-bkt", "model-bkt"}
    assert {v["mountPath"] for v in vols} == {f"{MOUNT_ROOT}/data-bkt", f"{MOUNT_ROOT}/model-bkt"}
    script = task(cfg)["runnables"][0]["script"]["text"]
    assert f"ln -sfn {MOUNT_ROOT} /gcs" in script       # /gcs/<bucket> works in the script


def test_buckets_deduplicate_with_inputs_and_output():
    cfg = build_job_config(spec(buckets=["data-bkt"], inputs={"d": "gs://data-bkt/x/"},
                                output="gs://data-bkt/out/"), "j")
    assert len(task(cfg)["volumes"]) == 1               # one mount per bucket


def test_container_sees_buckets_under_gcs():
    cfg = build_job_config(spec(image="img", command="python t.py", buckets=["data-bkt"]), "j")
    rn = task(cfg)["runnables"]
    assert "ln -sfn" in rn[0]["script"]["text"]          # host layout runs before the container
    assert f"{MOUNT_ROOT}:/gcs" in rn[-1]["container"]["volumes"]


def test_no_mounts_when_no_buckets_needed():
    cfg = build_job_config(spec(), "j")
    assert "volumes" not in task(cfg) and "/gcs" not in task(cfg)["runnables"][0]["script"]["text"]


def test_retries_only_on_preemption():
    t = task(build_job_config(spec(max_retries=3), "j"))
    assert t["maxRetryCount"] == 3
    assert t["lifecyclePolicies"] == [
        {"action": "RETRY_TASK", "actionCondition": {"exitCodes": [50001]}}]


def test_no_retries_by_default():
    t = task(build_job_config(spec(), "j"))
    assert t["maxRetryCount"] == 0 and "lifecyclePolicies" not in t


def test_scripts_start_with_bash_shebang_because_batch_runs_sh():
    for kw in ({}, {"buckets": ["b-bucket"]}):
        text = task(build_job_config(spec(**kw), "j"))["runnables"][0]["script"]["text"]
        assert text.startswith("#!/bin/bash\nset -euo pipefail")


def test_list_filter_quotes_the_label_value():
    from unittest.mock import MagicMock

    from gcejob.backends.batch import BatchBackend

    client = MagicMock()
    client.list_jobs.return_value = []
    BatchBackend("p", "us-west1", client=client).list()
    assert client.list_jobs.call_args.kwargs["request"]["filter"] == 'labels.gcejob="true"'
