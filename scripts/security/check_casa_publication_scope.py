import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

REQUIRED_PUBLICATION_FILES = [
    ".azure-pipelines/azure-backend-pipeline-prod.yml",
    ".azure-pipelines/templates/terraform-env-stage.yml",
    ".gitignore",
    ".secrets.baseline",
    "infra/docs/Technical Documentation/soc2-casa-preparedness.md",
    "infra/security/casa/README.md",
    "infra/security/casa/casa-scope-values.example.json",
    "infra/security/casa/evidence/.gitignore",
    "infra/security/casa/evidence-log.md",
    "infra/security/casa/export-prod-casa-evidence.ps1",
    "infra/security/casa/export-prod-infra-pipeline-artifacts.ps1",
    "infra/security/casa/export-prod-pipeline-artifacts.ps1",
    "infra/security/casa/google-oauth-scope-inventory.md",
    "infra/security/casa/run-approved-window-dast.ps1",
    "infra/security/casa/run-readiness-scans.ps1",
    "infra/security/casa/secrets-baseline-audit.md",
    "scripts/security/README.md",
    "scripts/security/build_casa_submission_package.py",
    "scripts/security/check_casa_publication_scope.py",
    "scripts/security/check_casa_release_publication.py",
    "scripts/security/check_casa_submission_preflight.py",
    "scripts/security/check_detect_secrets_baseline.py",
    "scripts/security/validate_health_security.py",
    "tests/scripts/test_backend_pipeline_deploy.py",
    "tests/scripts/test_casa_artifact_export.py",
    "tests/scripts/test_casa_dast_export.py",
    "tests/scripts/test_casa_publication_scope.py",
    "tests/scripts/test_casa_readiness_runner.py",
    "tests/scripts/test_casa_release_publication.py",
    "tests/scripts/test_casa_scope_values.py",
    "tests/scripts/test_casa_submission_package.py",
    "tests/scripts/test_casa_submission_preflight.py",
    "tests/scripts/test_check_detect_secrets_baseline.py",
    "tests/scripts/test_terraform_pipeline_security.py",
    "tests/scripts/test_validate_health_security.py",
]

FORBIDDEN_PUBLICATION_FILES = [
    "infra/security/casa/casa-scope-values.local.json",
    "infra/security/casa/evidence/submission-preflight.txt",
]


@dataclass
class ScopeCheckResult:
    passed: bool
    failures: list[str]


@dataclass
class ChangedFileScope:
    casa_files: list[str]
    non_casa_files: list[str]


def check_publication_scope(repo_root: Path) -> ScopeCheckResult:
    failures = [
        f"required CASA publication file missing: {relative_path}"
        for relative_path in REQUIRED_PUBLICATION_FILES
        if not (repo_root / relative_path).exists()
    ]
    failures.extend(
        f"forbidden CASA publication file listed: {relative_path}"
        for relative_path in FORBIDDEN_PUBLICATION_FILES
        if relative_path in REQUIRED_PUBLICATION_FILES
    )
    return ScopeCheckResult(passed=not failures, failures=failures)


def _status_path(line: str) -> str:
    path = line[3:].strip().replace("\\", "/")
    if " -> " in path:
        path = path.rsplit(" -> ", maxsplit=1)[-1]
    return path.strip('"')


def classify_changed_files(status_lines: list[str]) -> ChangedFileScope:
    required = set(REQUIRED_PUBLICATION_FILES)
    casa_files: list[str] = []
    non_casa_files: list[str] = []
    for line in status_lines:
        if not line.strip():
            continue
        path = _status_path(line)
        if path in required:
            casa_files.append(path)
        else:
            non_casa_files.append(path)
    return ChangedFileScope(casa_files=casa_files, non_casa_files=non_casa_files)


def classify_name_status_lines(status_lines: list[str]) -> ChangedFileScope:
    normalized_lines = []
    for line in status_lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            normalized_lines.append(f"{parts[0]}  {parts[-1]}")
        elif len(parts) >= 2:
            normalized_lines.append(f"{parts[0]}  {parts[1]}")
    return classify_changed_files(normalized_lines)


