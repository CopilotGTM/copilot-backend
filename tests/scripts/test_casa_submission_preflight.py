from pathlib import Path

from scripts.security import check_casa_submission_preflight as preflight


def _write(path: Path, text: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_ready_tree(root: Path) -> None:
    readiness = root / "evidence" / "runs" / "20260705-134328"
    _write(
        readiness / "99-summary.txt",
        "\n".join(
            [
                "0\t01-ruff-security.txt\tRuff security/lint rules",
                "0\t02-bandit-sast.txt\tBandit SAST",
                "0\t03-detect-secrets.txt\tDetect-secrets scan",
            ]
        ),
    )
    _write(readiness / "02-bandit-sast.txt")
    _write(readiness / "03-detect-secrets-baseline-check.txt")
    _write(readiness / "04-terraform-prod-validate.txt")
    _write(readiness / "05-terraform-prod-fmt-check.txt")
    _write(readiness / "06-iac-trivy-config.txt")

    pipeline = root / "evidence" / "pipeline-runs" / "12345"
    _write(pipeline / "manifest.txt")
    _write(pipeline / "security-report" / "bandit-report.json", "{}")
    _write(pipeline / "security-report" / "bandit-report.txt")
    _write(pipeline / "secrets-scan-reports" / "detect-secrets-baseline-check.txt")
    _write(pipeline / "python-sca-reports" / "pip-audit-report.txt")
    _write(pipeline / "trivy-reports" / "trivy-api-report.txt")

    dast = root / "evidence" / "dast-runs" / "20260705-140000"
    _write(dast / "dast-manifest.txt")
    _write(dast / "08-dast-health-security-report.txt")
    _write(dast / "08-dast-zap-baseline.txt")

    _write(
        root / "google-oauth-scope-inventory.md",
        "| Google OAuth consent app name | CopilotGTM | Console |\n"
        "| Google OAuth client ID | client-id.apps.googleusercontent.com | Console |\n"
        "| CASA / Google notification email | casa@example.com | Inbox |\n"
        "| Authorized assessor path | Assessor | Contract |\n"
        "| Assessor test account | assessor@example.com | Created |\n",
    )
    _write(
        root / "casa-scope-values.local.json",
        "{"
        '"google_oauth_consent_app_name":"CopilotGTM",'
        '"google_oauth_client_id":"client-id.apps.googleusercontent.com",'
        '"casa_google_notification_email":"casa@example.com",'
        '"authorized_assessor_path":"Assessor",'
        '"assessor_test_account":"assessor@example.com"'
        "}",
    )


def test_submission_preflight_passes_when_required_evidence_exists(tmp_path):
    _make_ready_tree(tmp_path)

    result = preflight.run_preflight(tmp_path)

    assert result.passed is True
    assert result.failures == []


def test_submission_preflight_reports_missing_pipeline_artifact(tmp_path):
    _make_ready_tree(tmp_path)
    (
        tmp_path / "evidence" / "pipeline-runs" / "12345" / "trivy-reports" / "trivy-api-report.txt"
    ).unlink()

    result = preflight.run_preflight(tmp_path)

    assert result.passed is False
    assert any("pipeline artifact missing: trivy-reports" in failure for failure in result.failures)


def test_submission_preflight_accepts_infra_pipeline_iac_scan_artifact(tmp_path):
    _make_ready_tree(tmp_path)
    (tmp_path / "evidence" / "runs" / "20260705-134328" / "06-iac-trivy-config.txt").unlink()
    _write(
        tmp_path
        / "evidence"
        / "infra-pipeline-runs"
        / "67890"
        / "terraform-iac-scan"
        / "terraform-iac-trivy-report.txt"
    )

    result = preflight.run_preflight(tmp_path)

    assert result.passed is True


def test_submission_preflight_rejects_failed_local_iac_scan(tmp_path):
    _make_ready_tree(tmp_path)
    iac_report = tmp_path / "evidence" / "runs" / "20260705-134328" / "06-iac-trivy-config.txt"
    iac_report.write_text("trivy failed\n\nExitCode=127\n", encoding="utf-8")

    result = preflight.run_preflight(tmp_path)

    assert result.passed is False
    assert any("IaC scan evidence missing" in failure for failure in result.failures)


def test_submission_preflight_uses_latest_clean_readiness_run(tmp_path):
    _make_ready_tree(tmp_path)
    failed = tmp_path / "evidence" / "runs" / "20260705-150000"
    _write(
        failed / "99-summary.txt",
        "2\t06-iac-trivy-config-status.txt\tTerraform Trivy config scan status",
    )
    _write(failed / "02-bandit-sast.txt")
    _write(failed / "03-detect-secrets-baseline-check.txt")
    _write(failed / "04-terraform-prod-validate.txt")
    _write(failed / "05-terraform-prod-fmt-check.txt")

    result = preflight.run_preflight(tmp_path)

    assert result.passed is True


def test_submission_preflight_reports_missing_google_scope_values(tmp_path):
    _make_ready_tree(tmp_path)
    (tmp_path / "casa-scope-values.local.json").unlink()

    result = preflight.run_preflight(tmp_path)

    assert result.passed is False
    assert any("Google/CASA scope values missing" in failure for failure in result.failures)
