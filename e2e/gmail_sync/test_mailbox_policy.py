"""Deployed Gmail smoke coverage for mailbox inclusion and label transitions."""

from __future__ import annotations

import os
import sys
import warnings
from datetime import UTC, datetime
from typing import Any

import pytest

from e2e.gmail_sync.support import (
    GmailSeeder,
    SeededMessage,
    configured_uuid,
    count_email_identity,
    dispatch_email_sync,
    fetch_email,
    fetch_sync_state,
    require_gmail_live_target,
    wait_for_sync_after,
    wait_for_task,
)

pytestmark = pytest.mark.e2e


def _cursor(state: dict[str, Any]) -> int:
    value = (state.get("cursor_metadata") or {}).get("gmail_history_id")
    assert isinstance(value, str) and value.isdigit(), state
    return int(value)


def test_gmail_mailbox_policy_lifecycle() -> None:
    """Exercise a deployed subset of the initial and incremental Gmail scenarios.

    Records: messages created in the dedicated Gmail and tenant fixtures.
    Forbidden outcome: drafts/archive/sent are omitted, trash remains visible, or reruns duplicate.
    Cleanup: exact seeded provider messages only; tenant rows remain as soft-delete history.

    This smoke does not claim the GMAIL-E2E scenario IDs. Their remaining mail
    classes, draft lifecycle, expired cursor, pagination, and cap checks are
    tracked by the deployed E2E framework contract.
    """
    require_gmail_live_target()
    organisation_id = configured_uuid("E2E_GMAIL_ORGANISATION_ID")
    integration_id = configured_uuid("E2E_GMAIL_INTEGRATION_ID")
    before = fetch_sync_state(organisation_id, integration_id)
    assert before is not None
    if os.getenv("E2E_GMAIL_REQUIRE_INITIAL", "").lower() == "true":
        assert not (before.get("cursor_metadata") or {}).get("gmail_history_id"), before

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    seeder = GmailSeeder()
    seeded: list[SeededMessage] = []
    try:
        # Seed every supported mailbox class before dispatch so one provider pass
        # proves the inclusion policy, folder assignment, and attachment parsing.
        inbox = seeder.insert(f"e2e-{run_id}-inbox", ["INBOX"], attachment=True)
        seeded.append(inbox)
        archive = seeder.insert(f"e2e-{run_id}-archive", [])
        seeded.append(archive)
        sent = seeder.send(f"e2e-{run_id}-sent")
        seeded.append(sent)
        draft = seeder.create_draft(f"e2e-{run_id}-draft")
        seeded.append(draft)
        excluded = seeder.insert(f"e2e-{run_id}-trash", ["INBOX"])
        seeded.append(excluded)
        seeder.trash(excluded.message_id)

        started_at = datetime.now(UTC)
        task_id = dispatch_email_sync(organisation_id, integration_id)
        assert task_id
        wait_for_task(task_id)
        first_state = wait_for_sync_after(organisation_id, integration_id, started_at=started_at)
        first_cursor = _cursor(first_state)

        expected = {
            inbox.message_id: "INBOX",
            archive.message_id: "ALL_MAIL",
            sent.message_id: "SENT",
            draft.message_id: "DRAFT",
        }
        for message_id, folder_id in expected.items():
            row = fetch_email(organisation_id, integration_id, message_id)
            assert row is not None and row["is_deleted"] is False, (message_id, row)
            assert row["external_folder_id"] == folder_id, row
        inbox_row = fetch_email(organisation_id, integration_id, inbox.message_id)
        assert inbox_row is not None and inbox_row["has_attachments"] is True
        excluded_row = fetch_email(organisation_id, integration_id, excluded.message_id)
        assert excluded_row is None or excluded_row["is_deleted"] is True

        # Label changes must be read from Gmail history; advancing the cursor first
        # would leave the old local rows visible forever.
        seeder.trash(inbox.message_id)
        seeder.modify_labels(archive.message_id, add=["INBOX"])
        started_at = datetime.now(UTC)
        wait_for_task(dispatch_email_sync(organisation_id, integration_id))
        second_state = wait_for_sync_after(organisation_id, integration_id, started_at=started_at)
        assert _cursor(second_state) >= first_cursor
        moved_out = fetch_email(organisation_id, integration_id, inbox.message_id)
        moved_in = fetch_email(organisation_id, integration_id, archive.message_id)
        assert moved_out is not None and moved_out["is_deleted"] is True
        assert moved_in is not None and moved_in["external_folder_id"] == "INBOX"

        # A no-change replay locks the tenant identity and cursor idempotency contract.
        started_at = datetime.now(UTC)
        wait_for_task(dispatch_email_sync(organisation_id, integration_id))
        third_state = wait_for_sync_after(organisation_id, integration_id, started_at=started_at)
        assert _cursor(third_state) >= _cursor(second_state)
        for message in seeded:
            assert count_email_identity(organisation_id, integration_id, message.message_id) <= 1
    finally:
        original_failure = sys.exc_info()[0] is not None
        cleanup_failures = []
        for message in reversed(seeded):
            try:
                seeder.cleanup(message)
            except Exception as error:
                cleanup_failures.append(f"{message.message_id}: {error}")
        seeder.close()
        if cleanup_failures:
            detail = "; ".join(cleanup_failures)
            if original_failure:
                warnings.warn(f"Gmail E2E cleanup also failed: {detail}", stacklevel=1)
            else:
                pytest.fail(f"Gmail E2E cleanup failed: {detail}")
