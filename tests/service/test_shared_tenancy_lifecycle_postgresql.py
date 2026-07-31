"""Real-PostgreSQL proof for tenant_data export, delete, restore, and verify."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from src.service.orgnisation.shared_tenancy_cutover_service import CutoverError
from src.service.orgnisation.shared_tenancy_lifecycle_service import (
    SharedTenancyLifecycleService,
)

ADMIN_URL = os.getenv("TENANT_RLS_TEST_ADMIN_URL")
RUNTIME_URL = os.getenv("TENANT_RLS_TEST_RUNTIME_URL")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL or not RUNTIME_URL,
    reason="set both tenant RLS test URLs to a disposable migrated database",
)


def test_export_delete_restore_and_verify_leave_other_tenant_unchanged(tmp_path) -> None:  # noqa: ANN001
    """A complete lifecycle for A must preserve B byte-for-byte."""

    assert ADMIN_URL is not None
    assert RUNTIME_URL is not None
    engine = create_engine(ADMIN_URL)
    runtime_engine = create_engine(RUNTIME_URL)
    organisation_a = uuid4()
    organisation_b = uuid4()
    dashboard_a = uuid4()
    dashboard_b = uuid4()
    archive_path = tmp_path / "tenant-a.zip"

    try:
        with engine.begin() as connection:
            revision = str(
                connection.execute(
                    text("SELECT version_num FROM tenant_data.alembic_version")
                ).scalar_one()
            )
            for organisation_id, name in (
                (organisation_a, "Lifecycle A"),
                (organisation_b, "Lifecycle B"),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO public.organisations (
                            organisation_id,
                            organisation_name,
                            is_active,
                            status
                        ) VALUES (
                            :organisation_id,
                            :organisation_name,
                            true,
                            'PENDING'
                        )
                        """
                    ),
                    {
                        "organisation_id": str(organisation_id),
                        "organisation_name": f"{name} {organisation_id}",
                    },
                )
            for organisation_id, dashboard_id, dashboard_name in (
                (organisation_a, dashboard_a, "Pipeline A"),
                (organisation_b, dashboard_b, "Pipeline B"),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO tenant_data.dashboards (
                            dashboard_id,
                            organisation_id,
                            dashboard_key,
                            dashboard_name,
                            dashboard_type
                        ) VALUES (
                            :dashboard_id,
                            :organisation_id,
                            'pipeline',
                            :dashboard_name,
                            'crm'
                        )
                        """
                    ),
                    {
                        "dashboard_id": str(dashboard_id),
                        "organisation_id": str(organisation_id),
                        "dashboard_name": dashboard_name,
                    },
                )

        with Session(engine) as session:
            export_report = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).export(organisation_a, destination=archive_path)
        assert export_report["result"] == "exported"
        assert export_report["table_count"] == 74
        assert export_report["tables"]["dashboards"]["row_count"] == 1

        with Session(engine) as session:
            preview = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).delete(organisation_a, approval_token=None, apply=False)
        assert preview["result"] == "approval_required"
        assert len(preview["approval_token"]) == 64

        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).delete(organisation_a, approval_token="0" * 64, apply=True)
        assert exc_info.value.code == "approval_token_mismatch"

        with Session(engine) as session:
            deleted = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).delete(
                organisation_a,
                approval_token=preview["approval_token"],
                apply=True,
            )
        assert deleted["result"] == "deleted"
        assert deleted["validation"]["target_absent"] is True
        assert deleted["validation"]["other_tenants_unchanged"] is True

        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).verify(organisation_a, manifest_path=archive_path)
        assert exc_info.value.code == "manifest_database_mismatch"

        with Session(engine) as session:
            restored = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).restore(
                organisation_a,
                manifest_path=archive_path,
                replacement_approval_token=None,
                apply=True,
            )
        assert restored["result"] == "restored"
        assert restored["validation"]["parity"] is True
        assert restored["validation"]["other_tenants_unchanged"] is True

        with Session(engine) as session:
            verified = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).verify(organisation_a, manifest_path=archive_path)
        assert verified["result"] == "verified"

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE tenant_data.dashboards "
                    "SET dashboard_name = 'Changed A' "
                    "WHERE organisation_id = :organisation_id"
                ),
                {"organisation_id": str(organisation_a)},
            )
        with Session(engine) as session:
            replacement_preview = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).delete(organisation_a, approval_token=None, apply=False)
        tampered_archive = tmp_path / "tenant-a-wrong-identity.zip"
        with zipfile.ZipFile(archive_path, mode="r") as source:
            members = {name: source.read(name) for name in source.namelist()}
        dashboard_member = "tables/dashboards.jsonl"
        dashboard_row = json.loads(members[dashboard_member])
        dashboard_row["organisation_id"] = str(organisation_b)
        members[dashboard_member] = (
            json.dumps(
                dashboard_row,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        manifest = json.loads(members["manifest.json"])
        manifest["tables"]["dashboards"]["archive_file_sha256"] = hashlib.sha256(
            members[dashboard_member]
        ).hexdigest()
        members["manifest.json"] = (
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        with zipfile.ZipFile(
            tampered_archive,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
        ) as target:
            for name, payload in members.items():
                target.writestr(name, payload)

        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).restore(
                organisation_a,
                manifest_path=tampered_archive,
                replacement_approval_token=replacement_preview["approval_token"],
                apply=True,
            )
        assert exc_info.value.code == "archive_row_identity_mismatch"

        with engine.connect() as connection:
            names_after_failed_restore = dict(
                connection.execute(
                    text(
                        "SELECT organisation_id::text, dashboard_name "
                        "FROM tenant_data.dashboards "
                        "WHERE organisation_id IN (:organisation_a, :organisation_b)"
                    ),
                    {
                        "organisation_a": str(organisation_a),
                        "organisation_b": str(organisation_b),
                    },
                ).all()
            )
        assert names_after_failed_restore == {
            str(organisation_a): "Changed A",
            str(organisation_b): "Pipeline B",
        }

        with Session(engine) as session:
            replaced = SharedTenancyLifecycleService(
                session,
                expected_revision=revision,
            ).restore(
                organisation_a,
                manifest_path=archive_path,
                replacement_approval_token=replacement_preview["approval_token"],
                apply=True,
            )
        assert replaced["replaced_existing"] is True

        with engine.connect() as connection:
            names = dict(
                connection.execute(
                    text(
                        "SELECT organisation_id::text, dashboard_name "
                        "FROM tenant_data.dashboards "
                        "WHERE organisation_id IN (:organisation_a, :organisation_b)"
                    ),
                    {
                        "organisation_a": str(organisation_a),
                        "organisation_b": str(organisation_b),
                    },
                ).all()
            )
        assert names == {
            str(organisation_a): "Pipeline A",
            str(organisation_b): "Pipeline B",
        }

        for organisation_id, expected_name in (
            (organisation_a, "Pipeline A"),
            (organisation_b, "Pipeline B"),
        ):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text(
                        "SELECT set_config('app.current_organisation_id', :organisation_id, true)"
                    ),
                    {"organisation_id": str(organisation_id)},
                )
                visible_names = connection.execute(
                    text(
                        "SELECT dashboard_name FROM tenant_data.dashboards ORDER BY dashboard_name"
                    )
                ).scalars()
                assert list(visible_names) == [expected_name]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM tenant_data.dashboards "
                    "WHERE organisation_id IN (:organisation_a, :organisation_b)"
                ),
                {
                    "organisation_a": str(organisation_a),
                    "organisation_b": str(organisation_b),
                },
            )
            connection.execute(
                text(
                    "DELETE FROM public.organisations "
                    "WHERE organisation_id IN (:organisation_a, :organisation_b)"
                ),
                {
                    "organisation_a": str(organisation_a),
                    "organisation_b": str(organisation_b),
                },
            )
        engine.dispose()
        runtime_engine.dispose()
