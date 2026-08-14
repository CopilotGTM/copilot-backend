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
from src.workers.deal_worker.scheduled_jobs import SCHEDULED_JOBS as DEAL_SCHEDULED_JOBS
from src.workers.integrations_worker.scheduled_jobs import (
    SCHEDULED_JOBS as INTEGRATIONS_SCHEDULED_JOBS,
)
from src.workers.knowledge_worker.scheduled_jobs import (
    SCHEDULED_JOBS as KNOWLEDGE_SCHEDULED_JOBS,
)

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


def test_prod_renders_one_fixed_salesforce_cdc_worker_without_scaler() -> None:
    manifests = _render_all_manifests("charts/application/values-prod.yaml")
    values = yaml.safe_load(
        (REPO_ROOT / "charts" / "application" / "values.yaml").read_text(encoding="utf-8")
    )
    name = "copilot-backend-salesforce-cdc-worker"
    deployments = [
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name
    ]

    assert len(deployments) == 1
    deployment = deployments[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {
        "maxSurge": 0,
        "maxUnavailable": 1,
    }
    assert container["command"] == [
        "python",
        "-m",
        "src.workers.salesforce_cdc_worker.main",
    ]
    assert container["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "1000m", "memory": "2Gi"},
    }
    assert "celery" not in " ".join(container["command"])
    assert "SALESFORCE_CDC_MAX_INTEGRATIONS" in {entry["name"] for entry in container["env"]}
    assert container["livenessProbe"]["exec"]["command"][-1] == "--live"
    assert container["readinessProbe"]["exec"]["command"][-1] == "--ready"
    assert "lifecycle" not in container
    assert values["salesforce-cdc-worker"]["autoscaling"]["enabled"] is False
    assert not any(
        doc.get("kind") in {"HorizontalPodAutoscaler", "ScaledObject"}
        and name in doc["metadata"]["name"]
        for doc in manifests
    )


def test_dev_disables_salesforce_cdc_worker_with_small_memory_budget() -> None:
    manifests = _render_all_manifests("charts/application/values-dev.yaml")
    deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-salesforce-cdc-worker"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert deployment["spec"]["replicas"] == 0
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }


def test_prod_startup_hook_does_not_require_priority_class_before_upgrade() -> None:
    """Keep startup hooks independent from resources created by the same Helm upgrade."""
    manifests = _render_all_manifests("charts/application/values-prod.yaml")
    startup_job = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Job" and doc["metadata"]["name"] == "copilot-backend-startup"
    )

    assert "pre-upgrade" in startup_job["metadata"]["annotations"]["helm.sh/hook"]
    assert "priorityClassName" not in startup_job["spec"]["template"]["spec"]


def test_chart_cronjob_names_match_worker_schedule_registries() -> None:
    """Every Helm CronJob name must resolve in the worker cron dispatcher."""
    values = yaml.safe_load(
        (REPO_ROOT / "charts" / "application" / "values.yaml").read_text(encoding="utf-8")
    )

    assert {job["name"] for job in values["knowledgeCronJobs"]["jobs"]} <= set(
        KNOWLEDGE_SCHEDULED_JOBS
    )
    assert {job["name"] for job in values["integrationsCronJobs"]["jobs"]} <= set(
        INTEGRATIONS_SCHEDULED_JOBS
    )
    assert {job["name"] for job in values["dealCronJobs"]["jobs"]} <= set(DEAL_SCHEDULED_JOBS)


def test_allowed_prod_integrations_cronjob_renders_with_hardening() -> None:
    """Assert remaining integrations CronJob keeps strict production hardening."""
    cronjobs = _render_prod_worker_cronjobs()
    token_refresh = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-refresh-tokens"
    )

    spec = token_refresh["spec"]
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


def test_prod_integration_sync_polling_cronjobs_are_not_rendered() -> None:
    """Prod uses the same reduced integrations CronJob set as dev."""
    cronjobs = _render_prod_worker_cronjobs()
    cronjob_names = {doc["metadata"]["name"] for doc in cronjobs}

    assert "copilot-backend-sync-messaging-channels-all-orgs" not in cronjob_names
    assert "copilot-backend-sync-deal-messages-all-orgs" not in cronjob_names
    assert "copilot-backend-sync-hubspot-core-incremental" not in cronjob_names
    assert "copilot-backend-reconcile-hubspot-deletions" not in cronjob_names


