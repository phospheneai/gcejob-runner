import json
import re
from pathlib import Path

from typer.testing import CliRunner

from gcejob.cli import app

EXAMPLES = Path(__file__).parent.parent / "examples"
runner = CliRunner()


def test_validate_example():
    r = runner.invoke(app, ["validate", str(EXAMPLES / "cpu_smoke.yaml")])
    assert r.exit_code == 0 and "cpu-smoke" in r.output


def test_validate_show_config_is_json():
    r = runner.invoke(app, ["validate", str(EXAMPLES / "cpu_smoke.yaml"), "--show-config"])
    assert r.exit_code == 0
    cfg = json.loads(r.output.split("\n", 1)[1])
    assert cfg["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"] == "STANDARD"


def test_run_dry_run_makes_no_gcp_calls():
    r = runner.invoke(app, ["run", str(EXAMPLES / "cpu_smoke.yaml"), "--dry-run"])
    assert r.exit_code == 0
    assert json.loads(r.output)["logsPolicy"]["destination"] == "CLOUD_LOGGING"


def test_invalid_spec_fails(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("name: Bad_Name\nproject: p\ncommand: x\n")
    r = runner.invoke(app, ["validate", str(f)])
    assert r.exit_code != 0


def test_status_requires_project():
    r = runner.invoke(app, ["status", "job-1"], env={"GCEJOB_PROJECT": ""})
    assert r.exit_code != 0


def test_cleanup_has_stuck_flag():
    r = runner.invoke(app, ["cleanup", "--help"])
    # CI forces colour, which splits "--stuck" with ANSI codes; strip them.
    assert "--stuck" in re.sub(r"\x1b\[[0-9;]*m", "", r.output)
