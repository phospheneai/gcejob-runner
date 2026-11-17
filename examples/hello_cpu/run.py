"""Run job/hello.py on a small CPU VM. Edit the variables, then: python examples/hello_cpu/run.py"""

import logging
import sys
from pathlib import Path

import gcejob

# ---- edit these ----
PROJECT = "your-project-id"
REGION = "us-west1"
STAGING_BUCKET = "gs://your-bucket"
SERVICE_ACCOUNT = "sa-gcejob-runner@your-project-id.iam.gserviceaccount.com"

SCRIPT = Path(__file__).parent / "job" / "hello.py"


def main() -> int:
    if any("your-" in v for v in (PROJECT, STAGING_BUCKET, SERVICE_ACCOUNT)):
        print("Edit run.py first: replace the your-* values.")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    gcejob.configure(project=PROJECT, region=REGION, staging_bucket=STAGING_BUCKET,
                     service_account=SERVICE_ACCOUNT)

    run = gcejob.ScriptRun("hello-cpu", script=str(SCRIPT))
    run.start(machine="e2-standard-2", max_run="5m")
    print(f"final state: {run.state.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
