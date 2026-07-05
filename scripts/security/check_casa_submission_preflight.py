import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PreflightResult:
    passed: bool
    failures: list[str]
    warnings: list[str]


REQUIRED_READINESS_FILES = [
    "02-bandit-sast.txt",
    "03-detect-secrets-baseline-check.txt",
    "04-terraform-prod-validate.txt",
    "05-terraform-prod-fmt-check.txt",
]

REQUIRED_PIPELINE_ARTIFACTS = {
    "security-report": ["bandit-report.json", "bandit-report.txt"],
    "secrets-scan-reports": ["detect-secrets-baseline-check.txt"],
    "python-sca-reports": ["pip-audit-report.txt"],
    "trivy-reports": ["trivy-api-report.txt"],
}

REQUIRED_DAST_FILES = [
    "dast-manifest.txt",
    "08-dast-health-security-report.txt",
    "08-dast-zap-baseline.txt",
]

REQUIRED_INFRA_PIPELINE_ARTIFACTS = {
    "terraform-iac-scan": ["terraform-iac-trivy-report.txt"],
}

SCOPE_VALUES_FILE = "casa-scope-values.local.json"
REQUIRED_GOOGLE_SCOPE_VALUES = [
    "google_oauth_consent_app_name",
    "google_oauth_client_id",
    "casa_google_notification_email",
    "authorized_assessor_path",
    "assessor_test_account",
]


def _latest_child(path: Path) -> Path | None:
    if not path.exists():
        return None
    children = [child for child in path.iterdir() if child.is_dir()]
    return max(children, key=lambda child: child.name, default=None)


def _children_newest_first(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted([child for child in path.iterdir() if child.is_dir()], key=lambda child: child.name, reverse=True)


def _readiness_summary_is_clean(summary_path: Path) -> bool:
    for line in summary_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] != "0":
            return False
    return True


def _readiness_run_is_complete(run_path: Path) -> bool:
    summary = run_path / "99-summary.txt"
    return summary.exists() and _readiness_summary_is_clean(summary) and all(
        (run_path / filename).exists() for filename in REQUIRED_READINESS_FILES
    )


def _latest_clean_readiness_run(casa_root: Path) -> Path | None:
    for run_path in _children_newest_first(casa_root / "evidence" / "runs"):
        if _readiness_run_is_complete(run_path):
            return run_path
    return None


def _has_file_under(root: Path, relative_name: str) -> bool:
    return any(path.is_file() for path in root.rglob(relative_name))


def _scan_output_succeeded(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    return "ExitCode=0" in text or "ExitCode=" not in text


def _check_readiness(casa_root: Path, failures: list[str]) -> None:
    if not (casa_root / "evidence" / "runs").exists():
        failures.append("readiness run missing: infra/security/casa/evidence/runs")
        return

    if _latest_clean_readiness_run(casa_root) is not None:
        return

    failures.append("clean readiness run missing: rerun run-readiness-scans.ps1 and resolve non-zero scan exit codes")


def _has_local_iac_scan(casa_root: Path) -> bool:
    return any(
        (run_path / "06-iac-trivy-config.txt").exists()
        and _scan_output_succeeded(run_path / "06-iac-trivy-config.txt")
        for run_path in _children_newest_first(casa_root / "evidence" / "runs")
    )


def _has_infra_pipeline_iac_scan(casa_root: Path) -> bool:
    latest = _latest_child(casa_root / "evidence" / "infra-pipeline-runs")
    if latest is None:
        return False

    for artifact_name, required_files in REQUIRED_INFRA_PIPELINE_ARTIFACTS.items():
        artifact_root = latest / artifact_name
        if not artifact_root.exists():
            return False
        for required_file in required_files:
            if not _has_file_under(artifact_root, required_file):
                return False
    return True


def _check_iac_scan(casa_root: Path, failures: list[str]) -> None:
    if _has_local_iac_scan(casa_root) or _has_infra_pipeline_iac_scan(casa_root):
        return
    failures.append(
        "IaC scan evidence missing: export prod infra terraform-iac-scan artifact "
        "or run run-readiness-scans.ps1 -RunIacScan"
    )


def _check_pipeline_artifacts(casa_root: Path, failures: list[str]) -> None:
    latest = _latest_child(casa_root / "evidence" / "pipeline-runs")
    if latest is None:
        failures.append("prod pipeline artifact export missing: run export-prod-pipeline-artifacts.ps1")
        return

    if not (latest / "manifest.txt").exists():
        failures.append(f"pipeline artifact manifest missing: {latest / 'manifest.txt'}")

    for artifact_name, required_files in REQUIRED_PIPELINE_ARTIFACTS.items():
        artifact_root = latest / artifact_name
        if not artifact_root.exists():
            failures.append(f"pipeline artifact missing: {artifact_name}")
            continue
        for required_file in required_files:
            if not _has_file_under(artifact_root, required_file):
                failures.append(f"pipeline artifact missing: {artifact_name}/{required_file}")


def _check_dast(casa_root: Path, failures: list[str]) -> None:
    latest = _latest_child(casa_root / "evidence" / "dast-runs")
    if latest is None:
        failures.append("DAST evidence missing: run run-approved-window-dast.ps1 in approved window")
        return

    for filename in REQUIRED_DAST_FILES:
        if not (latest / filename).exists():
            failures.append(f"DAST artifact missing: {filename}")


def missing_google_scope_values(casa_root: Path) -> list[str]:
    values_path = casa_root / SCOPE_VALUES_FILE
    if not values_path.exists():
        return REQUIRED_GOOGLE_SCOPE_VALUES.copy()

    values = json.loads(values_path.read_text(encoding="utf-8"))
    return [
        field
        for field in REQUIRED_GOOGLE_SCOPE_VALUES
        if not str(values.get(field, "")).strip() or values.get(field) == "TODO"
    ]


def _check_google_inventory(casa_root: Path, failures: list[str]) -> None:
    inventory = casa_root / "google-oauth-scope-inventory.md"
    if not inventory.exists():
        failures.append("Google OAuth inventory missing")
        return

    missing_values = missing_google_scope_values(casa_root)
    if missing_values:
        failures.append(
            "Google/CASA scope values missing in "
            f"{SCOPE_VALUES_FILE}: {', '.join(missing_values)}"
        )


def run_preflight(casa_root: Path) -> PreflightResult:
    failures: list[str] = []
    warnings: list[str] = []

    _check_google_inventory(casa_root, failures)
    _check_readiness(casa_root, failures)
    _check_iac_scan(casa_root, failures)
    _check_pipeline_artifacts(casa_root, failures)
    _check_dast(casa_root, failures)

    return PreflightResult(passed=not failures, failures=failures, warnings=warnings)


def format_report(result: PreflightResult) -> str:
    lines = [f"CASA submission preflight: {'PASS' if result.passed else 'FAIL'}"]
    if result.failures:
        lines.append("")
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in result.failures)
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether CASA submission evidence is ready.")
    parser.add_argument(
        "--casa-root",
        type=Path,
        default=Path("infra/security/casa"),
        help="Path to the CASA evidence package root.",
    )
    parser.add_argument("--output", type=Path, help="Optional report output path.")
    args = parser.parse_args()

    result = run_preflight(args.casa_root)
    report = format_report(result)
    print(report, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
