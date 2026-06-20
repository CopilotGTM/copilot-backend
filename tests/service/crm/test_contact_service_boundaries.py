from __future__ import annotations

from pathlib import Path


def test_contact_vertical_module_exists() -> None:
    assert Path("src/service/crm/contacts/__init__.py").exists()
    assert Path("src/service/crm/contacts/contracts.py").exists()
    assert Path("src/service/crm/contacts/read_service.py").exists()
    assert Path("src/service/crm/contacts/write_service.py").exists()
    assert Path("src/service/crm/contacts/deal_link_service.py").exists()


def test_contact_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/contacts/read_service.py").read_text()

    assert "async def create_contact" not in source
    assert "async def update_contact" not in source
    assert "async def add_contact_to_deal" not in source
    assert "CRMContact(" not in source


def test_contact_write_service_does_not_manage_deal_participants() -> None:
    source = Path("src/service/crm/contacts/write_service.py").read_text()

    assert "DealParticipantService" not in source
    assert "CRMDealParticipant" not in source
    assert "send_deal_task_async" not in source


def test_contact_construction_removed_from_access_service() -> None:
    source = Path("src/service/crm/access/contacts_access_service.py").read_text()

    assert "CRMContact(" not in source
