"""Contracts for the one-source shared-tenancy cutover boundary."""

from __future__ import annotations

import json
from argparse import Namespace
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from src.infra.utils.resource_naming import get_role_name, get_schema_name
from src.service.orgnisation.shared_tenancy_cutover_service import (
    CutoverError,
    SharedTenancyCutoverService,
)

from scripts.maintenance import cutover_shared_tenant_schema as cutover_cli


def _service_without_database() -> SharedTenancyCutoverService:
    """Build a service for authorization checks that must precede database I/O."""

    return SharedTenancyCutoverService(
        cast(Session, object()),
        expected_revision="test_head",
    )


@pytest.mark.parametrize(
    ("apply", "writers_stopped_evidence", "backup_reference", "error_code"),
    [
        (False, "deploy/42", "backup/42", "apply_required"),
        (True, "", "backup/42", "writers_stopped_evidence_required"),
        (True, "deploy/42", "", "backup_reference_required"),
    ],
)
def test_copy_requires_explicit_apply_and_operational_evidence_before_database_io(
    apply: bool,
    writers_stopped_evidence: str,
    backup_reference: str,
    error_code: str,
) -> None:
    """Mutation is impossible until all three independent operator gates exist."""

    with pytest.raises(CutoverError) as exc_info:
        _service_without_database().copy_and_validate(
            uuid4(),
            writers_stopped_evidence=writers_stopped_evidence,
            backup_reference=backup_reference,
            apply=apply,
        )

    assert exc_info.value.code == error_code


def test_immutable_report_refuses_to_overwrite_existing_evidence(tmp_path: Path) -> None:
    """A rerun cannot replace evidence from an earlier cutover attempt."""

    from scripts.maintenance.cutover_shared_tenant_schema import (
        write_immutable_report,
    )

    report_path = tmp_path / "cutover.json"
    report_path.write_text('{"result": "earlier"}', encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_immutable_report(report_path, {"result": "replacement"})

    assert report_path.read_text(encoding="utf-8") == '{"result": "earlier"}'


def test_cli_exposes_only_forward_cutover_and_exact_source_deletion() -> None:
    """The maintenance tool must never expose reverse copy or broad cleanup."""

    parser = cutover_cli.build_parser()

    assert set(parser._subparsers._group_actions[0].choices) == {  # noqa: SLF001
        "precheck",
        "copy-and-validate",
        "delete-source-resources",
    }


def test_copy_cli_requires_operator_evidence_apply_and_report() -> None:
    """Argparse must reject an incomplete mutating command before service access."""

    parser = cutover_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "copy-and-validate",
                "--organisation-id",
                str(uuid4()),
            ]
        )


