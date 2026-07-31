"""Contracts for tenant export, deletion, restore, and parity verification."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from src.service.orgnisation.shared_tenancy_cutover_service import CutoverError
from src.service.orgnisation.shared_tenancy_lifecycle_service import (
    SharedTenancyLifecycleService,
)

from scripts.maintenance import manage_shared_tenant_data as lifecycle_cli


def _service_without_database() -> SharedTenancyLifecycleService:
    """Build a service for mutation gates that must run before database access."""

    return SharedTenancyLifecycleService(
        cast(Session, object()),
        expected_revision="test_head",
    )


def test_delete_apply_requires_state_bound_approval_before_database_io() -> None:
    """A mutating tenant delete cannot run from an accidental bare command."""

    with pytest.raises(CutoverError) as exc_info:
        _service_without_database().delete(
            uuid4(),
            approval_token="",
            apply=True,
        )

    assert exc_info.value.code == "approval_token_required"


def test_restore_requires_apply_before_opening_archive(tmp_path: Path) -> None:
    """Restore is always an explicit mutation, even when the target is empty."""

    with pytest.raises(CutoverError) as exc_info:
        _service_without_database().restore(
            uuid4(),
            manifest_path=tmp_path / "missing.zip",
            replacement_approval_token=None,
            apply=False,
        )

    assert exc_info.value.code == "apply_required"


def test_cli_exposes_only_tenant_data_lifecycle_operations() -> None:
    """The lifecycle tool must not expose schema, role, or routing operations."""

    parser = lifecycle_cli.build_parser()

    assert set(parser._subparsers._group_actions[0].choices) == {  # noqa: SLF001
        "export",
        "delete",
        "restore",
        "verify",
    }


def test_cli_routes_export_through_existing_master_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI must reuse the established master-session maintenance boundary."""

    events: list[str] = []

    class _FakeService:
        """Record the selected operation without accessing a database."""

        def __init__(self, _session: object, *, expected_revision: str) -> None:
            assert expected_revision == "head"

        def export(self, _organisation_id: object, *, destination: Path) -> dict[str, str]:
            """Return one structured export result for the CLI."""

            events.append(str(destination))
            return {"result": "exported"}

    class _SessionScope:
        """Provide the context-manager protocol used by master sessions."""

        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(lifecycle_cli, "SharedTenancyLifecycleService", _FakeService)
    monkeypatch.setattr(lifecycle_cli, "master_session_scope", _SessionScope)
    monkeypatch.setattr(lifecycle_cli, "current_org_migration_head", lambda: "head")
    destination = tmp_path / "tenant.zip"
    args = lifecycle_cli.build_parser().parse_args(
        [
            "export",
            "--organisation-id",
            str(uuid4()),
            "--destination",
            str(destination),
        ]
    )

    report = lifecycle_cli.run(args)

    assert report == {"result": "exported"}
    assert events == [str(destination)]
