"""Smoke tests for Helm-rendered worker CronJob manifests.

These assertions lock the production CronJob contracts (schedule, history limits,
deadlines, and hardening settings) so chart edits don't quietly change runtime
behavior.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _render_worker_cronjobs() -> list[dict]:
    helm_path = shutil.which("helm")
    if helm_path is None:
        msg = "helm must be installed to render worker cronjobs"
        raise RuntimeError(msg)

    result = subprocess.run(  # noqa: S603 - fixed helm binary path and static arguments
        [
            helm_path,
            "template",
            "copilot-backend",
            "charts/application",
            "-f",
            "charts/application/values.yaml",
            "-f",
            "charts/application/values-dev.yaml",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if isinstance(doc, dict) and doc.get("kind") == "CronJob"
    ]


def _render_all_manifests() -> list[dict]:
    helm_path = shutil.which("helm")
    if helm_path is None:
        msg = "helm must be installed to render worker cronjobs"
        raise RuntimeError(msg)

    result = subprocess.run(  # noqa: S603 - fixed helm binary path and static arguments
        [
            helm_path,
            "template",
            "copilot-backend",
            "charts/application",
            "-f",
            "charts/application/values.yaml",
            "-f",
            "charts/application/values-dev.yaml",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def test_integrations_cronjobs_render_with_production_job_hardening() -> None:
    """Assert integrations CronJob keeps strict production restart and deadline settings."""
    cronjobs = _render_worker_cronjobs()
    calendar = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-sync-calendar-events"
    )

    spec = calendar["spec"]
    job_spec = spec["jobTemplate"]["spec"]

    assert spec["timeZone"] == "Etc/UTC"
    assert spec["successfulJobsHistoryLimit"] == 1
    assert spec["failedJobsHistoryLimit"] == 2
    assert spec["startingDeadlineSeconds"] == 900
    assert job_spec["backoffLimit"] == 1
    assert job_spec["activeDeadlineSeconds"] == 300
    assert job_spec["ttlSecondsAfterFinished"] == 900


def test_hubspot_core_incremental_cronjob_renders_dispatch_contract() -> None:
    """Assert HubSpot incremental polling is deployed as a hardened integrations CronJob."""
    cronjobs = _render_worker_cronjobs()
    incremental = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-sync-hubspot-core-incremental"
    )

    container = incremental["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert incremental["spec"]["schedule"] == "*/10 * * * *"
    assert incremental["spec"]["concurrencyPolicy"] == "Forbid"
    assert container["command"] == [
        "python",
        "-m",
        "src.workers.integrations_worker.cron_dispatch",
        "--job",
        "sync-hubspot-core-incremental-all-orgs",
    ]


def test_hubspot_delete_reconciliation_cronjob_renders_dispatch_contract() -> None:
    """Assert slow HubSpot delete reconciliation is deployed as an integrations CronJob."""
    cronjobs = _render_worker_cronjobs()
    reconciliation = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-reconcile-hubspot-deletions"
    )

    container = reconciliation["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert reconciliation["spec"]["schedule"] == "17 */12 * * *"
    assert reconciliation["spec"]["concurrencyPolicy"] == "Forbid"
    assert container["command"] == [
        "python",
        "-m",
        "src.workers.integrations_worker.cron_dispatch",
        "--job",
        "reconcile-hubspot-deletions-all-orgs",
    ]


def test_deal_cronjobs_render_with_production_job_hardening() -> None:
    """Assert deal-mapping and attachment CronJobs still render hardened settings."""
    cronjobs = _render_worker_cronjobs()
    deal_mapping = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-deal-mapping"
    )

    spec = deal_mapping["spec"]
    job_spec = spec["jobTemplate"]["spec"]

    assert spec["timeZone"] == "Etc/UTC"
    assert spec["successfulJobsHistoryLimit"] == 1
    assert spec["failedJobsHistoryLimit"] == 2
    assert spec["startingDeadlineSeconds"] == 900
    assert job_spec["backoffLimit"] == 1
    assert job_spec["activeDeadlineSeconds"] == 300
    assert job_spec["ttlSecondsAfterFinished"] == 900

    email_attachments = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-email-attachment-extraction"
    )
    assert email_attachments["spec"]["schedule"] == "20,50 * * * *"


def test_knowledge_cronjobs_render_with_production_job_hardening() -> None:
    """Assert knowledge cleanup CronJobs keep strict history and TTL settings."""
    cronjobs = _render_worker_cronjobs()
    cleanup = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-cleanup-stale-knowledge-jobs"
    )

    spec = cleanup["spec"]
    job_spec = spec["jobTemplate"]["spec"]

    assert spec["timeZone"] == "Etc/UTC"
    assert spec["successfulJobsHistoryLimit"] == 1
    assert spec["failedJobsHistoryLimit"] == 2
    assert spec["startingDeadlineSeconds"] == 900
    assert job_spec["backoffLimit"] == 1
    assert job_spec["activeDeadlineSeconds"] == 300
    assert job_spec["ttlSecondsAfterFinished"] == 900
    assert {
        doc["metadata"]["name"]
        for doc in cronjobs
        if doc["metadata"]["name"].startswith("copilot-backend-cleanup-stale-")
    } == {
        "copilot-backend-cleanup-stale-knowledge-jobs",
        "copilot-backend-cleanup-stale-db-connections",
    }


def test_deal_and_knowledge_beat_deployments_are_disabled() -> None:
    """Protect against reintroducing deprecated deal/knowledge beat Deployments."""
    manifests = _render_all_manifests()
    deployment_names = {
        doc["metadata"]["name"] for doc in manifests if doc.get("kind") == "Deployment"
    }

    assert "copilot-backend-deal-beat" not in deployment_names
    assert "copilot-backend-knowledge-beat" not in deployment_names


def test_api_secret_renders_sentry_settings_for_dev() -> None:
    """Assert dev Sentry secret values render with expected non-empty defaults."""
    manifests = _render_all_manifests()
    secret = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Secret" and doc["metadata"]["name"] == "copilot-backend-secret"
    )

    assert (
        base64.b64decode(secret["data"]["SENTRY_DSN"]).decode()
        == "https://d47183944100e4f69572365fd1634d16@o4511017432186880.ingest.us.sentry.io/4511017435070464"
    )
    assert base64.b64decode(secret["data"]["SENTRY_TRACES_SAMPLE_RATE"]).decode() == "0.2"
