from pathlib import Path

RUNNER_PATH = Path("infra/security/casa/run-readiness-scans.ps1")


def test_readiness_runner_supports_optional_iac_trivy_config_scan() -> None:
    runner_text = RUNNER_PATH.read_text(encoding="utf-8")

    assert "[switch]$RunIacScan" in runner_text
    assert "06-iac-trivy-config.txt" in runner_text
    assert "trivy config" in runner_text
    assert "$script:IacScanAvailable" in runner_text
    assert "docker info" in runner_text
    assert "docker run --rm" in runner_text
    assert "aquasec/trivy" in runner_text
    assert "infra/terraform/azure/prod" in runner_text
