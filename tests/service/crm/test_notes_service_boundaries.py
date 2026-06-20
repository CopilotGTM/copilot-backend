from __future__ import annotations

from pathlib import Path


def test_notes_vertical_module_exists() -> None:
    assert Path("src/service/crm/notes/__init__.py").exists()
    assert Path("src/service/crm/notes/contracts.py").exists()
    assert Path("src/service/crm/notes/read_service.py").exists()
    assert Path("src/service/crm/notes/write_service.py").exists()
    assert Path("src/service/crm/notes/insight_dispatch_service.py").exists()


def test_notes_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/notes/read_service.py").read_text()

    assert "async def create_user_note" not in source
    assert "async def update_user_note" not in source
    assert "async def delete_user_note" not in source
    assert "CRMActivity(" not in source


def test_notes_write_service_does_not_dispatch_insights() -> None:
    source = Path("src/service/crm/notes/write_service.py").read_text()

    assert "dispatch_deal_insight_pipeline" not in source
    assert "get_or_create_deal_memory_job" not in source


def test_deal_notes_service_is_compatibility_facade_after_migration() -> None:
    source = Path("src/service/crm/internal/deal_notes_service.py").read_text()

    assert "NotesReadService" in source
    assert "NotesWriteService" in source
    assert "NotesInsightDispatchService" in source
    assert "CRMActivity(" not in source
