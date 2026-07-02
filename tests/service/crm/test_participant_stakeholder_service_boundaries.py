from __future__ import annotations

import re
from pathlib import Path


def test_participant_vertical_module_exists() -> None:
    assert Path("src/service/crm/participants/__init__.py").exists()
    assert Path("src/service/crm/participants/contracts.py").exists()
    assert Path("src/service/crm/participants/participant_write_service.py").exists()
    assert Path("src/service/crm/participants/stakeholder_read_service.py").exists()
    assert Path("src/service/crm/participants/stakeholder_write_service.py").exists()
    assert Path("src/service/crm/participants/stakeholder_adapter.py").exists()


def test_stakeholder_table_is_not_reintroduced() -> None:
    stakeholder_table_pattern = re.compile(r"__tablename__ = \"deal_stakeholders\"")
    matches = [
        path
        for path in Path("src/domain/models").rglob("*.py")
        if stakeholder_table_pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert matches == []


def test_participant_plan_does_not_require_new_migrations() -> None:
    plan = Path(
        "docs/superpowers/plans/"
        "2026-06-20-crm-deal-participant-stakeholder-service-standardization.md"
    ).read_text()
    assert "Do not create or modify migrations in this first implementation slice." in plan


def test_new_participant_services_do_not_commit() -> None:
    for path in [
        "src/service/crm/participants/participant_write_service.py",
        "src/service/crm/participants/stakeholder_read_service.py",
        "src/service/crm/participants/stakeholder_write_service.py",
    ]:
        source = Path(path).read_text()
        assert ".commit(" not in source


def test_stakeholder_read_service_does_not_expose_write_methods() -> None:
    source = Path("src/service/crm/participants/stakeholder_read_service.py").read_text()
    assert "class StakeholderReadService(StakeholdersService)" not in source
    assert "StakeholdersService(" not in source
    assert "async def create_stakeholder" not in source
    assert "async def update_stakeholder" not in source
    assert "async def remove_stakeholder" not in source


def test_stakeholder_write_service_does_not_inherit_old_monolith() -> None:
    source = Path("src/service/crm/participants/stakeholder_write_service.py").read_text()
    assert "class StakeholderWriteService(StakeholdersService)" not in source
    assert "StakeholdersService(" not in source


def test_stakeholder_write_service_delegates_contact_writes() -> None:
    source = Path("src/service/crm/participants/stakeholder_write_service.py").read_text()
    assert "ContactWriteService(" in source


def test_old_stakeholders_service_is_only_a_facade() -> None:
    source = Path("src/service/crm/insights/stakeholders_service.py").read_text()
    forbidden_patterns = [
        "async def _ensure_contact_for_stakeholder",
        "async def _ensure_deal_participant",
        "async def _find_contact_participant",
        "async def _get_participant_contact",
        "select(CRMDealParticipant)",
        "select(CRMContact)",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in source


def test_new_stakeholder_contracts_do_not_expose_stakeholder_id() -> None:
    source = Path("src/service/crm/participants/contracts.py").read_text()
    assert "stakeholder_id:" not in source
    assert "participant_id:" in source
