#!/usr/bin/env python3
"""Forward-only shared-tenancy cutover and exact legacy-resource cleanup.

Operators call this module for read-only readiness, transactional copy, and the
evidence-gated removal of one approved schema, role, and Key Vault secret. It
never discovers deletion targets or provides a reverse-copy path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from azure.core.exceptions import ResourceNotFoundError
from src.infra.database.sessions import master_session_scope
from src.infra.secrets.master_key_vault_sync import build_master_secret_client_sync
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import get_role_name, get_schema_name
from src.service.orgnisation.shared_tenancy_cutover_service import (
    CUTOVER_TOOL_VERSION,
    CutoverError,
    SharedTenancyCutoverService,
)

TOOL_NAME = "copilotgtm-shared-tenancy-cutover"
logger = get_logger(__name__)
_COPY_VALIDATIONS = {
    "counts",
    "primary_key_sets",
    "checksums",
    "foreign_keys",
    "uniqueness",
    "rls",
    "sentinel_unchanged",
}
_SMOKE_CHECKS = {
    "master_login",
    "organisation_read",
    "organisation_write",
    "integrations",
    "bots",
    "agents",
    "jobs",
    "rls_isolation",
    "permanent_runtime",
}
_MAX_EVIDENCE_BYTES = 20 * 1024 * 1024


class SourceDeletionError(CutoverError):
    """A post-database failure carrying the committed database absence proof."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        database_result: dict[str, Any],
    ) -> None:
        self.database_result = database_result
        super().__init__(code, message)


def write_immutable_report(path: Path, report: dict[str, Any]) -> None:
    """Create one evidence file and refuse to replace an earlier report."""

    with path.open("x", encoding="utf-8") as handle:
        _write_report(handle, report)


