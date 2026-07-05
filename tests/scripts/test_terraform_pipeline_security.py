from pathlib import Path

TERRAFORM_TEMPLATE_PATH = Path(".azure-pipelines/templates/terraform-env-stage.yml")


def test_terraform_pipeline_publishes_trivy_iac_scan_artifact() -> None:
    template_text = TERRAFORM_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "terraform-iac-scan" in template_text
    assert "trivy config" in template_text
    assert "docker run --rm" in template_text
    assert "aquasec/trivy:0.49.1" in template_text
    assert "terraform-iac-trivy-report.txt" in template_text
    assert "Publish Terraform IaC Scan Artifact" in template_text
