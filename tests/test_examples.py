import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gcejob.cli import app

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_hello_script_runs_locally():
    out = subprocess.run([sys.executable, str(EXAMPLES / "hello_cpu" / "job" / "hello.py")],
                         capture_output=True, text=True, check=True).stdout
    assert out.splitlines()[0] == "hi from gcejob"


def _load_runner():
    spec = importlib.util.spec_from_file_location("hello_run", EXAMPLES / "hello_cpu" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_refuses_to_run_with_placeholders(capsys):
    mod = _load_runner()
    assert mod.main() == 2
    assert "replace the your-* values" in capsys.readouterr().out


def test_runner_submits_when_filled_in(monkeypatch):
    """With real-looking values the runner configures gcejob and starts a ScriptRun (no GCP calls)."""
    mod = _load_runner()
    monkeypatch.setattr(mod, "PROJECT", "proj")
    monkeypatch.setattr(mod, "STAGING_BUCKET", "gs://bkt")
    monkeypatch.setattr(mod, "SERVICE_ACCOUNT", "sa@proj.iam.gserviceaccount.com")
    started = {}

    class FakeRun:
        state = type("S", (), {"value": "SUCCEEDED"})()

        def __init__(self, name, script):
            started.update(name=name, script=script)

        def start(self, **kw):
            started["options"] = kw

    monkeypatch.setattr(mod.gcejob, "ScriptRun", FakeRun)
    assert mod.main() == 0
    assert started["name"] == "hello-cpu" and started["script"].endswith("hello.py")
    assert started["options"]["machine"] == "e2-standard-2"


@pytest.mark.parametrize("name", ["cpu_smoke.yaml", "train.yaml"])
def test_yaml_examples_validate_and_have_only_placeholders(name):
    text = (EXAMPLES / name).read_text()
    assert "your-project-id" in text and "prj-" not in text
    r = CliRunner().invoke(app, ["validate", str(EXAMPLES / name)])
    assert r.exit_code == 0, r.output