def test_cli_writes_sanitized_failure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known refusals become immutable evidence without leaking exception text."""

    def _refuse(_args: object) -> dict[str, object]:
        raise CutoverError("destination_not_empty", "private database detail")

    monkeypatch.setattr(cutover_cli, "run", _refuse)
    report_path = tmp_path / "failed-cutover.json"
    organisation_id = uuid4()

    exit_code = cutover_cli.main(
        [
            "precheck",
            "--organisation-id",
            str(organisation_id),
            "--report",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["error_code"] == "destination_not_empty"
    assert report["organisation_id"] == str(organisation_id)
    assert report["mutation_performed"] is False
    assert "private database detail" not in report_path.read_text(encoding="utf-8")


def _valid_deletion_evidence() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    organisation_id = uuid4()
    source_schema = get_schema_name(organisation_id)
    source_role = get_role_name(organisation_id)
    source_secret = f"gtm-{organisation_id}-db-password"
    cutover_report: dict[str, object] = {
        "operation": "copy-and-validate",
        "result": "copied_and_validated",
        "mutation_performed": True,
        "organisation_id": str(organisation_id),
        "source_schema": source_schema,
        "destination_schema": "tenant_data",
        "expected_revision": "head",
        "source_revision": "head",
        "destination_revision": "head",
        "writers_stopped_evidence": "deployment/42/drained",
        "backup_reference": "backup/42",
        "validation": {
            "counts": True,
            "primary_key_sets": True,
            "checksums": True,
            "foreign_keys": True,
            "uniqueness": True,
            "rls": True,
            "sentinel_unchanged": True,
        },
        "tables": {
            "dashboards": {
                "source_count": 1,
                "destination_count": 1,
                "source_primary_key_sha256": "a" * 64,
                "destination_primary_key_sha256": "a" * 64,
                "source_row_sha256": "b" * 64,
                "destination_row_sha256": "b" * 64,
            }
        },
    }
    cutover_bytes = (
        json.dumps(
            cutover_report,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    smoke_report: dict[str, object] = {
        "operation": "permanent-runtime-smoke",
        "result": "passed",
        "export_reference": "tenant-export/42",
        "evidence_reference": "smoke-evidence/42",
        "cutover_report_sha256": sha256(cutover_bytes).hexdigest(),
        "approved_source_resources": {
            "schema": source_schema,
            "role": source_role,
            "secret": source_secret,
        },
        "checks": {
            "master_login": True,
            "organisation_read": True,
            "organisation_write": True,
            "integrations": True,
            "bots": True,
            "agents": True,
            "jobs": True,
            "rls_isolation": True,
            "permanent_runtime": True,
        },
    }
    arguments = {
        "source_schema": source_schema,
        "source_role": source_role,
        "source_secret": source_secret,
        "apply": True,
    }
    return arguments, cutover_report, smoke_report


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ({"apply": False}, "apply_required"),
        ({"source_schema": "org_*"}, "exact_source_target_required"),
        ({"source_schema": "org_"}, "exact_source_target_required"),
        ({"source_role": "org_"}, "exact_source_target_required"),
        ({"source_secret": "gtm-"}, "exact_source_target_required"),
    ],
)
def test_source_deletion_refuses_inexact_or_unapproved_targets(
    mutation: dict[str, object],
    error_code: str,
) -> None:
    """CLI targets must exactly equal the organisation-bound approved manifest."""

    arguments, cutover_report, smoke_report = _valid_deletion_evidence()
    arguments.update(mutation)

    with pytest.raises(CutoverError) as exc_info:
        cutover_cli.validate_source_deletion_evidence(
            arguments=arguments,
            cutover_report=cutover_report,
            smoke_report=smoke_report,
            cutover_report_sha256=str(smoke_report["cutover_report_sha256"]),
        )

    assert exc_info.value.code == error_code


@pytest.mark.parametrize(
    ("report_name", "field", "value", "error_code"),
    [
        ("cutover", "backup_reference", "", "backup_reference_required"),
        (
            "cutover",
            "writers_stopped_evidence",
            "",
            "copy_validation_evidence_incomplete",
        ),
        ("smoke", "export_reference", "", "export_reference_required"),
        (
            "smoke",
            "evidence_reference",
            "",
            "runtime_smoke_evidence_incomplete",
        ),
        ("cutover", "result", "failed", "copy_validation_evidence_incomplete"),
        ("smoke", "result", "failed", "runtime_smoke_evidence_incomplete"),
        (
            "smoke",
            "cutover_report_sha256",
            "0" * 64,
            "cutover_report_hash_mismatch",
        ),
    ],
)
def test_source_deletion_requires_complete_linked_cutover_and_smoke_evidence(
    report_name: str,
    field: str,
    value: object,
    error_code: str,
) -> None:
    """Backup, export, parity, and live-runtime proof are independent gates."""

    arguments, cutover_report, smoke_report = _valid_deletion_evidence()
    actual_cutover_hash = str(smoke_report["cutover_report_sha256"])
    target = cutover_report if report_name == "cutover" else smoke_report
    target[field] = value

    with pytest.raises(CutoverError) as exc_info:
        cutover_cli.validate_source_deletion_evidence(
            arguments=arguments,
            cutover_report=cutover_report,
            smoke_report=smoke_report,
            cutover_report_sha256=actual_cutover_hash,
        )

    assert exc_info.value.code == error_code


def test_source_deletion_rejects_missing_table_digest_fields() -> None:
    """Equal missing values are not valid copy-parity evidence."""

    arguments, cutover_report, smoke_report = _valid_deletion_evidence()
    table = cast(dict[str, object], cast(dict[str, object], cutover_report["tables"])["dashboards"])
    table.pop("source_row_sha256")
    table.pop("destination_row_sha256")

    with pytest.raises(CutoverError) as exc_info:
        cutover_cli.validate_source_deletion_evidence(
            arguments=arguments,
            cutover_report=cutover_report,
            smoke_report=smoke_report,
            cutover_report_sha256=str(smoke_report["cutover_report_sha256"]),
        )

    assert exc_info.value.code == "copy_validation_evidence_incomplete"


@pytest.mark.parametrize(
    ("apply", "schema_suffix", "role_suffix", "error_code"),
    [
        (False, "", "", "apply_required"),
        (True, "_other", "", "exact_source_target_required"),
        (True, "", "_other", "exact_source_target_required"),
    ],
)
def test_database_source_deletion_revalidates_exact_targets_before_database_io(
    apply: bool,
    schema_suffix: str,
    role_suffix: str,
    error_code: str,
) -> None:
    """The database service must remain safe when called without the CLI."""

    organisation_id = uuid4()
    with pytest.raises(CutoverError) as exc_info:
        _service_without_database().delete_source_database_resources(
            organisation_id,
            source_schema=get_schema_name(organisation_id) + schema_suffix,
            source_role=get_role_name(organisation_id) + role_suffix,
            apply=apply,
        )

    assert exc_info.value.code == error_code


def test_source_deletion_orders_database_before_verified_secret_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The external secret is removed only after the database transaction commits."""

    arguments, cutover_report, smoke_report = _valid_deletion_evidence()
    cutover_path = tmp_path / "cutover.json"
    smoke_path = tmp_path / "smoke.json"
    cutover_cli.write_immutable_report(cutover_path, cutover_report)
    smoke_report["cutover_report_sha256"] = sha256(cutover_path.read_bytes()).hexdigest()
    cutover_cli.write_immutable_report(smoke_path, smoke_report)
    events: list[str] = []

    class _FakeService:
        """Record the database phase without replacing its ordering contract."""

        def __init__(self, _session: object, *, expected_revision: str) -> None:
            assert expected_revision == "head"

        def delete_source_database_resources(
            self,
            _organisation_id: object,
            *,
            source_schema: str,
            source_role: str,
            apply: bool,
        ) -> dict[str, object]:
            """Return the verified database post-state used by the orchestrator."""

            assert source_schema == arguments["source_schema"]
            assert source_role == arguments["source_role"]
            assert apply is True
            events.append("database")
            return {"schema_absent": True, "role_absent": True}

    class _FakePoller:
        """Model the completion boundary of Azure's long-running delete."""

        def wait(self) -> None:
            """Record that deletion was awaited before verification."""

            events.append("secret")

    class _FakeSecretClient:
        """Expose the active secret once, then prove it is absent."""

        def __init__(self) -> None:
            self.reads = 0

        def get_secret(self, _name: str) -> object:
            """Return the secret before deletion and not-found afterward."""

            from azure.core.exceptions import ResourceNotFoundError

            self.reads += 1
            if self.reads > 1:
                raise ResourceNotFoundError("deleted")
            return object()

        def begin_delete_secret(self, _name: str) -> _FakePoller:
            """Require the committed database phase before external deletion."""

            assert events == ["database"]
            return _FakePoller()

    @contextmanager
    def _session_scope():
        yield object()

    monkeypatch.setattr(cutover_cli, "SharedTenancyCutoverService", _FakeService)
    monkeypatch.setattr(cutover_cli, "master_session_scope", _session_scope)
    monkeypatch.setattr(cutover_cli, "current_org_migration_head", lambda: "head")
    monkeypatch.setattr(
        cutover_cli,
        "build_master_secret_client_sync",
        lambda **_kwargs: _FakeSecretClient(),
    )
    args = Namespace(
        command="delete-source-resources",
        cutover_report=cutover_path,
        smoke_report=smoke_path,
        **arguments,
    )

    report = cutover_cli.run_source_deletion(args)

    assert events == ["database", "secret"]
    assert report["result"] == "source_resources_deleted"
    assert report["validation"] == {
        "schema_absent": True,
        "role_absent": True,
        "secret_absent": True,
    }
    assert report["cutover_report_sha256"] == smoke_report["cutover_report_sha256"]