def build_parser() -> argparse.ArgumentParser:
    """Expose only the audited forward-cutover operations."""

    parser = argparse.ArgumentParser(
        description="Precheck or atomically copy the sole legacy tenant dataset."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    precheck = commands.add_parser("precheck")
    _add_common_arguments(precheck)

    copy = commands.add_parser("copy-and-validate")
    _add_common_arguments(copy)
    copy.add_argument("--writers-stopped-evidence", required=True)
    copy.add_argument("--backup-reference", required=True)
    copy.add_argument("--apply", action="store_true", required=True)

    delete = commands.add_parser("delete-source-resources")
    delete.add_argument("--source-schema", required=True)
    delete.add_argument("--source-role", required=True)
    delete.add_argument("--source-secret", required=True)
    delete.add_argument("--cutover-report", required=True, type=Path)
    delete.add_argument("--smoke-report", required=True, type=Path)
    delete.add_argument("--apply", action="store_true", required=True)
    return parser


def current_org_migration_head() -> str:
    """Return the single organisation migration head shipped with this checkout."""

    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    config = Config(str(migrations_dir / "alembic.ini"))
    config.set_main_option("script_location", str(migrations_dir))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("organisation migrations have no single head")
    return str(head)


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run one service operation through the existing master session boundary."""

    with master_session_scope() as session:
        service = SharedTenancyCutoverService(
            session,
            expected_revision=current_org_migration_head(),
        )
        if args.command == "precheck":
            return service.precheck(args.organisation_id)
        return service.copy_and_validate(
            args.organisation_id,
            writers_stopped_evidence=args.writers_stopped_evidence,
            backup_reference=args.backup_reference,
            apply=args.apply,
        )


def validate_source_deletion_evidence(
    *,
    arguments: dict[str, object],
    cutover_report: dict[str, object],
    smoke_report: dict[str, object],
    cutover_report_sha256: str,
) -> dict[str, str]:
    """Bind exact deletion targets to complete copy and runtime evidence."""

    if arguments.get("apply") is not True:
        raise CutoverError("apply_required", "--apply is required")
    if not _nonempty(cutover_report.get("backup_reference")):
        raise CutoverError("backup_reference_required", "backup evidence is required")
    if not _nonempty(smoke_report.get("export_reference")):
        raise CutoverError("export_reference_required", "tenant export evidence is required")
    if not _complete_copy_report(cutover_report):
        raise CutoverError(
            "copy_validation_evidence_incomplete",
            "complete copy-and-validation evidence is required",
        )
    if not _complete_smoke_report(smoke_report):
        raise CutoverError(
            "runtime_smoke_evidence_incomplete",
            "complete permanent-runtime smoke evidence is required",
        )
    if smoke_report.get("cutover_report_sha256") != cutover_report_sha256:
        raise CutoverError(
            "cutover_report_hash_mismatch",
            "smoke evidence does not reference this cutover report",
        )

    try:
        organisation_id = UUID(str(cutover_report["organisation_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CutoverError(
            "copy_validation_evidence_incomplete",
            "cutover evidence has no valid organisation identity",
        ) from exc
    approved = smoke_report["approved_source_resources"]
    if not isinstance(approved, dict):
        raise CutoverError(
            "exact_source_target_required",
            "approved source resource manifest is required",
        )
    expected = {
        "source_schema": get_schema_name(organisation_id),
        "source_role": get_role_name(organisation_id),
        "source_secret": approved.get("secret"),
    }
    supplied = {
        "source_schema": arguments.get("source_schema"),
        "source_role": arguments.get("source_role"),
        "source_secret": arguments.get("source_secret"),
    }
    approved_targets = {
        "source_schema": approved.get("schema"),
        "source_role": approved.get("role"),
        "source_secret": approved.get("secret"),
    }
    secret = supplied["source_secret"]
    if (
        supplied != approved_targets
        or supplied != expected
        or cutover_report.get("source_schema") != supplied["source_schema"]
        or not isinstance(secret, str)
        or re.fullmatch(r"[0-9A-Za-z-]{1,127}", secret) is None
    ):
        raise CutoverError(
            "exact_source_target_required",
            "source targets must exactly match the approved organisation manifest",
        )
    return {
        "organisation_id": str(organisation_id),
        "source_schema": str(supplied["source_schema"]),
        "source_role": str(supplied["source_role"]),
        "source_secret": secret,
        "backup_reference": str(cutover_report["backup_reference"]),
        "export_reference": str(smoke_report["export_reference"]),
    }


def run_source_deletion(
    args: argparse.Namespace,
    *,
    report_id: str | None = None,
) -> dict[str, Any]:
    """Delete exact approved source resources and return hash-linked evidence."""

    started = time.perf_counter()
    cutover_report, cutover_hash = _read_json_report(args.cutover_report)
    smoke_report, smoke_hash = _read_json_report(args.smoke_report)
    approval = validate_source_deletion_evidence(
        arguments={
            "source_schema": args.source_schema,
            "source_role": args.source_role,
            "source_secret": args.source_secret,
            "apply": args.apply,
        },
        cutover_report=cutover_report,
        smoke_report=smoke_report,
        cutover_report_sha256=cutover_hash,
    )

    with master_session_scope() as session:
        database_result = SharedTenancyCutoverService(
            session,
            expected_revision=current_org_migration_head(),
        ).delete_source_database_resources(
            UUID(approval["organisation_id"]),
            source_schema=approval["source_schema"],
            source_role=approval["source_role"],
            apply=True,
        )

    # Database cleanup commits first by design. A Key Vault failure leaves only
    # an unused recoverable secret and can be retried from the same exact manifest.
    try:
        secret_result = delete_and_verify_secret(
            build_master_secret_client_sync(prefer_default_credential=True),
            approval["source_secret"],
        )
    except CutoverError as exc:
        raise SourceDeletionError(
            exc.code,
            str(exc),
            database_result=database_result,
        ) from exc
    except Exception as exc:
        raise SourceDeletionError(
            "source_secret_deletion_failed",
            "database resources are absent but Key Vault deletion failed",
            database_result=database_result,
        ) from exc
    return {
        "report_id": report_id or str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": TOOL_NAME,
        "tool_version": CUTOVER_TOOL_VERSION,
        "operation": "delete-source-resources",
        "result": "source_resources_deleted",
        "organisation_id": approval["organisation_id"],
        "source_schema": approval["source_schema"],
        "source_role": approval["source_role"],
        "source_secret": approval["source_secret"],
        "backup_reference": approval["backup_reference"],
        "export_reference": approval["export_reference"],
        "cutover_report_sha256": cutover_hash,
        "smoke_report_sha256": smoke_hash,
        "database": database_result,
        "key_vault": secret_result,
        "validation": {
            "schema_absent": database_result["schema_absent"],
            "role_absent": database_result["role_absent"],
            "secret_absent": secret_result["secret_absent"],
        },
        "timings_ms": {
            "total": round((time.perf_counter() - started) * 1000, 3),
        },
    }


def delete_and_verify_secret(client: Any, secret_name: str) -> dict[str, bool]:
    """Soft-delete one exact Key Vault secret and prove active lookup is absent."""

    try:
        client.get_secret(secret_name)
    except ResourceNotFoundError:
        return {
            "secret_existed": False,
            "secret_absent": True,
            "soft_deleted": False,
        }

    client.begin_delete_secret(secret_name).wait()
    try:
        client.get_secret(secret_name)
    except ResourceNotFoundError:
        return {
            "secret_existed": True,
            "secret_absent": True,
            "soft_deleted": True,
        }
    raise CutoverError(
        "source_secret_deletion_verification_failed",
        "source secret remains active after the delete operation",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "delete-source-resources":
        return _run_source_deletion_cli(args)

    started = time.perf_counter()

    # Reserving the path before database access guarantees that every admitted
    # attempt owns exactly one evidence file, including failed copy transactions.
    with args.report.open("x", encoding="utf-8") as handle:
        try:
            report = run(args)
            exit_code = 0
        except CutoverError as exc:
            report = _failure_report(args, exc.code, started)
            exit_code = 1
        except Exception:
            report = _failure_report(args, "unexpected_failure", started)
            exit_code = 1
        _write_report(handle, report)

    _log_cutover_report(report)
    print(f"{report['result']}: {args.report}")
    return exit_code


def _run_source_deletion_cli(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    report_id = str(uuid4())
    report_path = args.cutover_report.with_name(
        f"{args.cutover_report.stem}.source-deletion.{report_id}.json"
    )
    with report_path.open("x", encoding="utf-8") as handle:
        try:
            report = run_source_deletion(args, report_id=report_id)
            exit_code = 0
        except SourceDeletionError as exc:
            report = _source_deletion_failure_report(
                args,
                report_id=report_id,
                error_code=exc.code,
                started=started,
                database_result=exc.database_result,
            )
            exit_code = 1
        except CutoverError as exc:
            report = _source_deletion_failure_report(
                args,
                report_id=report_id,
                error_code=exc.code,
                started=started,
            )
            exit_code = 1
        except Exception:
            report = _source_deletion_failure_report(
                args,
                report_id=report_id,
                error_code="unexpected_failure",
                started=started,
            )
            exit_code = 1
        _write_report(handle, report)

    _log_cutover_report(report)
    print(f"{report['result']}: {report_path}")
    return exit_code


def _source_deletion_failure_report(
    args: argparse.Namespace,
    *,
    report_id: str,
    error_code: str,
    started: float,
    database_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "report_id": report_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": TOOL_NAME,
        "tool_version": CUTOVER_TOOL_VERSION,
        "operation": "delete-source-resources",
        "result": "failed",
        "error_code": error_code,
        "source_schema": args.source_schema,
        "source_role": args.source_role,
        "source_secret": args.source_secret,
        "cutover_report_sha256": _hash_file_if_available(args.cutover_report),
        "smoke_report_sha256": _hash_file_if_available(args.smoke_report),
        "database_changes_committed": database_result is not None,
        "database": database_result,
        "timings_ms": {
            "total": round((time.perf_counter() - started) * 1000, 3),
        },
    }


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--organisation-id", required=True, type=UUID)
    parser.add_argument("--report", required=True, type=Path)


def _complete_copy_report(report: dict[str, object]) -> bool:
    revisions = (
        report.get("expected_revision"),
        report.get("source_revision"),
        report.get("destination_revision"),
    )
    if (
        report.get("operation") != "copy-and-validate"
        or report.get("result") != "copied_and_validated"
        or report.get("mutation_performed") is not True
        or report.get("destination_schema") != "tenant_data"
        or not _nonempty(report.get("writers_stopped_evidence"))
        or not _all_true(report.get("validation"), _COPY_VALIDATIONS)
        or not all(_nonempty(revision) for revision in revisions)
        or len(set(revisions)) != 1
    ):
        return False
    tables = report.get("tables")
    if not isinstance(tables, dict) or not tables:
        return False
    for table in tables.values():
        if not isinstance(table, dict):
            return False
        source_count = table.get("source_count")
        destination_count = table.get("destination_count")
        source_key_hash = table.get("source_primary_key_sha256")
        destination_key_hash = table.get("destination_primary_key_sha256")
        source_row_hash = table.get("source_row_sha256")
        destination_row_hash = table.get("destination_row_sha256")
        if (
            not isinstance(source_count, int)
            or source_count < 0
            or source_count != destination_count
            or not _is_sha256(source_key_hash)
            or source_key_hash != destination_key_hash
            or not _is_sha256(source_row_hash)
            or source_row_hash != destination_row_hash
        ):
            return False
    return True


def _complete_smoke_report(report: dict[str, object]) -> bool:
    return (
        report.get("operation") == "permanent-runtime-smoke"
        and report.get("result") == "passed"
        and _nonempty(report.get("evidence_reference"))
        and isinstance(report.get("approved_source_resources"), dict)
        and _all_true(report.get("checks"), _SMOKE_CHECKS)
    )


def _all_true(value: object, required_keys: set[str]) -> bool:
    return isinstance(value, dict) and all(value.get(key) is True for key in required_keys)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read_json_report(path: Path) -> tuple[dict[str, object], str]:
    if path.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise CutoverError("evidence_report_too_large", "evidence report exceeds 20 MiB")
    payload = path.read_bytes()
    try:
        report = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError("invalid_evidence_report", "evidence report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise CutoverError("invalid_evidence_report", "evidence report must be a JSON object")
    return report, hashlib.sha256(payload).hexdigest()


def _hash_file_if_available(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _failure_report(
    args: argparse.Namespace,
    error_code: str,
    started: float,
) -> dict[str, Any]:
    return {
        "report_id": str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": TOOL_NAME,
        "tool_version": CUTOVER_TOOL_VERSION,
        "operation": args.command,
        "organisation_id": str(args.organisation_id),
        "mutation_performed": False,
        "result": "failed",
        "error_code": error_code,
        "timings_ms": {
            "total": round((time.perf_counter() - started) * 1000, 3),
        },
    }


def _write_report(handle: Any, report: dict[str, Any]) -> None:
    """Write only structured evidence; database rows and credentials never enter it."""

    json.dump(report, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


def _log_cutover_report(report: dict[str, Any]) -> None:
    """Forward the immutable report outcome without its table or row evidence."""

    outcome = str(report.get("result", "unknown"))
    timings = report.get("timings_ms")
    duration_ms = timings.get("total") if isinstance(timings, dict) else None
    logger.log(
        logging.ERROR if outcome == "failed" else logging.INFO,
        "Shared tenant cutover operation completed",
        extra={
            "event": "shared_tenancy_cutover",
            "operation": report.get("operation"),
            "outcome": outcome,
            "report_id": report.get("report_id"),
            "organisation_id": report.get("organisation_id"),
            "error_code": report.get("error_code"),
            "duration_ms": duration_ms,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
