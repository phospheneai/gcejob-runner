"""`gcejob` command line."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import typer

from gcejob.backends import BatchBackend
from gcejob.backends.batch import build_job_config
from gcejob.cleanup import cleanup as run_cleanup
from gcejob.job import Job, JobError
from gcejob.spec import JobSpec, parse_duration

app = typer.Typer(no_args_is_help=True, help="Run training/evaluation jobs on ephemeral GCP VMs.")

ProjectOpt = typer.Option(None, "--project", envvar="GCEJOB_PROJECT", help="GCP project")
RegionOpt = typer.Option("us-west1", "--region", envvar="GCEJOB_REGION", help="GCP region")


def _backend(project: str | None, region: str) -> BatchBackend:
    if not project:
        raise typer.BadParameter("set --project or GCEJOB_PROJECT")
    return BatchBackend(project, region)


@app.command()
def validate(spec_file: Path, show_config: bool = typer.Option(False, "--show-config")):
    """Check a spec file; optionally print the Batch job it would create (no GCP calls)."""
    spec = JobSpec.from_yaml(spec_file)
    typer.echo(f"OK: {spec.name} -> {spec.machine} in {spec.region} ({spec.provisioning})")
    if show_config:
        code_uri = f"{spec.staging.rstrip('/')}/<job-id>-code.tar.gz" if spec.code else None
        typer.echo(json.dumps(build_job_config(spec, f"{spec.name}-<timestamp>", code_uri), indent=2))


@app.command()
def run(
    spec_file: Path,
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for the job and stream logs"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the Batch config and exit"),
):
    """Submit a job. By default waits, streams logs, and exits non-zero on failure."""
    spec = JobSpec.from_yaml(spec_file)
    job = Job(spec)
    if dry_run:
        code_uri = f"{spec.staging.rstrip('/')}/{job.job_id}-code.tar.gz" if spec.code else None
        typer.echo(json.dumps(build_job_config(spec, job.job_id, code_uri), indent=2))
        raise typer.Exit()

    job.submit()
    typer.echo(f"submitted {job.job_id} ({spec.machine}, {spec.region})")
    if not wait:
        typer.echo(f"follow with: gcejob logs {job.job_id} -f --project {spec.project} --region {spec.region}")
        typer.echo("tip: nothing cancels a job that never starts while you are away; schedule\n"
                   f"  gcejob cleanup --stuck --older-than 30m --yes --project {spec.project} --region {spec.region}")
        raise typer.Exit()
    try:
        job.wait_and_stream(emit=typer.echo,
                            on_update=lambda i: typer.echo(f"[{i.state.value}] {i.reason}".rstrip()))
    except JobError as err:
        typer.echo(f"ERROR: {err}", err=True)
        raise typer.Exit(1) from err
    typer.echo(f"done: {job.job_id}")


@app.command()
def status(job_id: str, project: str = ProjectOpt, region: str = RegionOpt):
    """Show a job's state and, if it's stuck or failed, why."""
    info = _backend(project, region).get(job_id)
    typer.echo(f"{info.job_id}: {info.state.value}")
    if info.reason:
        typer.echo(f"  {info.failure.value}: {info.reason}")


@app.command()
def logs(job_id: str, follow: bool = typer.Option(False, "-f", "--follow"),
         project: str = ProjectOpt, region: str = RegionOpt):
    """Print (or follow) a job's logs from Cloud Logging."""
    for line in _backend(project, region).logs(job_id, follow=follow):
        typer.echo(line)


@app.command()
def cancel(job_id: str, project: str = ProjectOpt, region: str = RegionOpt):
    """Cancel a running or queued job. Batch removes its VM and disk."""
    _backend(project, region).cancel(job_id)
    typer.echo(f"cancel requested: {job_id}")


@app.command("list")
def list_jobs(project: str = ProjectOpt, region: str = RegionOpt):
    """List gcejob jobs in a region."""
    for j in _backend(project, region).list():
        typer.echo(f"{j.job_id}\t{j.state.value}\t{j.reason}")


@app.command()
def cleanup(
    older_than: str = typer.Option("24h", help="Only jobs older than this (e.g. 6h, 2d)"),
    include_active: bool = typer.Option(False, help="Also remove queued/running jobs (cancels them)"),
    stuck: bool = typer.Option(
        False, "--stuck", help="Only jobs still QUEUED/STARTING after the age; never running jobs"
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually delete; default is a dry run"),
    project: str = ProjectOpt,
    region: str = RegionOpt,
):
    """Delete old gcejob job records; --stuck cancels jobs that never started (and their VMs)."""
    backend = _backend(project, region)
    stale = run_cleanup(backend, timedelta(seconds=parse_duration(older_than)),
                        include_active=include_active, dry_run=not yes, stuck_only=stuck)
    for j in stale:
        verb = "deleted" if yes else "would delete"
        typer.echo(f"{verb}: {j.job_id} ({j.state.value})")
    if not stale:
        typer.echo("nothing to clean")
    elif not yes:
        typer.echo("dry run; re-run with --yes to delete")


if __name__ == "__main__":
    app()
