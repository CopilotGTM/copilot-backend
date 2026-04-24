from __future__ import annotations

from datetime import UTC, datetime

from src.service.integrations.crm.records.salesforce.deals.stage_history_normalizer import (
    current_stage_entered_at_by_deal,
    normalize_stage_history,
)


def test_normalizer_orders_salesforce_stage_history_and_derives_previous_stage() -> None:
    transitions, skipped = normalize_stage_history(
        [
            {
                "OpportunityId": "006A",
                "StageName": "Prospecting",
                "CreatedDate": "2026-04-01T10:00:00+00:00",
                "Amount": 100,
                "Probability": 10,
            },
            {
                "OpportunityId": "006A",
                "StageName": "Closed Won",
                "CreatedDate": "2026-04-03T15:30:00+00:00",
                "Amount": 100,
                "Probability": 100,
            },
        ],
        pipeline_external_id_by_deal={"006A": "012PIPE"},
    )

    assert skipped == 0
    assert transitions[1].from_stage_external_id == "012PIPE:Prospecting"
    assert transitions[0].exited_at == datetime(2026, 4, 3, 15, 30, tzinfo=UTC)

    entered_at_map = current_stage_entered_at_by_deal(
        transitions,
        current_stage_external_id_by_deal={"006A": "012PIPE:Closed Won"},
    )
    assert entered_at_map["006A"] == datetime(2026, 4, 3, 15, 30, tzinfo=UTC)
