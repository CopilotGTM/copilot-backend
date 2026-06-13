from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from scripts import audit_duplicate_deal_contact_participants as audit


class _FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeSession:
    def __init__(self, *result_sets: list[dict[str, object]]) -> None:
        self._result_sets = list(result_sets)
        self.statements: list[str] = []
        self.params: list[dict[str, object] | None] = []

    async def execute(self, statement, params=None):  # noqa: ANN001
        self.statements.append(str(statement))
        self.params.append(params)
        return _FakeResult(self._result_sets.pop(0))


class _FakeContext:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        return None


@pytest.mark.asyncio
async def test_audit_session_reports_email_and_contact_id_duplicates() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    session = _FakeSession(
        [
            {
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "email": "buyer@example.com",
                "count": 2,
            }
        ],
        [
            {
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "related_local_id": contact_id,
                "count": 3,
            }
        ],
    )

    report = await audit.audit_session(session, organisation_id)

    assert report.has_duplicates is True
    assert report.email_duplicates[0].email == "buyer@example.com"
    assert report.email_duplicates[0].count == 2
    assert report.contact_id_duplicates[0].related_local_id == contact_id
    assert report.contact_id_duplicates[0].count == 3
    assert "lower(btrim(related_email))" in session.statements[0]
    assert "participant_type = 'contact'" in session.statements[0]
    assert "organisation_id = :organisation_id" in session.statements[0]
    assert session.params[0] == {"organisation_id": organisation_id}
    assert "relationship_role" not in session.statements[0]
    assert "is_stakeholder" not in session.statements[0]
    assert "related_local_id IS NOT NULL" in session.statements[1]
    assert "organisation_id = :organisation_id" in session.statements[1]
    assert session.params[1] == {"organisation_id": organisation_id}


@pytest.mark.asyncio
async def test_audit_org_uses_admin_org_session(monkeypatch: pytest.MonkeyPatch) -> None:
    organisation_id = uuid4()
    used_org_ids: list[object] = []
    session = _FakeSession([], [])

    def fake_acquire_org_admin_async_session(org_id):  # noqa: ANN001
        used_org_ids.append(org_id)
        return _FakeContext(session)

    monkeypatch.setattr(
        audit,
        "acquire_org_admin_async_session",
        fake_acquire_org_admin_async_session,
    )

    report = await audit.audit_org(organisation_id)

    assert used_org_ids == [organisation_id]
    assert report.has_duplicates is False


def test_exit_code_only_fails_when_requested() -> None:
    clean_report = audit.DuplicateAuditReport(email_duplicates=[], contact_id_duplicates=[])
    dirty_report = audit.DuplicateAuditReport(
        email_duplicates=[
            audit.EmailDuplicateGroup(
                organisation_id=uuid4(),
                deal_id=uuid4(),
                email="buyer@example.com",
                count=2,
            )
        ],
        contact_id_duplicates=[],
    )

    assert audit.exit_code_for_report(clean_report, fail_on_duplicates=True) == 0
    assert audit.exit_code_for_report(dirty_report, fail_on_duplicates=False) == 0
    assert audit.exit_code_for_report(dirty_report, fail_on_duplicates=True) == 1


def test_uniqueness_migration_uses_partial_indexes_without_role_marker() -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637739_060_add_stakeholder_fields_to_deal_participants.py"
    )

    migration_text = migration_path.read_text(encoding="utf-8")

    assert "ux_crm_deal_participant_active_contact_email" in migration_text
    assert "ux_crm_deal_participant_active_contact_local_id" in migration_text
    assert "lower(btrim(related_email))" in migration_text
    assert "participant_type = 'contact'" in migration_text
    assert "is_deleted = false" in migration_text
    assert "relationship_role" not in migration_text
    assert "is_stakeholder" not in migration_text