def test_source_deletion_refuses_to_claim_success_when_secret_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed delete poller is insufficient until active-secret lookup fails."""

    class _SecretStillPresent:
        """Model a provider that completes its poller but keeps the secret active."""

        def get_secret(self, _name: str) -> object:
            """Keep returning the secret so absence verification fails."""

            return object()

        def begin_delete_secret(self, _name: str) -> object:
            """Return a completed poller without changing provider state."""

            return type("Poller", (), {"wait": lambda self: None})()

    with pytest.raises(CutoverError) as exc_info:
        cutover_cli.delete_and_verify_secret(
            _SecretStillPresent(),
            "gtm-source-db-password",
        )

    assert exc_info.value.code == "source_secret_deletion_verification_failed"


def test_delete_cli_writes_hash_linked_companion_without_rewriting_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion evidence is append-only: the original cutover JSON never changes."""

    arguments, cutover_report, _smoke_report = _valid_deletion_evidence()
    cutover_path = tmp_path / "cutover.json"
    smoke_path = tmp_path / "smoke.json"
    cutover_cli.write_immutable_report(cutover_path, cutover_report)
    smoke_path.write_text("{}", encoding="utf-8")
    original_cutover = cutover_path.read_bytes()

    def _delete(_args: Namespace, *, report_id: str) -> dict[str, object]:
        return {
            "report_id": report_id,
            "operation": "delete-source-resources",
            "result": "source_resources_deleted",
        }

    monkeypatch.setattr(cutover_cli, "run_source_deletion", _delete)

    exit_code = cutover_cli.main(
        [
            "delete-source-resources",
            "--source-schema",
            str(arguments["source_schema"]),
            "--source-role",
            str(arguments["source_role"]),
            "--source-secret",
            str(arguments["source_secret"]),
            "--cutover-report",
            str(cutover_path),
            "--smoke-report",
            str(smoke_path),
            "--apply",
        ]
    )

    companions = list(tmp_path.glob("cutover.source-deletion.*.json"))
    assert exit_code == 0
    assert len(companions) == 1
    assert json.loads(companions[0].read_text(encoding="utf-8"))["result"] == (
        "source_resources_deleted"
    )
    assert cutover_path.read_bytes() == original_cutover