def current_changed_file_scope() -> ChangedFileScope:
    status = subprocess.check_output(  # noqa: S603
        ["git", "status", "--short", "--untracked-files=all"],  # noqa: S607
        text=True,
        encoding="utf-8",
    )
    return classify_changed_files(status.splitlines())


def current_staged_file_scope() -> ChangedFileScope:
    status = subprocess.check_output(  # noqa: S603
        ["git", "diff", "--cached", "--name-status"],  # noqa: S607
        text=True,
        encoding="utf-8",
    )
    return classify_name_status_lines(status.splitlines())


def format_changed_files_report(changed: ChangedFileScope) -> str:
    lines = ["CASA publication worktree scope"]
    lines.append("")
    lines.append("CASA changed files:")
    lines.extend(f"- {path}" for path in changed.casa_files) if changed.casa_files else lines.append("- none")
    lines.append("")
    lines.append("Non-CASA changed files:")
    lines.extend(f"- {path}" for path in changed.non_casa_files) if changed.non_casa_files else lines.append("- none")
    return "\n".join(lines) + "\n"


def non_casa_change_result(changed: ChangedFileScope, *, label: str = "changed") -> ScopeCheckResult:
    failures = [f"non-CASA {label} file present: {path}" for path in changed.non_casa_files]
    return ScopeCheckResult(passed=not failures, failures=failures)


def _git_add_command() -> str:
    return "git add -- " + " ".join(f'"{relative_path}"' for relative_path in REQUIRED_PUBLICATION_FILES)


def _release_commands() -> list[str]:
    return [
        "uv run python scripts/security/check_casa_publication_scope.py --fail-on-non-casa-staged",
        'git commit -m "Prepare CASA production evidence workflow"',
    ]


def format_report(
    result: ScopeCheckResult,
    *,
    include_files: bool = False,
    include_git_add: bool = False,
    include_release_commands: bool = False,
) -> str:
    lines = [f"CASA publication scope check: {'PASS' if result.passed else 'FAIL'}"]
    if result.failures:
        lines.append("")
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in result.failures)
    if include_files:
        lines.append("")
        lines.append("Required files:")
        lines.extend(REQUIRED_PUBLICATION_FILES)
    if include_git_add:
        lines.append("")
        lines.append("Git add command:")
        lines.append(_git_add_command())
    if include_release_commands:
        lines.append("")
        lines.append("Release commit commands:")
        lines.extend(_release_commands())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the required file scope for publishing CASA changes.")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--print-files", action="store_true", help="Print the exact required CASA publication files.")
    parser.add_argument("--print-git-add", action="store_true", help="Print a dry-run git add command for CASA files.")
    parser.add_argument("--print-release-commands", action="store_true", help="Print dry-run CASA release commit commands.")
    parser.add_argument("--print-worktree-scope", action="store_true", help="Print CASA vs non-CASA changed files.")
    parser.add_argument(
        "--fail-on-non-casa-changes",
        action="store_true",
        help="Fail if the current worktree has changed files outside the CASA publication scope.",
    )
    parser.add_argument(
        "--fail-on-non-casa-staged",
        action="store_true",
        help="Fail if the staged index has files outside the CASA publication scope.",
    )
    args = parser.parse_args()

    result = check_publication_scope(args.repo_root)
    print(
        format_report(
            result,
            include_files=args.print_files,
            include_git_add=args.print_git_add,
            include_release_commands=args.print_release_commands,
        ),
        end="",
    )
    changed = current_changed_file_scope() if args.print_worktree_scope or args.fail_on_non_casa_changes else None
    if changed and args.print_worktree_scope:
        print()
        print(format_changed_files_report(changed), end="")
    if changed and args.fail_on_non_casa_changes:
        non_casa_result = non_casa_change_result(changed)
        if not non_casa_result.passed:
            print()
            print(format_report(non_casa_result), end="")
            return 1
    if args.fail_on_non_casa_staged:
        staged_result = non_casa_change_result(current_staged_file_scope(), label="staged")
        if not staged_result.passed:
            print()
            print(format_report(staged_result), end="")
            return 1
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
