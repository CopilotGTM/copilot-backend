from pathlib import Path

SCRIPT_PATH = Path("infra/security/casa/run-approved-window-dast.ps1")
README_PATH = Path("infra/security/casa/README.md")


def test_dast_export_script_requires_approved_window_and_writes_evidence() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[switch]$ApprovedWindow" in script_text
    assert "ApprovedWindow is required" in script_text
    assert "validate_health_security.py" in script_text
    assert "dast-manifest.txt" in script_text
    assert "08-dast-health-security-report.txt" in script_text
    assert "08-dast-health-security-report.json" in script_text


def test_dast_export_script_supports_optional_zap_baseline() -> None:
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[switch]$RunZap" in script_text
    assert "docker run" in script_text
    assert "ghcr.io/zaproxy/zaproxy:stable" in script_text
    assert "zap-baseline.py" in script_text
    assert "08-dast-zap-baseline.txt" in script_text


def test_casa_readme_documents_dast_export_command() -> None:
    readme_text = README_PATH.read_text(encoding="utf-8")

    assert "run-approved-window-dast.ps1" in readme_text
    assert "-ApprovedWindow" in readme_text
    assert "-RunZap" in readme_text
