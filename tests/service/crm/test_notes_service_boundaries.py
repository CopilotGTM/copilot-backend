from __future__ import annotations

from pathlib import Path


def test_notes_vertical_module_exists() -> None:
    assert Path("src/api/presenters/crm/note_presenter.py").exists()
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


def test_notes_read_service_uses_crm_notes_as_source_table() -> None:
    source = Path("src/service/crm/notes/read_service.py").read_text()

    assert "CRMNote" in source
    assert "select(CRMNote)" in source
    assert "select(CRMActivity)" not in source
    assert "cast(Any, CRMActivity)" not in source


def test_notes_write_service_uses_crm_notes_as_source_table() -> None:
    source = Path("src/service/crm/notes/write_service.py").read_text()

    assert "CRMNote" in source
    assert "CRMNote(" in source
    assert "CRMActivity(" not in source


def test_provider_note_writers_target_crm_notes_not_activity_rows() -> None:
    writer_paths = [
        Path("src/service/integrations/crm/records/hubspot/notes/note_writer.py"),
        Path("src/service/integrations/crm/records/salesforce/notes/note_writer.py"),
    ]

    for path in writer_paths:
        source = path.read_text()
        assert "CRMNote" in source
        assert "CRMActivity(" not in source
        assert "select(CRMActivity)" not in source
        assert "pg_insert(CRMActivity)" not in source
        assert "local_object_type=CRMSourceObjectType.ACTIVITY" not in source


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


def test_deal_notes_handler_does_not_own_note_presentation_or_sql() -> None:
    source = Path("src/api/routers/crm/deal_notes_handler.py").read_text()

    assert "_map_note_to_schema" not in source
    assert "CRMNote" not in source
    assert "note_title_or_preview" not in source
    assert "select(" not in source
    assert "func" not in source
    assert "col(" not in source


def test_note_presenter_does_not_own_database_access() -> None:
    source = Path("src/api/presenters/crm/note_presenter.py").read_text()

    assert "AsyncSession" not in source
    assert "select(" not in source
    assert "func" not in source
    assert "col(" not in source
