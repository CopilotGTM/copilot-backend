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


def _render_all_manifests(values_file: str = "charts/application/values-dev.yaml") -> list[dict]:
    helm_path = shutil.which("helm")
    if helm_path is None:
        msg = "helm must be installed to render worker manifests"
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
            values_file,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _render_worker_cronjobs(values_file: str = "charts/application/values-dev.yaml") -> list[dict]:
    return [doc for doc in _render_all_manifests(values_file) if doc.get("kind") == "CronJob"]


def _render_prod_worker_cronjobs() -> list[dict]:
    return _render_worker_cronjobs("charts/application/values-prod.yaml")


def test_integrations_cronjobs_render_with_production_job_hardening() -> None:
    """Assert integrations CronJob keeps strict production restart and deadline settings."""
    cronjobs = _render_prod_worker_cronjobs()
    deal_messages = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-sync-deal-messages-all-orgs"
    )

    spec = deal_messages["spec"]
    job_spec = spec["jobTemplate"]["spec"]

    assert spec["timeZone"] == "Etc/UTC"
    assert spec["successfulJobsHistoryLimit"] == 1
    assert spec["failedJobsHistoryLimit"] == 2
    assert spec["startingDeadlineSeconds"] == 900
    assert job_spec["backoffLimit"] == 1
    assert job_spec["activeDeadlineSeconds"] == 300
    assert job_spec["ttlSecondsAfterFinished"] == 900


def test_calendar_and_email_polling_cronjobs_are_not_rendered() -> None:
    """Outlook calendar/email sync is webhook-driven; scheduled polling stays disabled."""
    cronjobs = _render_worker_cronjobs()
    cronjob_names = {doc["metadata"]["name"] for doc in cronjobs}

    assert "copilot-backend-sync-calendar-events" not in cronjob_names
    assert "copilot-backend-sync-email-all-orgs" not in cronjob_names
    assert "copilot-backend-transcript-fetch-pipeline" not in cronjob_names
    assert "copilot-backend-timeline-sync" not in cronjob_names


def test_microsoft_graph_subscription_renewal_cronjob_renders_dispatch_contract() -> None:
    """Assert webhook renewal keeps running after Outlook polling CronJobs are removed."""
    cronjobs = _render_worker_cronjobs()
    renewal = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-renew-microsoft-graph-subscriptions"
    )

    container = renewal["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert renewal["spec"]["schedule"] == "0 */12 * * *"
    assert renewal["spec"]["concurrencyPolicy"] == "Forbid"
    assert container["command"] == [
        "python",
        "-m",
        "src.workers.integrations_worker.cron_dispatch",
        "--job",
        "renew-microsoft-graph-subscriptions",
    ]


def test_hubspot_core_incremental_cronjob_renders_dispatch_contract() -> None:
    """Assert HubSpot incremental polling is deployed as a hardened integrations CronJob."""
    cronjobs = _render_prod_worker_cronjobs()
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
    cronjobs = _render_prod_worker_cronjobs()
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
    cronjobs = _render_prod_worker_cronjobs()
    deal_mapping = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-deal-mapping"
    )

    spec = deal_mapping["spec"]
    job_spec = spec["jobTemplate"]["spec"]

    assert spec["schedule"] == "3 */2 * * *"
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

    meeting_bot_fallback = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-meeting-bot-fallback"
    )
    assert meeting_bot_fallback["spec"]["schedule"] == "0 20 * * *"


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


def test_chat_worker_scaledobject_watches_all_consumed_queues() -> None:
    """Scale chat workers on user messages, reminders, voice, and attachment work."""
    manifests = _render_all_manifests()
    scaled_object = next(
        doc
        for doc in manifests
        if doc.get("kind") == "ScaledObject"
        and doc["metadata"]["name"] == "copilot-backend-chat-worker-scaler"
    )

    trigger_queues = {
        trigger["metadata"]["listName"] for trigger in scaled_object["spec"]["triggers"]
    }

    assert trigger_queues == {
        "attachments",
        "chat_processing",
        "chat_messages",
        "voice_realtime",
    }


def test_dev_chat_worker_renders_memory_guardrails() -> None:
    """Assert dev chat worker uses the same Celery memory guardrails as prod."""
    manifests = _render_all_manifests()
    deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-chat-worker"
    )

    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert "--concurrency=1" in container["command"]
    assert "--max-tasks-per-child=25" in container["command"]
    assert "--max-memory-per-child=700000" in container["command"]


def test_worker_deployments_render_celery_events_and_health_probes() -> None:
    """Workers must be observable and restarted when Celery stops answering control traffic."""
    manifests = _render_all_manifests()
    deployments = [
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"].endswith("-worker")
        and "beat" not in doc["metadata"]["name"]
    ]

    assert {doc["metadata"]["name"] for doc in deployments} == {
        "copilot-backend-chat-worker",
        "copilot-backend-deal-worker",
        "copilot-backend-integrations-worker",
        "copilot-backend-knowledge-extract-worker",
        "copilot-backend-knowledge-upload-worker",
        "copilot-backend-organization-worker",
    }

    for deployment in deployments:
        name = deployment["metadata"]["name"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        command = container["command"]
        joined_liveness = " ".join(container["livenessProbe"]["exec"]["command"])
        joined_readiness = " ".join(container["readinessProbe"]["exec"]["command"])

        assert "-E" in command, name
        assert "celery -A" in joined_liveness, name
        assert "inspect ping" in joined_liveness, name
        assert "celery@$HOSTNAME" in joined_liveness, name
        assert "celery -A" in joined_readiness, name
        assert "inspect ping" in joined_readiness, name
        assert "celery@$HOSTNAME" in joined_readiness, name


def test_chat_worker_prestop_drains_all_consumed_queues() -> None:
    """Rolling restarts must stop every queue the multi-queue chat worker consumes."""
    manifests = _render_all_manifests()
    deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-chat-worker"
    )

    command = deployment["spec"]["template"]["spec"]["containers"][0]["lifecycle"]["preStop"][
        "exec"
    ]["command"][-1]

    for queue in {"attachments", "chat_processing", "chat_messages", "voice_realtime"}:
        assert f"control cancel_consumer {queue}" in command


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


def test_integrations_worker_values_consume_salesforce_streaming_queue() -> None:
    """Assert Salesforce CDC tasks are routed to a queue the integrations worker consumes."""
    values = yaml.safe_load(
        (REPO_ROOT / "charts" / "application" / "values.yaml").read_text(encoding="utf-8")
    )

    worker_config = values["integrations-worker"]["config"]

    assert worker_config["queueName"] == "integrations"
    assert "integrations_priority" in worker_config["additionalQueues"]
    assert "integrations_streaming" in worker_config["additionalQueues"]
