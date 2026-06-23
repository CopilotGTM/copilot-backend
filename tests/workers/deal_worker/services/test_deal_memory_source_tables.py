from __future__ import annotations

from pathlib import Path


def test_deal_memory_backfill_note_detection_uses_crm_notes() -> None:
    source = Path(
        "src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py"
    ).read_text()

    deals_with_data_source = source.split("async def _deals_with_data", 1)[1]
    assert "CRMNote" in deals_with_data_source
    assert "CRMActivity" not in deals_with_data_source
    assert "activity_type" not in deals_with_data_source