def test_deal_cronjobs_render_with_production_job_hardening() -> None:
    """Assert remaining prod deal CronJobs still render hardened settings."""
    cronjobs = _render_prod_worker_cronjobs()
    cronjob_names = {doc["metadata"]["name"] for doc in cronjobs}

    assert "copilot-backend-deal-memory-backfill" not in cronjob_names
    assert "copilot-backend-email-attachment-extraction" not in cronjob_names

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

    meeting_bot_fallback = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-meeting-bot-fallback"
    )
    assert meeting_bot_fallback["spec"]["schedule"] == "0 20 * * *"

    sales_dashboard_snapshot = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-sales-dashboard-snapshot"
    )
    sales_dashboard_container = sales_dashboard_snapshot["spec"]["jobTemplate"]["spec"]["template"][
        "spec"
    ]["containers"][0]

    assert sales_dashboard_snapshot["spec"]["schedule"] == "15 1 * * *"
    assert sales_dashboard_container["command"] == [
        "python",
        "-m",
        "src.workers.deal_worker.cron_dispatch",
        "--job",
        "sales-dashboard-snapshot",
    ]


def test_sales_dashboard_snapshot_cronjob_renders_in_dev() -> None:
    """Dev overrides replace the base job list, so keep the snapshot CronJob explicit."""
    cronjobs = _render_worker_cronjobs()
    sales_dashboard_snapshot = next(
        doc
        for doc in cronjobs
        if doc["metadata"]["name"] == "copilot-backend-sales-dashboard-snapshot"
    )

    assert sales_dashboard_snapshot["spec"]["schedule"] == "15 1 * * *"


def test_deal_mapping_cronjob_renders_every_two_hours_in_dev() -> None:
    """Keep scheduled stakeholder backfill enabled when dev replaces the base job list."""
    cronjobs = _render_worker_cronjobs()
    deal_mapping = next(
        doc for doc in cronjobs if doc["metadata"]["name"] == "copilot-backend-deal-mapping"
    )

    assert deal_mapping["spec"]["schedule"] == "3 */2 * * *"


def test_stale_cleanup_cronjob_boundaries() -> None:
    """Dev disables stale cleanup CronJobs; prod keeps app-state cleanup jobs."""
    disabled_cleanup_cronjobs = {
        "copilot-backend-cleanup-stale-knowledge-jobs",
        "copilot-backend-stale-deal-job-cleanup",
    }

    dev_cronjob_names = {doc["metadata"]["name"] for doc in _render_worker_cronjobs()}
    prod_cronjobs = _render_prod_worker_cronjobs()

    assert disabled_cleanup_cronjobs.isdisjoint(dev_cronjob_names)
    knowledge_cleanup = next(
        doc
        for doc in prod_cronjobs
        if doc["metadata"]["name"] == "copilot-backend-cleanup-stale-knowledge-jobs"
    )
    assert knowledge_cleanup["spec"]["schedule"] == "0 */6 * * *"

    deal_cleanup = next(
        doc
        for doc in prod_cronjobs
        if doc["metadata"]["name"] == "copilot-backend-stale-deal-job-cleanup"
    )
    assert deal_cleanup["spec"]["schedule"] == "45 * * * *"


def test_deal_and_knowledge_beat_deployments_are_disabled() -> None:
    """Protect against reintroducing deprecated deal/knowledge beat Deployments."""
    manifests = _render_all_manifests()
    deployment_names = {
        doc["metadata"]["name"] for doc in manifests if doc.get("kind") == "Deployment"
    }

    assert "copilot-backend-deal-beat" not in deployment_names
    assert "copilot-backend-knowledge-beat" not in deployment_names


