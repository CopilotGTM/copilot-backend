"""Tenant-scoped backup and recovery inside the permanent `tenant_data` schema.

The maintenance CLI calls this service with an exact organisation UUID. It
reuses the cutover service's reflected table inventory, FK ordering, checksums,
RLS readiness gate, and cross-tenant sentinel logic. Archives are ZIP files
containing one JSONL payload per table plus a hash-bearing manifest.
"""

# ruff: noqa: S608

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from migrations.org_migrations.tenant_identity import validate_migration_schema_name
from sqlalchemy import Table, text
from sqlalchemy.exc import SQLAlchemyError
from src.service.orgnisation.shared_tenancy_cutover_service import (
    CUTOVER_TOOL_VERSION,
    CutoverError,
    SharedTenancyCutoverService,
    _SchemaInventory,
)

ARCHIVE_FORMAT_VERSION = 1
_MANIFEST_MEMBER = "manifest.json"
_ROW_BATCH_SIZE = 250
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class SharedTenancyLifecycleService(SharedTenancyCutoverService):
    """Export, delete, restore, and verify one tenant without recreating schemas."""

    def export(self, organisation_id: UUID, *, destination: Path) -> dict[str, Any]:
        """Create one immutable, streaming tenant archive from a repeatable snapshot."""

        if destination.exists():
            raise CutoverError("archive_exists", "export destination already exists")

        started = time.perf_counter()
        archive_created = False
        try:
            with self._session.begin():
                self._session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                inventory, table_order, deferred = self._prepare(organisation_id)
                manifest = self._base_manifest(
                    organisation_id,
                    inventory,
                    table_order,
                    deferred,
                )
                with zipfile.ZipFile(
                    destination,
                    mode="x",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive_created = True
                    for table_name in table_order:
                        table = inventory.tables[table_name]
                        row_count, key_hash, row_hash = self._table_digest(
                            table,
                            organisation_id,
                            include_target=True,
                        )
                        member = _table_member(table_name)
                        exported_count, file_hash = self._write_table_member(
                            archive,
                            member,
                            table,
                            organisation_id,
                        )
                        if exported_count != row_count:
                            raise CutoverError(
                                "export_snapshot_changed",
                                f"row count changed while exporting table {table_name}",
                            )
                        manifest["tables"][table_name] = {
                            "member": member,
                            "primary_key_columns": [
                                column.name for column in table.primary_key.columns
                            ],
                            "row_count": row_count,
                            "primary_key_sha256": key_hash,
                            "row_sha256": row_hash,
                            "archive_file_sha256": file_hash,
                        }
                    archive.writestr(
                        _MANIFEST_MEMBER,
                        _canonical_json_bytes(manifest),
                    )
            return {
                "operation": "export",
                "result": "exported",
                "organisation_id": str(organisation_id),
                "destination": str(destination),
                "archive_sha256": _file_sha256(destination),
                "revision": inventory.revision,
                "table_count": len(table_order),
                "tables": manifest["tables"],
                "timings_ms": {
                    "total": _elapsed_ms(started),
                },
            }
        except CutoverError:
            if archive_created:
                destination.unlink(missing_ok=True)
            raise
        except (OSError, SQLAlchemyError, zipfile.BadZipFile) as exc:
            if archive_created:
                destination.unlink(missing_ok=True)
            raise CutoverError(
                "export_failed",
                "tenant export failed without leaving a usable archive",
            ) from exc

    def delete(
        self,
        organisation_id: UUID,
        *,
        approval_token: str | None,
        apply: bool,
    ) -> dict[str, Any]:
        """Preview or apply an exact-tenant deletion bound to the current row state."""

        if apply and not (approval_token or "").strip():
            raise CutoverError(
                "approval_token_required",
                "a dry-run approval token is required for tenant deletion",
            )

        started = time.perf_counter()
        try:
            with self._session.begin():
                if not apply:
                    self._session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                inventory, table_order, deferred = self._prepare(organisation_id)
                tables = self._state_reports(inventory, table_order, organisation_id)
                current_token = self._approval_token(
                    organisation_id,
                    inventory.revision,
                    tables,
                )
                if not apply:
                    return {
                        "operation": "delete",
                        "result": "approval_required",
                        "organisation_id": str(organisation_id),
                        "revision": inventory.revision,
                        "approval_token": current_token,
                        "mutation_performed": False,
                        "tables": tables,
                        "timings_ms": {
                            "total": _elapsed_ms(started),
                        },
                    }
                if approval_token != current_token:
                    raise CutoverError(
                        "approval_token_mismatch",
                        "tenant rows changed after deletion approval",
                    )

                sentinel_before = self._sentinel_snapshot(inventory, organisation_id)
                self._delete_tenant_rows(
                    inventory,
                    table_order,
                    deferred,
                    organisation_id,
                )
                self._validate_target_empty(inventory, organisation_id)
                sentinel_after = self._sentinel_snapshot(inventory, organisation_id)
                if sentinel_before != sentinel_after:
                    raise CutoverError(
                        "sentinel_changed",
                        "another organisation changed during tenant deletion",
                    )
            return {
                "operation": "delete",
                "result": "deleted",
                "organisation_id": str(organisation_id),
                "revision": inventory.revision,
                "approval_token": current_token,
                "mutation_performed": True,
                "tables": tables,
                "validation": {
                    "target_absent": True,
                    "other_tenants_unchanged": True,
                },
                "timings_ms": {
                    "total": _elapsed_ms(started),
                },
            }
        except CutoverError:
            raise
        except SQLAlchemyError as exc:
            raise CutoverError(
                "tenant_delete_failed",
                "tenant deletion failed and was rolled back",
            ) from exc

    def restore(
        self,
        organisation_id: UUID,
        *,
        manifest_path: Path,
        replacement_approval_token: str | None,
        apply: bool,
    ) -> dict[str, Any]:
        """Restore one archive into an empty or explicitly approved tenant target."""

        if not apply:
            raise CutoverError("apply_required", "--apply is required")
        manifest = self._read_and_validate_archive(manifest_path)
        self._validate_manifest_identity(manifest, organisation_id)

        started = time.perf_counter()
        try:
            with self._session.begin():
                inventory, table_order, deferred = self._prepare(organisation_id)
                self._validate_manifest_inventory(manifest, inventory, table_order)
                current_tables = self._state_reports(
                    inventory,
                    table_order,
                    organisation_id,
                )
                target_has_rows = any(table["row_count"] for table in current_tables.values())
                replaced_existing = False
                if target_has_rows:
                    if not replacement_approval_token:
                        raise CutoverError(
                            "restore_target_not_empty",
                            "restore target contains rows; replacement approval is required",
                        )
                    current_token = self._approval_token(
                        organisation_id,
                        inventory.revision,
                        current_tables,
                    )
                    if replacement_approval_token != current_token:
                        raise CutoverError(
                            "replacement_approval_token_mismatch",
                            "tenant rows changed after replacement approval",
                        )

                sentinel_before = self._sentinel_snapshot(inventory, organisation_id)
                if target_has_rows:
                    self._delete_tenant_rows(
                        inventory,
                        table_order,
                        deferred,
                        organisation_id,
                    )
                    replaced_existing = True
                self._restore_archive_rows(
                    manifest_path,
                    manifest,
                    inventory,
                    table_order,
                    deferred,
                    organisation_id,
                )
                self._validate_manifest_database(
                    manifest,
                    inventory,
                    table_order,
                    organisation_id,
                )
                self._validate_foreign_keys(inventory, organisation_id)
                self._validate_uniqueness(inventory, organisation_id)
                self._validate_destination_rls()
                sentinel_after = self._sentinel_snapshot(inventory, organisation_id)
                if sentinel_before != sentinel_after:
                    raise CutoverError(
                        "sentinel_changed",
                        "another organisation changed during tenant restore",
                    )
            return {
                "operation": "restore",
                "result": "restored",
                "organisation_id": str(organisation_id),
                "archive": str(manifest_path),
                "archive_sha256": _file_sha256(manifest_path),
                "revision": inventory.revision,
                "replaced_existing": replaced_existing,
                "validation": {
                    "parity": True,
                    "foreign_keys": True,
                    "uniqueness": True,
                    "rls": True,
                    "other_tenants_unchanged": True,
                },
                "timings_ms": {
                    "total": _elapsed_ms(started),
                },
            }
        except CutoverError:
            raise
        except (OSError, SQLAlchemyError, zipfile.BadZipFile) as exc:
            raise CutoverError(
                "restore_failed",
                "tenant restore failed and all database changes were rolled back",
            ) from exc

    def verify(
        self,
        organisation_id: UUID,
        *,
        manifest_path: Path,
    ) -> dict[str, Any]:
        """Compare one tenant's live database state with a validated archive."""

        manifest = self._read_and_validate_archive(manifest_path)
        self._validate_manifest_identity(manifest, organisation_id)
        started = time.perf_counter()
        try:
            with self._session.begin():
                self._session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                inventory, table_order, _deferred = self._prepare(organisation_id)
                self._validate_manifest_inventory(manifest, inventory, table_order)
                self._validate_manifest_database(
                    manifest,
                    inventory,
                    table_order,
                    organisation_id,
                )
                self._validate_foreign_keys(inventory, organisation_id)
                self._validate_uniqueness(inventory, organisation_id)
            return {
                "operation": "verify",
                "result": "verified",
                "organisation_id": str(organisation_id),
                "archive": str(manifest_path),
                "archive_sha256": _file_sha256(manifest_path),
                "revision": inventory.revision,
                "validation": {
                    "parity": True,
                    "foreign_keys": True,
                    "uniqueness": True,
                    "rls": True,
                },
                "timings_ms": {
                    "total": _elapsed_ms(started),
                },
            }
        except CutoverError:
            raise
        except SQLAlchemyError as exc:
            raise CutoverError(
                "tenant_verify_failed",
                "tenant archive verification failed",
            ) from exc

    def _prepare(
        self,
        organisation_id: UUID,
    ) -> tuple[_SchemaInventory, tuple[str, ...], tuple[tuple[str, str], ...]]:
        if not isinstance(organisation_id, UUID):
            raise CutoverError("invalid_organisation_id", "organisation_id must be a UUID")
        self._acquire_lock(organisation_id)
        self._session.execute(text("SET LOCAL row_security = off"))
        self._set_tenant_context(organisation_id)
        inventory = self._inventory_schema(self._destination_schema)
        if inventory.revision != self._expected_revision:
            raise CutoverError(
                "migration_revision_mismatch",
                "tenant_data and code migration revisions must match",
            )
        self._validate_destination_rls()
        table_order, deferred = self._copy_plan(inventory)
        return inventory, table_order, deferred

    def _base_manifest(
        self,
        organisation_id: UUID,
        inventory: _SchemaInventory,
        table_order: tuple[str, ...],
        deferred: tuple[tuple[str, str], ...],
    ) -> dict[str, Any]:
        return {
            "format": "copilotgtm-tenant-data-archive",
            "format_version": ARCHIVE_FORMAT_VERSION,
            "tool_version": CUTOVER_TOOL_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "organisation_id": str(organisation_id),
            "schema": inventory.schema,
            "revision": inventory.revision,
            "table_order": list(table_order),
            "deferred_foreign_keys": [
                {"table": table_name, "constraint": constraint_name}
                for table_name, constraint_name in deferred
            ],
            "tables": {},
        }

    def _write_table_member(
        self,
        archive: zipfile.ZipFile,
        member: str,
        table: Table,
        organisation_id: UUID,
    ) -> tuple[int, str]:
        alias = "export_row"
        key_expression = ", ".join(
            f"{alias}.{_quote_identifier(column.name)}" for column in table.primary_key.columns
        )
        # Reflected identifiers are validated; the organisation value remains bound.
        query = text(
            f"SELECT to_jsonb({alias}) AS row_json "  # nosec B608
            f"FROM {_qualified(str(table.schema), table.name)} AS {alias} "
            f"WHERE {alias}.organisation_id = :organisation_id "
            f"ORDER BY {key_expression}"
        ).execution_options(stream_results=True, yield_per=500)
        digest = hashlib.sha256()
        count = 0
        with archive.open(member, mode="w") as target:
            for row in self._session.execute(
                query,
                {"organisation_id": str(organisation_id)},
            ):
                encoded = _canonical_json_bytes(row.row_json)
                target.write(encoded)
                digest.update(encoded)
                count += 1
        return count, digest.hexdigest()

    def _state_reports(
        self,
        inventory: _SchemaInventory,
        table_order: tuple[str, ...],
        organisation_id: UUID,
    ) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for table_name in table_order:
            table = inventory.tables[table_name]
            row_count, key_hash, row_hash = self._table_digest(
                table,
                organisation_id,
                include_target=True,
            )
            reports[table_name] = {
                "row_count": row_count,
                "primary_key_sha256": key_hash,
                "row_sha256": row_hash,
            }
        return reports

    def _approval_token(
        self,
        organisation_id: UUID,
        revision: str,
        tables: dict[str, dict[str, Any]],
    ) -> str:
        payload = {
            "operation": "delete-tenant-data",
            "organisation_id": str(organisation_id),
            "revision": revision,
            "tables": tables,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _delete_tenant_rows(
        self,
        inventory: _SchemaInventory,
        table_order: tuple[str, ...],
        deferred: tuple[tuple[str, str], ...],
        organisation_id: UUID,
    ) -> None:
        for table_name in sorted({item[0] for item in deferred}):
            table = inventory.tables[table_name]
            deferred_columns = self._deferred_column_names(
                table,
                deferred,
            )
            if deferred_columns:
                self._session.execute(
                    table.update()
                    .where(table.c.organisation_id == organisation_id)
                    .values(dict.fromkeys(deferred_columns))
                )
        for table_name in reversed(table_order):
            table = inventory.tables[table_name]
            self._session.execute(table.delete().where(table.c.organisation_id == organisation_id))

    def _deferred_column_names(
        self,
        table: Table,
        deferred: tuple[tuple[str, str], ...],
    ) -> set[str]:
        constraint_names = {
            constraint_name for table_name, constraint_name in deferred if table_name == table.name
        }
        return {
            element.parent.name
            for constraint in table.foreign_key_constraints
            if (constraint.name or "") in constraint_names
            for element in constraint.elements
            if element.parent.name != "organisation_id"
        }

    def _validate_target_empty(
        self,
        inventory: _SchemaInventory,
        organisation_id: UUID,
    ) -> None:
        for table_name, table in inventory.tables.items():
            # Inventory identifiers are validated; the organisation value remains bound.
            remaining = self._session.execute(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM "  # nosec B608
                    f"{_qualified(inventory.schema, table_name)} "
                    "WHERE organisation_id = :organisation_id)"
                ),
                {"organisation_id": str(organisation_id)},
            ).scalar_one()
            if remaining:
                raise CutoverError(
                    "tenant_delete_validation_failed",
                    f"tenant rows remain in table {table.name}",
                )

    def _read_and_validate_archive(self, path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path, mode="r") as archive:
                members = archive.namelist()
                if len(members) != len(set(members)) or _MANIFEST_MEMBER not in members:
                    raise CutoverError(
                        "invalid_manifest",
                        "archive has duplicate members or no manifest",
                    )
                manifest_info = archive.getinfo(_MANIFEST_MEMBER)
                if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                    raise CutoverError("invalid_manifest", "archive manifest is too large")
                manifest = json.loads(archive.read(_MANIFEST_MEMBER))
                if not isinstance(manifest, dict):
                    raise CutoverError(
                        "invalid_manifest",
                        "archive manifest must be a JSON object",
                    )
                tables = manifest.get("tables")
                if not isinstance(tables, dict) or not tables:
                    raise CutoverError(
                        "invalid_manifest",
                        "archive manifest has no table inventory",
                    )
                expected_members = {_MANIFEST_MEMBER}
                for table_name, table_report in tables.items():
                    validate_migration_schema_name(str(table_name))
                    if not isinstance(table_report, dict):
                        raise CutoverError(
                            "invalid_manifest",
                            "archive table report is invalid",
                        )
                    member = table_report.get("member")
                    if member != _table_member(str(table_name)):
                        raise CutoverError(
                            "invalid_manifest",
                            "archive table member does not match its table",
                        )
                    expected_members.add(member)
                    digest = hashlib.sha256()
                    row_count = 0
                    with archive.open(member, mode="r") as source:
                        for line in source:
                            digest.update(line)
                            row_count += 1
                    if row_count != table_report.get(
                        "row_count"
                    ) or digest.hexdigest() != table_report.get("archive_file_sha256"):
                        raise CutoverError(
                            "archive_payload_mismatch",
                            f"archive payload differs for table {table_name}",
                        )
                if set(members) != expected_members:
                    raise CutoverError(
                        "invalid_manifest",
                        "archive contains unexpected members",
                    )
            return manifest
        except CutoverError:
            raise
        except (
            OSError,
            KeyError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ) as exc:
            raise CutoverError(
                "invalid_manifest",
                "tenant archive is missing or invalid",
            ) from exc

    def _validate_manifest_identity(
        self,
        manifest: dict[str, Any],
        organisation_id: UUID,
    ) -> None:
        if (
            manifest.get("format") != "copilotgtm-tenant-data-archive"
            or manifest.get("format_version") != ARCHIVE_FORMAT_VERSION
            or manifest.get("organisation_id") != str(organisation_id)
            or manifest.get("schema") != self._destination_schema
            or manifest.get("revision") != self._expected_revision
        ):
            raise CutoverError(
                "manifest_identity_mismatch",
                "archive identity, schema, format, or revision does not match",
            )

    def _validate_manifest_inventory(
        self,
        manifest: dict[str, Any],
        inventory: _SchemaInventory,
        table_order: tuple[str, ...],
    ) -> None:
        manifest_tables = manifest["tables"]
        if set(manifest_tables) != set(inventory.tables) or manifest.get("table_order") != list(
            table_order
        ):
            raise CutoverError(
                "manifest_schema_mismatch",
                "archive and tenant_data table inventories differ",
            )
        for table_name, table in inventory.tables.items():
            report = manifest_tables[table_name]
            if report.get("primary_key_columns") != [
                column.name for column in table.primary_key.columns
            ]:
                raise CutoverError(
                    "manifest_schema_mismatch",
                    f"primary key differs for table {table_name}",
                )

    def _validate_manifest_database(
        self,
        manifest: dict[str, Any],
        inventory: _SchemaInventory,
        table_order: tuple[str, ...],
        organisation_id: UUID,
    ) -> None:
        actual = self._state_reports(inventory, table_order, organisation_id)
        for table_name in table_order:
            expected = manifest["tables"][table_name]
            if actual[table_name] != {
                "row_count": expected.get("row_count"),
                "primary_key_sha256": expected.get("primary_key_sha256"),
                "row_sha256": expected.get("row_sha256"),
            }:
                raise CutoverError(
                    "manifest_database_mismatch",
                    f"database parity differs for table {table_name}",
                )

    def _restore_archive_rows(
        self,
        path: Path,
        manifest: dict[str, Any],
        inventory: _SchemaInventory,
        table_order: tuple[str, ...],
        deferred: tuple[tuple[str, str], ...],
        organisation_id: UUID,
    ) -> None:
        with zipfile.ZipFile(path, mode="r") as archive:
            for table_name in table_order:
                table = inventory.tables[table_name]
                deferred_columns = self._deferred_column_names(table, deferred)
                for rows in self._archive_row_batches(
                    archive,
                    manifest["tables"][table_name]["member"],
                    table,
                    organisation_id,
                ):
                    insert_rows = [dict(row) for row in rows]
                    for row in insert_rows:
                        for column in deferred_columns:
                            row[column] = None
                    self._insert_json_rows(table, insert_rows)

            # Cyclic nullable FKs are restored only after every referenced row exists.
            for table_name in sorted({item[0] for item in deferred}):
                table = inventory.tables[table_name]
                columns = self._deferred_column_names(table, deferred)
                for rows in self._archive_row_batches(
                    archive,
                    manifest["tables"][table_name]["member"],
                    table,
                    organisation_id,
                ):
                    self._restore_deferred_values(table, columns, rows, organisation_id)

    def _archive_row_batches(
        self,
        archive: zipfile.ZipFile,
        member: str,
        table: Table,
        organisation_id: UUID,
    ) -> Iterator[list[dict[str, Any]]]:
        expected_columns = set(table.c.keys())
        batch: list[dict[str, Any]] = []
        with archive.open(member, mode="r") as source:
            for line in source:
                row = json.loads(line)
                if (
                    not isinstance(row, dict)
                    or set(row) != expected_columns
                    or row.get("organisation_id") != str(organisation_id)
                ):
                    raise CutoverError(
                        "archive_row_identity_mismatch",
                        f"archive row does not match table {table.name}",
                    )
                batch.append(row)
                if len(batch) == _ROW_BATCH_SIZE:
                    yield batch
                    batch = []
        if batch:
            yield batch

    def _insert_json_rows(self, table: Table, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        columns = [column.name for column in table.columns if column.computed is None]
        column_sql = ", ".join(_quote_identifier(column) for column in columns)
        # Reflected identifiers are validated; row data remains a bound JSON value.
        self._session.execute(
            text(
                f"INSERT INTO {_qualified(str(table.schema), table.name)} "  # nosec B608
                f"({column_sql}) SELECT {column_sql} "
                "FROM jsonb_populate_recordset("
                f"NULL::{_qualified(str(table.schema), table.name)}, "
                "CAST(:rows AS jsonb))"
            ),
            {"rows": json.dumps(rows, separators=(",", ":"))},
        )

    def _restore_deferred_values(
        self,
        table: Table,
        columns: set[str],
        rows: list[dict[str, Any]],
        organisation_id: UUID,
    ) -> None:
        if not rows or not columns:
            return
        assignments = ", ".join(
            f"{_quote_identifier(column)} = source_rows.{_quote_identifier(column)}"
            for column in sorted(columns)
        )
        primary_key_match = " AND ".join(
            f"target.{_quote_identifier(column.name)} "
            f"= source_rows.{_quote_identifier(column.name)}"
            for column in table.primary_key.columns
        )
        # Reflected identifiers are validated; row and tenant data remain bound.
        self._session.execute(
            text(
                "WITH source_rows AS (SELECT * "
                "FROM jsonb_populate_recordset("
                f"NULL::{_qualified(str(table.schema), table.name)}, "
                "CAST(:rows AS jsonb))) "
                f"UPDATE {_qualified(str(table.schema), table.name)} AS target "  # nosec B608
                f"SET {assignments} FROM source_rows "
                "WHERE target.organisation_id = :organisation_id "
                f"AND {primary_key_match}"
            ),
            {
                "rows": json.dumps(rows, separators=(",", ":")),
                "organisation_id": str(organisation_id),
            },
        )


def _table_member(table_name: str) -> str:
    validate_migration_schema_name(table_name)
    return f"tables/{table_name}.jsonl"


def _quote_identifier(value: str) -> str:
    validate_migration_schema_name(value)
    return f'"{value}"'


def _qualified(schema: str, table: str) -> str:
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
