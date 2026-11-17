import pytest
from pydantic import ValidationError

from gcejob.spec import JobSpec, parse_duration, split_gcs_uri


def base(**kw):
    d = dict(name="job-1", project="p", command="echo hi")
    d.update(kw)
    return d


def test_durations():
    assert parse_duration("90s") == 90
    assert parse_duration("15m") == 900
    assert parse_duration("10h") == 36000
    assert parse_duration("2d") == 172800
    assert parse_duration(60) == 60
    with pytest.raises(ValueError):
        parse_duration("soon")
    with pytest.raises(ValueError):
        parse_duration("0s")


def test_gcs_uri_split():
    assert split_gcs_uri("gs://b/a/b/") == ("b", "a/b")
    assert split_gcs_uri("gs://b") == ("b", "")
    with pytest.raises(ValueError):
        split_gcs_uri("s3://b/x")


def test_minimal_spec_defaults():
    s = JobSpec(**base())
    assert s.region == "us-west1" and s.provisioning == "standard"
    assert s.max_run_seconds == 3600 and s.startup_timeout_seconds == 900
    assert not s.install_gpu_drivers


@pytest.mark.parametrize("machine,expected", [
    ("e2-standard-4", False), ("g2-standard-4", True), ("g4-standard-6", True),
    ("a3-highgpu-8g", True), ("n2-standard-8", False),
])
def test_gpu_driver_autodetect(machine, expected):
    assert JobSpec(**base(machine=machine)).install_gpu_drivers is expected


def test_gpu_driver_override():
    assert JobSpec(**base(machine="g2-standard-4", gpu_drivers=False)).install_gpu_drivers is False


@pytest.mark.parametrize("bad", ["Job", "1job", "job_1", "a" * 50, ""])
def test_bad_names(bad):
    with pytest.raises(ValidationError):
        JobSpec(**base(name=bad))


def test_needs_command_or_image():
    with pytest.raises(ValidationError):
        JobSpec(name="x", project="p")
    JobSpec(name="x", project="p", image="img")


def test_code_requires_staging():
    with pytest.raises(ValidationError):
        JobSpec(**base(code="./c"))


def test_code_and_image_can_combine():
    JobSpec(**base(code="./c", staging="gs://b/s", image="img"))


def test_zone_must_match_region():
    with pytest.raises(ValidationError):
        JobSpec(**base(zones=["us-central1-a"]))
    JobSpec(**base(zones=["us-west1-a"]))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        JobSpec(**base(machin="x"))


def test_bad_provisioning():
    with pytest.raises(ValidationError):
        JobSpec(**base(provisioning="cheap"))


def test_inputs_must_be_gcs():
    with pytest.raises(ValidationError):
        JobSpec(**base(inputs={"data": "/local/path"}))


def test_list_command_is_quoted():
    s = JobSpec(**base(command=["python", "train.py", "--name", "a b"]))
    assert s.command_text == "python train.py --name 'a b'"


def test_yaml_roundtrip_and_relative_code(tmp_path):
    (tmp_path / "proj").mkdir()
    f = tmp_path / "job.yaml"
    f.write_text("name: j\nproject: p\ncommand: ls\ncode: ./proj\nstaging: gs://b/s\n")
    s = JobSpec.from_yaml(f)
    assert s.code == str((tmp_path / "proj").resolve())


def test_buckets_accept_names_and_uris_and_dedupe():
    s = JobSpec(**base(buckets=["my-bkt", "gs://my-bkt/some/path", "other_bkt", "/third-bkt/"]))
    assert s.buckets == ["my-bkt", "other_bkt", "third-bkt"]


@pytest.mark.parametrize("bad", ["UPPER", "a", "has space", "-lead"])
def test_bad_bucket_names(bad):
    with pytest.raises(ValidationError):
        JobSpec(**base(buckets=[bad]))


def test_disk_must_fit_the_default_image():
    with pytest.raises(ValidationError):
        JobSpec(**base(disk_gb=20))
    assert JobSpec(**base(disk_gb=30)).disk_gb == 30
