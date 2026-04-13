from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _render_integrations_cronjobs() -> list[dict]:
    helm_path = shutil.which("helm")
    if helm_path is None:
        msg = "helm must be installed to render integrations cronjobs"
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


def test_integrations_cronjobs_render_with_production_job_hardening() -> None:
    cronjobs = _render_integrations_cronjobs()
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
