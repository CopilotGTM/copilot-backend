"""Reject runtime source paths that can bypass the shared tenant boundary."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ENGINE_PATTERN = re.compile(r"\bcreate_(?:async_)?engine\s*\(")
ORG_SCHEMA_PATTERN = re.compile(r"\borg_[0-9a-fA-F][0-9a-fA-F_]{2,}\b")
TENANT_SESSION_PATTERN = re.compile(
    r"\b(?:acquire_org_(?:async_)?session|org_(?:async_)?session_scope)\s*\("
    r"|\.tenant_session\s*\("
)

REVIEWED_API_TENANT_SESSION_BOUNDARIES = {
    "src/api/routers/crm/leads/lead_forms_handler.py",
    "src/api/routers/crm/leads/private_lead_ingestion_handler.py",
}


def _allows_direct_engine_creation(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized in {
        "src/infra/database/provider_async.py",
        "src/infra/database/provider_sync.py",
        "scripts/cleanup_internal_stakeholders.py",
        "scripts/org_admin_session.py",
        "scripts/run_master_migrations.py",
        "scripts/run_org_migrations.py",
    } or normalized.startswith("scripts/maintenance/")


def _allows_explicit_org_schema(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("migrations/") or normalized.startswith("scripts/maintenance/")


def _is_unreviewed_api_tenant_session_boundary(path: str, content: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith("src/api/routers/")
        and normalized not in REVIEWED_API_TENANT_SESSION_BOUNDARIES
        and TENANT_SESSION_PATTERN.search(content) is not None
    )


def find_bypasses(files: dict[str, str]) -> list[str]:
    """Return deterministic tenant-boundary findings for the supplied sources."""
    findings: list[str] = []
    for path, content in files.items():
        if ENGINE_PATTERN.search(content) and not _allows_direct_engine_creation(path):
            findings.append(f"{path}: direct tenant engine creation")
        if ORG_SCHEMA_PATTERN.search(content) and not _allows_explicit_org_schema(path):
            findings.append(f"{path}: explicit org schema reference")
        if _is_unreviewed_api_tenant_session_boundary(path, content):
            findings.append(f"{path}: direct tenant-session acquisition from API router")
    return findings


def exit_code_for_findings(findings: list[str], *, advisory: bool = False) -> int:
    """Fail release checks on findings unless an operator requested advisory mode."""
    if advisory:
        return 0
    return 1 if findings else 0


def _should_scan_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return not normalized.startswith("scripts/test_local/")


def _read_files(paths: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for root in paths:
        for path in Path(root).rglob("*.py"):
            normalized = path.as_posix()
            if normalized.startswith("scripts/maintenance/"):
                continue
            if not _should_scan_path(normalized):
                continue
            files[str(path)] = path.read_text(encoding="utf-8")
    return files


def _default_scan_roots(
    *,
    include_scripts: bool = False,
    include_tests: bool = False,
) -> list[str]:
    roots = ["src"]
    if include_scripts:
        roots.append("scripts")
    if include_tests:
        roots.append("tests")
    return roots


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--advisory", action="store_true")
    parser.add_argument("--include-scripts", action="store_true")
    parser.add_argument("--include-tests", action="store_true")
    args = parser.parse_args()
    findings = find_bypasses(
        _read_files(
            _default_scan_roots(
                include_scripts=args.include_scripts,
                include_tests=args.include_tests,
            )
        )
    )
    for finding in findings:
        print(finding)
    raise SystemExit(exit_code_for_findings(findings, advisory=args.advisory))


if __name__ == "__main__":
    main()
