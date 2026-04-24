from __future__ import annotations

from datetime import UTC, datetime

from src.service.integrations.crm.records.hubspot.tasks.task_normalizer import normalize_tasks


def test_normalize_tasks_maps_core_fields_and_related_object_precedence() -> None:
    normalized, skipped = normalize_tasks(
        [
            {
                "id": "123",
                "properties": {
                    "hs_task_subject": "Follow up with buyer",
                    "hs_task_body": "Call back tomorrow",
                    "hs_task_status": "NOT_STARTED",
                    "hubspot_owner_id": "456",
                    "hs_timestamp": "1713969000000",
                    "hs_createdate": "2026-04-20T10:00:00Z",
                    "hs_lastmodifieddate": "2026-04-21T12:00:00Z",
                },
                "associations": {
                    "deals": {"results": [{"id": "deal-1"}]},
                    "contacts": {"results": [{"id": "contact-1"}]},
                    "companies": {"results": [{"id": "company-1"}]},
                },
            }
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    task = normalized[0]
    assert task.external_id == "123"
    assert task.subject == "Follow up with buyer"
    assert task.body == "Call back tomorrow"
    assert task.status == "NOT_STARTED"
    assert task.owner_external_id == "456"
    assert task.deal_external_id == "deal-1"
    assert task.contact_external_id == "contact-1"
    assert task.company_external_id == "company-1"
    assert task.related_object_type == "DEAL"
    assert task.related_external_id == "deal-1"
    assert task.due_date == datetime.fromtimestamp(1713969000, tz=UTC)
    assert task.source_created_at == datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    assert task.source_modified_at == datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


def test_normalize_tasks_skips_rows_without_id() -> None:
    normalized, skipped = normalize_tasks([{"properties": {"hs_task_subject": "Missing id"}}])

    assert normalized == []
    assert skipped == 1
