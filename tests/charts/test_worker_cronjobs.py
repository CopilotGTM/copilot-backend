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


def test_deal_cronjobs_render_with_production_job_hardening() -> None:
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


def test_knowledge_cronjobs_render_with_production_job_hardening() -> None:
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
    manifests = _render_all_manifests()
    deployment_names = {
        doc["metadata"]["name"] for doc in manifests if doc.get("kind") == "Deployment"
    }

    assert "copilot-backend-deal-beat" not in deployment_names
    assert "copilot-backend-knowledge-beat" not in deployment_names


def test_api_secret_renders_sentry_settings_for_dev() -> None:
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
