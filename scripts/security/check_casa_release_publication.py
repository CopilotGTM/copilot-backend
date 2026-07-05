import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

BACKEND_PIPELINE_PATH = ".azure-pipelines/azure-backend-pipeline-prod.yml"
INFRA_TEMPLATE_PATH = ".azure-pipelines/templates/terraform-env-stage.yml"

BACKEND_REQUIRED_MARKERS = {
    "backend pipeline missing Bandit text report": "security-reports/bandit-report.txt",
    "backend pipeline missing secrets scan artifact": "artifact: 'secrets-scan-reports'",
    "backend pipeline missing Python SCA artifact": "artifact: 'python-sca-reports'",
    "backend pipeline missing detect-secrets baseline gate": "detect-secrets-baseline-check.txt",
}

INFRA_REQUIRED_MARKERS = {
    "infra pipeline missing Terraform IaC scan artifact": "artifact: terraform-iac-scan",
    "infra pipeline missing Terraform IaC text report": "terraform-iac-trivy-report.txt",
}


@dataclass
class PublicationCheckResult:
    passed: bool
    failures: list[str]


def _read_git_ref_file(ref: str, path: str) -> str:
    return subprocess.check_output(  # noqa: S603
        ["git", "show", f"{ref}:{path}"],  # noqa: S607
        text=True,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
    )


def _find_missing_markers(*, text: str, markers: dict[str, str]) -> list[str]:
    return [failure for failure, marker in markers.items() if marker not in text]


def check_publication_texts(*, backend_text: str, infra_text: str) -> PublicationCheckResult:
    failures = [
        *_find_missing_markers(text=backend_text, markers=BACKEND_REQUIRED_MARKERS),
        *_find_missing_markers(text=infra_text, markers=INFRA_REQUIRED_MARKERS),
    ]
    return PublicationCheckResult(passed=not failures, failures=failures)


def format_report(result: PublicationCheckResult) -> str:
    lines = [f"CASA release publication check: {'PASS' if result.passed else 'FAIL'}"]
    if result.failures:
        lines.append("")
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in result.failures)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether backend and infra pipeline refs contain required CASA artifact publishing."
    )
    parser.add_argument("--backend-ref", help="Git ref for the backend release branch, for example origin/release/backend/...")
    parser.add_argument("--infra-ref", help="Git ref for the infra release branch, for example origin/release/infra/...")
    parser.add_argument("--backend-file", type=Path, default=Path(BACKEND_PIPELINE_PATH))
    parser.add_argument("--infra-file", type=Path, default=Path(INFRA_TEMPLATE_PATH))
    args = parser.parse_args()

    backend_text = (
        _read_git_ref_file(args.backend_ref, BACKEND_PIPELINE_PATH)
        if args.backend_ref
        else args.backend_file.read_text(encoding="utf-8")
    )
    infra_text = (
        _read_git_ref_file(args.infra_ref, INFRA_TEMPLATE_PATH)
        if args.infra_ref
        else args.infra_file.read_text(encoding="utf-8")
    )

    result = check_publication_texts(backend_text=backend_text, infra_text=infra_text)
    print(format_report(result), end="")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
