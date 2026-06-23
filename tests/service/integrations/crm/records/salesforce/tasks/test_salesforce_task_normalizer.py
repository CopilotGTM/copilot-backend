from __future__ import annotations

from datetime import UTC, datetime

from src.service.integrations.crm.records.salesforce.tasks.task_normalizer import normalize_tasks


def test_normalize_tasks_maps_opportunity_contact_and_account_links_with_deal_precedence() -> None:
    normalized, skipped = normalize_tasks(
        [
            {
                "Id": "00T1",
                "Subject": "Follow up with champion",
                "Description": "Prepare renewal follow-up",
                "Status": "Completed",
                "Priority": "High",
                "ActivityDate": "2026-04-25",
                "CompletedDateTime": "2026-04-24T10:00:00.000+0000",
                "OwnerId": "005OWNER",
                "WhatId": "006OPPTY",
                "WhoId": "003CONTACT",
                "AccountId": "001ACCOUNT",
                "CreatedDate": "2026-04-20T09:00:00.000+0000",
                "LastModifiedDate": "2026-04-24T11:00:00.000+0000",
            }
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    task = normalized[0]
    assert task.external_id == "00T1"
    assert task.subject == "Follow up with champion"
    assert task.body == "Prepare renewal follow-up"
    assert task.status == "COMPLETED"
    assert task.priority == "HIGH"
    assert task.owner_external_id == "005OWNER"
    assert task.deal_external_id == "006OPPTY"
    assert task.contact_external_id == "003CONTACT"
    assert task.company_external_id == "001ACCOUNT"
    assert task.related_object_type == "DEAL"
    assert task.related_external_id == "006OPPTY"
    assert task.due_date == datetime(2026, 4, 25, 0, 0, tzinfo=UTC)
    assert task.completed_date == datetime(2026, 4, 24, 10, 0, tzinfo=UTC)
    assert task.source_created_at == datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    assert task.source_modified_at == datetime(2026, 4, 24, 11, 0, tzinfo=UTC)


def test_normalize_tasks_skips_rows_without_id() -> None:
    normalized, skipped = normalize_tasks([{"Subject": "Missing id"}])

    assert normalized == []
    assert skipped == 1


def test_normalize_tasks_emits_only_crm_task_status_values() -> None:
    raw_rows = [
        {"Id": "00T_WAITING", "Status": "Waiting on someone else"},
        {"Id": "00T_DEFERRED", "Status": "Deferred"},
        {"Id": "00T_CANCELLED", "Status": "Canceled"},
        {"Id": "00T_PROGRESS", "Status": "In Progress"},
    ]

    normalized, skipped = normalize_tasks(raw_rows)

    assert skipped == 0
    assert [task.status for task in normalized] == [
        "IN_PROGRESS",
        "NOT_STARTED",
        "CANCELLED",
        "IN_PROGRESS",
    ]
    assert [task.properties["raw_status"] for task in normalized] == [
        "Waiting on someone else",
        "Deferred",
        "Canceled",
        "In Progress",
    ]
