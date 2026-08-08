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


def test_terraform_pipeline_does_not_retry_a_partially_applied_saved_plan() -> None:
    template_text = TERRAFORM_TEMPLATE_PATH.read_text(encoding="utf-8")
    apply_task = template_text.split("displayName: 'Terraform Init & Apply'", maxsplit=1)[1]
    apply_task = apply_task.split("                  env:", maxsplit=1)[0]

    assert 'terraform plan -var-file="terraform.tfvars" -parallelism=5' in template_text
    assert "terraform apply -input=false -auto-approve -parallelism=5 tfplan" in apply_task
    assert "retryCountOnTaskFailure" not in apply_task