def test_delete_cli_records_committed_database_cleanup_when_secret_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-system partial failure must not be reported as a full rollback."""

    arguments, cutover_report, _smoke_report = _valid_deletion_evidence()
    cutover_path = tmp_path / "cutover.json"
    smoke_path = tmp_path / "smoke.json"
    cutover_cli.write_immutable_report(cutover_path, cutover_report)
    smoke_path.write_text("{}", encoding="utf-8")

    def _fail(_args: Namespace, *, report_id: str) -> dict[str, object]:
        raise cutover_cli.SourceDeletionError(
            "source_secret_deletion_failed",
            "private provider detail",
            database_result={"schema_absent": True, "role_absent": True},
        )

    monkeypatch.setattr(cutover_cli, "run_source_deletion", _fail)

    exit_code = cutover_cli.main(
        [
            "delete-source-resources",
            "--source-schema",
            str(arguments["source_schema"]),
            "--source-role",
            str(arguments["source_role"]),
            "--source-secret",
            str(arguments["source_secret"]),
            "--cutover-report",
            str(cutover_path),
            "--smoke-report",
            str(smoke_path),
            "--apply",
        ]
    )

    companion = next(tmp_path.glob("cutover.source-deletion.*.json"))
    payload = companion.read_text(encoding="utf-8")
    report = json.loads(payload)
    assert exit_code == 1
    assert report["database_changes_committed"] is True
    assert report["database"] == {"schema_absent": True, "role_absent": True}
    assert report["error_code"] == "source_secret_deletion_failed"
    assert "private provider detail" not in payload
