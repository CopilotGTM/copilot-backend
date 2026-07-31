#!/usr/bin/env python3
"""Export, delete, restore, or verify one tenant inside `tenant_data`."""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from scripts.maintenance.cutover_shared_tenant_schema import (
    current_org_migration_head,
)
from src.infra.database.sessions import master_session_scope
from src.infra.utils.logger import get_logger
from src.service.orgnisation.shared_tenancy_cutover_service import CutoverError
from src.service.orgnisation.shared_tenancy_lifecycle_service import (
    SharedTenancyLifecycleService,
)

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Expose only row-scoped lifecycle operations for the permanent schema."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export")
    _add_organisation_id(export)
    export.add_argument("--destination", required=True, type=Path)

    delete = commands.add_parser("delete")
    _add_organisation_id(delete)
    delete.add_argument("--approval-token")
    delete.add_argument("--apply", action="store_true")

    restore = commands.add_parser("restore")
    _add_organisation_id(restore)
    restore.add_argument("--manifest", required=True, type=Path)
    restore.add_argument("--replace-approval-token")
    restore.add_argument("--apply", action="store_true", required=True)

    verify = commands.add_parser("verify")
    _add_organisation_id(verify)
    verify.add_argument("--manifest", required=True, type=Path)
    return parser


def _add_organisation_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--organisation-id", required=True, type=UUID)


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run one lifecycle operation through the existing master session boundary."""

    with master_session_scope() as session:
        service = SharedTenancyLifecycleService(
            session,
            expected_revision=current_org_migration_head(),
        )
        if args.command == "export":
            return service.export(
                args.organisation_id,
                destination=args.destination,
            )
        if args.command == "delete":
            return service.delete(
                args.organisation_id,
                approval_token=args.approval_token,
                apply=args.apply,
            )
        if args.command == "restore":
            return service.restore(
                args.organisation_id,
                manifest_path=args.manifest,
                replacement_approval_token=args.replace_approval_token,
                apply=args.apply,
            )
        return service.verify(
            args.organisation_id,
            manifest_path=args.manifest,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Print structured evidence and return nonzero on any fail-closed refusal."""

    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    try:
        report = run(args)
        exit_code = 0
    except CutoverError as exc:
        report = {
            "operation": args.command,
            "result": "failed",
            "organisation_id": str(args.organisation_id),
            "error_code": exc.code,
        }
        exit_code = 1
    except Exception:
        report = {
            "operation": args.command,
            "result": "failed",
            "organisation_id": str(args.organisation_id),
            "error_code": "unexpected_failure",
        }
        exit_code = 1
    _log_lifecycle_report(report, started=started)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


def _log_lifecycle_report(report: dict[str, Any], *, started: float) -> None:
    """Emit the row-free lifecycle outcome consumed by operations alerts."""

    outcome = str(report.get("result", "unknown"))
    logger.log(
        logging.ERROR if outcome == "failed" else logging.INFO,
        "Shared tenant lifecycle operation completed",
        extra={
            "event": "shared_tenancy_lifecycle",
            "operation": report.get("operation"),
            "outcome": outcome,
            "organisation_id": report.get("organisation_id"),
            "error_code": report.get("error_code"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
