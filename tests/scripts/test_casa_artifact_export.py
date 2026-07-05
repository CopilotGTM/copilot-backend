from pathlib import Path

SCRIPT_PATH = Path("infra/security/casa/export-prod-pipeline-artifacts.ps1")
INFRA_SCRIPT_PATH = Path("infra/security/casa/export-prod-infra-pipeline-artifacts.ps1")
COMBINED_SCRIPT_PATH = Path("infra/security/casa/export-prod-casa-evidence.ps1")
README_PATH = Path("infra/security/casa/README.md")


def test_casa_artifact_export_downloads_required_prod_artifacts() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    for artifact_name in [
        "secrets-scan-reports",
        "python-sca-reports",
        "trivy-reports",
        "security-report",
    ]:
        assert artifact_name in script_text

    assert "az pipelines runs artifact download" in script_text
    assert "az pipelines runs artifact list" in script_text
    assert "Missing required pipeline artifact" in script_text
    assert "--run-id $RunId" in script_text
    assert "--artifact-name $artifactName" in script_text
    assert "manifest.txt" in script_text


def test_casa_readme_documents_pipeline_artifact_export_command() -> None:
    readme_text = README_PATH.read_text(encoding="utf-8")

    assert "export-prod-pipeline-artifacts.ps1" in readme_text
    assert "export-prod-casa-evidence.ps1" in readme_text
    assert "check_casa_release_publication.py" in readme_text
    assert "-RunId <Build.BuildId>" in readme_text
    assert "Commit and push the CASA pipeline changes" in readme_text
    assert "release/backend" in readme_text
    assert "release/infra" in readme_text


def test_casa_infra_artifact_export_downloads_terraform_iac_scan() -> None:
    script_text = INFRA_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "terraform-iac-scan" in script_text
    assert "az pipelines runs artifact download" in script_text
    assert "az pipelines runs artifact list" in script_text
    assert "Missing required infra pipeline artifact" in script_text
    assert "--run-id $RunId" in script_text
    assert "--artifact-name $artifactName" in script_text
    assert "manifest.txt" in script_text


def test_combined_casa_evidence_export_runs_both_exports_and_preflight() -> None:
    script_text = COMBINED_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "export-prod-pipeline-artifacts.ps1" in script_text
    assert "export-prod-infra-pipeline-artifacts.ps1" in script_text
    assert "[string]$BackendRunId" in script_text
    assert "[string]$InfraRunId" in script_text
    assert "-RunId $BackendRunId" in script_text
    assert "-RunId $InfraRunId" in script_text
    assert "check_casa_submission_preflight.py" in script_text
    assert "submission-preflight.txt" in script_text


def test_combined_casa_evidence_export_can_discover_latest_successful_runs() -> None:
    script_text = COMBINED_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "UseLatestSuccessfulRuns" in script_text
    assert "copilot-backend-pipeline-prod" in script_text
    assert "copilot-infra-pipeline-prod" in script_text
    assert "az pipelines list" in script_text
    assert "az pipelines runs list" in script_text
    assert "--pipeline-ids $pipelineId" in script_text
    assert "az pipelines runs artifact list" in script_text
    assert "secrets-scan-reports" in script_text
    assert "terraform-iac-scan" in script_text


def test_combined_casa_evidence_export_prints_clean_failures() -> None:
    script_text = COMBINED_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "Invoke-CasaEvidenceExport" in script_text
    assert "[Console]::Error.WriteLine" in script_text
    assert "ERROR: $($_.Exception.Message)" in script_text
    assert "exit 1" in script_text
