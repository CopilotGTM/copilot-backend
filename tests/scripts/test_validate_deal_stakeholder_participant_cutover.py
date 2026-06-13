from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "validate_deal_stakeholder_participant_cutover.py"


class _MappingRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _ExecuteResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingRows:
        return _MappingRows(self._rows)


class _Session:
    def __init__(self, *result_sets: list[dict[str, object]]) -> None:
        self._result_sets = list(result_sets)
        self.statements = []

    async def execute(self, stmt, params=None):  # noqa: ANN001
        self.statements.append((stmt, params))
        return _ExecuteResult(self._result_sets.pop(0))


class _FakeContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        return None


def _load_module():
    assert SCRIPT_PATH.exists()
    spec = importlib.util.spec_from_file_location(
        "validate_deal_stakeholder_participant_cutover",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validation_script_exists() -> None:
    assert SCRIPT_PATH.exists()


@pytest.mark.asyncio
async def test_validate_session_reports_mismatched_old_and_participant_counts() -> None:
    module = _load_module()
    organisation_id = uuid4()
    deal_id = uuid4()
    session = _Session(
        [
            {
                "deal_id": deal_id,
                "old_active_stakeholders": 3,
                "active_contact_participants": 2,
                "delta": 1,
            }
        ],
        [],
        [],
    )

    report = await module.validate_session(session, organisation_id)

    assert report.total_deals_compared == 1
    assert report.mismatched_deals == 1
    assert report.has_mismatches is True
    assert report.rows[0].deal_id == deal_id
    assert report.has_validation_failures is False
    assert module.exit_code_for_report(report, fail_on_mismatch=True) == 0
    statement, params = session.statements[0]
    assert "deal_stakeholders" in str(statement)
    assert "crm_deal_participants" in str(statement)
    assert params == {"organisation_id": organisation_id}


@pytest.mark.asyncio
async def test_validate_session_reports_identity_and_contact_gaps() -> None:
    module = _load_module()
    organisation_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    participant_id = uuid4()
    session = _Session(
        [
            {
                "deal_id": deal_id,
                "old_active_stakeholders": 1,
                "active_contact_participants": 1,
                "delta": 0,
            }
        ],
        [
            {
                "deal_id": deal_id,
                "stakeholder_id": stakeholder_id,
                "email": "buyer@example.com",
            }
        ],
        [
            {
                "deal_id": deal_id,
                "participant_id": participant_id,
                "related_email": "buyer@example.com",
                "related_local_id": None,
            }
        ],
    )

    report = await module.validate_session(session, organisation_id)

    assert report.mismatched_deals == 0
    assert report.unmatched_stakeholders_count == 1
    assert report.participant_contact_gap_count == 1
    assert report.has_validation_failures is True
    assert module.exit_code_for_report(report, fail_on_mismatch=True) == 1
    assert "NOT EXISTS" in str(session.statements[1][0])
    assert "LEFT JOIN crm_contacts" in str(session.statements[2][0])


@pytest.mark.asyncio
async def test_validate_org_uses_admin_org_session(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    organisation_id = uuid4()
    used_org_ids: list[object] = []
    session = _Session([], [], [])

    def fake_acquire_org_admin_async_session(org_id):  # noqa: ANN001
        used_org_ids.append(org_id)
        return _FakeContext(session)

    monkeypatch.setattr(
        module,
        "acquire_org_admin_async_session",
        fake_acquire_org_admin_async_session,
    )

    report = await module.validate_org(organisation_id)

    assert used_org_ids == [organisation_id]
    assert report.has_validation_failures is False


def test_run_requires_validation_mode() -> None:
    module = _load_module()

    try:
        module.validate_args(SimpleNamespace(show_matched=False, only_mismatches=False))
    except SystemExit as exc:
        assert "Pass exactly one" in str(exc)
    else:
        raise AssertionError("validate_args should reject ambiguous output mode")