def test_threaded_workers_can_drain_their_longest_cooperative_deadline() -> None:
    manifests = _render_all_manifests("charts/application/values-prod.yaml")
    deployments = {
        doc["metadata"]["name"]: doc for doc in manifests if doc.get("kind") == "Deployment"
    }

    assert (
        deployments["copilot-backend-integrations-worker"]["spec"]["template"]["spec"][
            "terminationGracePeriodSeconds"
        ]
        >= 7270
    )
    assert (
        deployments["copilot-backend-deal-worker"]["spec"]["template"]["spec"][
            "terminationGracePeriodSeconds"
        ]
        >= 3310
    )
    assert (
        deployments["copilot-backend-deal-insights-worker"]["spec"]["template"]["spec"][
            "terminationGracePeriodSeconds"
        ]
        >= 670
    )


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


def test_chat_worker_renders_ten_async_compatible_slots() -> None:
    """The complete chat worker must overlap I/O without ten prefork children."""
    base_values = yaml.safe_load((REPO_ROOT / "charts/application/values.yaml").read_text())
    assert base_values["chat-worker"]["config"]["concurrency"] == 10
    assert base_values["chat-worker"]["config"]["pool"] == "threads"

    for values_file in (
        "charts/application/values-dev.yaml",
        "charts/application/values-prod.yaml",
    ):
        manifests = _render_all_manifests(values_file)
        deployment = next(
            doc
            for doc in manifests
            if doc.get("kind") == "Deployment"
            and doc["metadata"]["name"] == "copilot-backend-chat-worker"
        )

        container = deployment["spec"]["template"]["spec"]["containers"][0]

        assert "--concurrency=10" in container["command"]
        assert "--pool=threads" in container["command"]
        assert not any("per-child" in argument for argument in container["command"])


def test_prod_integrations_and_deal_workers_render_async_slots() -> None:
    """I/O workers must use bounded threads with production-sized resources."""
    manifests = _render_all_manifests("charts/application/values-prod.yaml")
    deal_deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-deal-worker"
    )
    insights_deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-deal-insights-worker"
    )
    commands = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["containers"][0]["command"]
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"]
        in {
            "copilot-backend-integrations-worker",
            "copilot-backend-deal-worker",
            "copilot-backend-deal-insights-worker",
        }
    }

    integrations_command = commands["copilot-backend-integrations-worker"]
    integrations_deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-integrations-worker"
    )
    assert "--concurrency=4" in integrations_command
    assert "--pool=threads" in integrations_command
    assert not any("per-child" in argument for argument in integrations_command)
    assert integrations_deployment["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "250m", "memory": "1Gi"},
        "limits": {"cpu": "1000m", "memory": "2Gi"},
    }
    assert "--concurrency=4" in commands["copilot-backend-deal-worker"]
    assert "--pool=threads" in commands["copilot-backend-deal-worker"]
    assert "--concurrency=2" in commands["copilot-backend-deal-insights-worker"]
    assert "--pool=threads" in commands["copilot-backend-deal-insights-worker"]
    assert deal_deployment["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "1500m", "memory": "2Gi"},
    }
    assert insights_deployment["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "1500m", "memory": "2Gi"},
    }


def test_prod_api_renders_right_sized_cpu_request_with_burst_capacity() -> None:
    manifests = _render_all_manifests("charts/application/values-prod.yaml")
    deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "copilot-backend"
    )

    resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["requests"] == {"cpu": "250m", "memory": "1Gi"}
    assert resources["limits"] == {"cpu": "1500m", "memory": "6Gi"}


def test_dev_deal_workers_render_measured_memory_requests() -> None:
    """Both dev queues fit their observed workload without reserving a full GiB each."""
    manifests = _render_all_manifests("charts/application/values-dev.yaml")
    resources = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["containers"][0]["resources"]
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"]
        in {
            "copilot-backend-deal-worker",
            "copilot-backend-deal-insights-worker",
        }
    }

    assert resources["copilot-backend-deal-worker"]["requests"] == {
        "cpu": "25m",
        "memory": "700Mi",
    }
    assert resources["copilot-backend-deal-insights-worker"]["requests"] == {
        "cpu": "25m",
        "memory": "700Mi",
    }


def test_dev_integrations_worker_renders_measured_resources() -> None:
    """Dev scheduling reflects observed CPU while retaining memory burst headroom."""
    manifests = _render_all_manifests("charts/application/values-dev.yaml")
    deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-integrations-worker"
    )

    assert deployment["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "100m", "memory": "640Mi"},
        "limits": {"cpu": "2000m", "memory": "2Gi"},
    }


