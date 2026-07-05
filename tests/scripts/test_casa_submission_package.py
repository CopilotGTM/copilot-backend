from scripts.security import build_casa_submission_package as builder
from tests.scripts.test_casa_submission_preflight import _make_ready_tree


def test_build_submission_package_copies_required_docs_and_evidence(tmp_path):
    casa_root = tmp_path / "casa"
    _make_ready_tree(casa_root)
    (casa_root / "README.md").write_text("readme", encoding="utf-8")
    (casa_root / "evidence-log.md").write_text("log", encoding="utf-8")
    (casa_root / "secrets-baseline-audit.md").write_text("audit", encoding="utf-8")
    infra_iac_report = (
        casa_root
        / "evidence"
        / "infra-pipeline-runs"
        / "67890"
        / "terraform-iac-scan"
        / "terraform-iac-trivy-report.txt"
    )
    infra_iac_report.parent.mkdir(parents=True, exist_ok=True)
    infra_iac_report.write_text("iac scan", encoding="utf-8")

    package_path = builder.build_package(
        casa_root=casa_root,
        output_root=tmp_path / "packages",
        package_name="package-1",
    )

    assert (package_path / "README.md").exists()
    assert (package_path / "evidence-log.md").exists()
    assert (package_path / "google-oauth-scope-inventory.md").exists()
    assert (package_path / "evidence" / "runs" / "20260705-134328" / "99-summary.txt").exists()
    assert (package_path / "evidence" / "pipeline-runs" / "12345" / "manifest.txt").exists()
    assert (
        package_path
        / "evidence"
        / "infra-pipeline-runs"
        / "67890"
        / "terraform-iac-scan"
        / "terraform-iac-trivy-report.txt"
    ).exists()
    assert (
        package_path / "evidence" / "dast-runs" / "20260705-140000" / "dast-manifest.txt"
    ).exists()
    assert (package_path / "SUBMISSION-MANIFEST.txt").exists()


def test_build_submission_package_refuses_when_preflight_fails(tmp_path):
    casa_root = tmp_path / "casa"
    _make_ready_tree(casa_root)
    (casa_root / "evidence" / "dast-runs" / "20260705-140000" / "08-dast-zap-baseline.txt").unlink()

    try:
        builder.build_package(casa_root=casa_root, output_root=tmp_path / "packages")
    except RuntimeError as exc:
        assert "CASA submission preflight failed" in str(exc)
    else:
        raise AssertionError("package build should fail when preflight fails")


def test_submission_package_cli_prints_clean_preflight_failure(tmp_path, monkeypatch, capsys):
    casa_root = tmp_path / "casa"
    _make_ready_tree(casa_root)
    (casa_root / "evidence" / "dast-runs" / "20260705-140000" / "08-dast-zap-baseline.txt").unlink()
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_casa_submission_package.py",
            "--casa-root",
            str(casa_root),
            "--output-root",
            str(tmp_path / "packages"),
        ],
    )

    exit_code = builder.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "CASA submission preflight failed" in captured.err
    assert "Traceback" not in captured.err