def test_prod_keeps_two_interactive_deal_workers_warm() -> None:
    """Two warm pods each expose four I/O slots for interactive hydration."""
    manifests = _render_all_manifests("charts/application/values-prod.yaml")
    scaler = next(
        doc
        for doc in manifests
        if doc.get("kind") == "ScaledObject"
        and doc["metadata"]["name"] == "copilot-backend-deal-worker-scaler"
    )
    deployment = next(
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "copilot-backend-deal-worker"
    )

    assert scaler["spec"]["minReplicaCount"] == 2
    assert scaler["spec"]["maxReplicaCount"] == 2
    command = deployment["spec"]["template"]["spec"]["containers"][0]["command"]
    assert "--concurrency=4" in command
    assert "--pool=threads" in command


def test_deal_and_insight_queues_have_separate_workers() -> None:
    """Slow LLM jobs must not block stakeholder and communication hydration."""
    for values_file in (
        "charts/application/values-dev.yaml",
        "charts/application/values-prod.yaml",
    ):
        manifests = _render_all_manifests(values_file)
        commands = {
            doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["containers"][0]["command"]
            for doc in manifests
            if doc.get("kind") == "Deployment"
            and doc["metadata"]["name"].startswith("copilot-backend-deal")
        }

        assert commands["copilot-backend-deal-worker"][-2:] == ["-Q", "deal"]
        assert commands["copilot-backend-deal-insights-worker"][-2:] == [
            "-Q",
            "deal_jobs",
        ]


def test_worker_deployments_render_celery_events_and_health_probes() -> None:
    """Only chat liveness is local while every worker retains broker readiness."""
    manifests = _render_all_manifests()
    deployments = [
        doc
        for doc in manifests
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"].endswith("-worker")
        and "beat" not in doc["metadata"]["name"]
        and doc["spec"]["template"]["spec"]["containers"][0]["command"][0] == "celery"
    ]

    assert {doc["metadata"]["name"] for doc in deployments} == {
        "copilot-backend-chat-worker",
        "copilot-backend-deal-insights-worker",
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
        assert container["livenessProbe"]["initialDelaySeconds"] >= 60, name
        assert container["livenessProbe"]["failureThreshold"] >= 4, name
        assert "celery -A" in joined_readiness, name
        assert "inspect ping" in joined_readiness, name
        assert "celery@$HOSTNAME" in joined_readiness, name
        assert container["readinessProbe"]["initialDelaySeconds"] >= 30, name
        assert container["readinessProbe"]["timeoutSeconds"] >= 20, name
        assert container["readinessProbe"]["failureThreshold"] >= 3, name

        if name in {
            "copilot-backend-chat-worker",
            "copilot-backend-deal-insights-worker",
            "copilot-backend-deal-worker",
            "copilot-backend-integrations-worker",
        }:
            heartbeat_name = (
                "celery-worker-heartbeat"
                if name == "copilot-backend-chat-worker"
                else "worker-asyncio-loop-heartbeat"
            )
            assert heartbeat_name in joined_liveness
            assert "inspect ping" not in joined_liveness
            assert container["livenessProbe"]["timeoutSeconds"] == 5
            assert "src.workers.base.healthcheck_app:app" in joined_readiness
        else:
            assert "celery -A" in joined_liveness, name
            assert "inspect ping" in joined_liveness, name
            assert "celery@$HOSTNAME" in joined_liveness, name
            assert container["livenessProbe"]["timeoutSeconds"] >= 20, name
            assert "src.workers.base.healthcheck_app:app" not in joined_readiness, name


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


def test_does_not_add_a_second_salesforce_cdc_worker() -> None:
    """The existing shared CDC worker remains the only CDC deployment."""
    values = yaml.safe_load(
        (REPO_ROOT / "charts" / "application" / "values.yaml").read_text(encoding="utf-8")
    )

    worker_config = values["integrations-worker"]["config"]

    assert worker_config["queueName"] == "integrations"
    assert "integrations_priority" in worker_config["additionalQueues"]
    assert "integrations_streaming" in worker_config["additionalQueues"]
    assert "salesforce-cdc-legacy-worker" not in values
