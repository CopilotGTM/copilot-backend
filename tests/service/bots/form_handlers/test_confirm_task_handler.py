from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.service.bots.form_handlers.confirm_task_handler import ConfirmTaskFormHandler


@dataclass
class _FakeUser:
    user_id: UUID
    organisation_id: UUID
    email: str = "user@example.com"


def _make_activity(provider: str = "slack", team_id: str = "T123") -> dict[str, Any]:
    """Build a minimal activity dict for form handlers."""
    return {
        "from": {"id": "U_SLACK_123"},
        "channelData": {"team": {"id": team_id}},
        "provider": provider,
    }


def _make_task_payload(
    org_id: UUID,
    title: str = "Test task",
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "nonce": "test-nonce-123",
        "title": title,
        "organisation_id": str(org_id),
    }
    payload.update(overrides)
    return payload


def _patch_resolve_user(monkeypatch, user):
    """Patch resolve_user_from_activity to return the given user."""

    async def _fake_resolve(_activity):
        return user, None

    monkeypatch.setattr(
        "src.service.bots.form_handlers.confirm_task_handler.resolve_user_from_activity",
        _fake_resolve,
    )


def _patch_nonce(monkeypatch, *, valid=True):
    """Patch nonce validation. When valid=True, nonce passes once then fails."""
    call_count = {"n": 0}

    def _fake_validate(prefix, nonce):
        call_count["n"] += 1
        return valid and call_count["n"] == 1

    monkeypatch.setattr(
        "src.service.bots.form_handlers.nonce.validate_and_consume_nonce",
        _fake_validate,
    )


def _patch_task_service(monkeypatch, created_payloads, created_deal_ids=None):
    """Patch TasksWriteService and acquire_org_async_session."""

    @dataclass
    class FakeTask:
        subject: str = ""
        activity_id: UUID = uuid4()

    class FakeWriteService:
        def __init__(self, session, org_id, user_id):
            pass

        async def create_task(self, payload, *, deal_id=None):
            created_payloads.append(payload)
            if created_deal_ids is not None:
                created_deal_ids.append(deal_id)
            return FakeTask(subject=payload.get("subject", ""))

    monkeypatch.setattr(
        "src.service.crm.internal.tasks_write_service.TasksWriteService",
        FakeWriteService,
    )

    class _FakeSession:
        async def commit(self):
            pass

    async def _fake_session_gen():
        yield _FakeSession()

    monkeypatch.setattr(
        "src.service.bots.form_handlers.confirm_task_handler.acquire_org_async_session",
        lambda org_id: _fake_session_gen(),
    )


# ---------------------------------------------------------------------------
# Identity resolution failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_task_rejects_unlinked_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolve_user(monkeypatch, None)

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), {"title": "x"})
    assert not result.success
    assert "identify" in result.message.lower()


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_task_rejects_missing_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), {"action": "confirm_task_submit"})
    assert not result.success
    assert "missing task data" in result.message.lower()


@pytest.mark.asyncio
async def test_confirm_task_rejects_invalid_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(
        _make_activity(),
        {"task_payload": "not-valid-json{{{"},
    )
    assert not result.success
    assert "invalid" in result.message.lower()


# ---------------------------------------------------------------------------
# Org mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_task_rejects_org_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)

    payload = _make_task_payload(uuid4(), title="Mismatched task")

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), payload)
    assert not result.success
    assert "mismatch" in result.message.lower()


@pytest.mark.asyncio
async def test_confirm_task_rejects_missing_title(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)

    payload = {"nonce": "x", "organisation_id": str(user.organisation_id), "title": "   "}
    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), payload)
    assert not result.success
    assert "title" in result.message.lower()


@pytest.mark.asyncio
async def test_confirm_task_rejects_duplicate_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second click on Create Task should be rejected by nonce."""
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)  # first call valid, second invalid
    _patch_task_service(monkeypatch, [])

    payload = _make_task_payload(user.organisation_id, title="Only once")

    handler = ConfirmTaskFormHandler()
    result1 = await handler.handle(_make_activity(), payload)
    assert result1.success

    result2 = await handler.handle(_make_activity(), payload)
    assert not result2.success
    assert "already created" in result2.message.lower()


# ---------------------------------------------------------------------------
# Successful task creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_task_creates_task_via_service(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)

    created_payloads: list[dict] = []
    created_deal_ids: list[UUID | None] = []
    _patch_task_service(monkeypatch, created_payloads, created_deal_ids)

    deal_id = uuid4()
    payload = _make_task_payload(
        user.organisation_id,
        title="Follow up",
        priority="HIGH",
        due_date="2030-06-15T17:00:00+00:00",
        deal_id=str(deal_id),
        description="Send the proposal",
    )

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), payload)

    assert result.success
    assert "Follow up" in result.message

    assert len(created_payloads) == 1
    p = created_payloads[0]
    assert p["subject"] == "Follow up"
    assert p["body"] == "Send the proposal"
    assert p["priority"] == "HIGH"
    assert created_deal_ids[0] == deal_id


@pytest.mark.asyncio
async def test_confirm_task_teams_path_parses_task_payload_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teams path: form_data has 'task_payload' as a JSON string."""
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)

    created_payloads: list[dict] = []
    _patch_task_service(monkeypatch, created_payloads)

    inner_payload = _make_task_payload(user.organisation_id, title="Teams task")
    form_data = {
        "action": "confirm_task_submit",
        "task_payload": json.dumps(inner_payload),
    }

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(
        _make_activity(provider="teams"),
        form_data,
    )

    assert result.success
    assert "Teams task" in result.message
    assert created_payloads[0]["subject"] == "Teams task"


# ---------------------------------------------------------------------------
# Reminder integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_task_includes_reminder_config(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)

    created_payloads: list[dict] = []
    _patch_task_service(monkeypatch, created_payloads)

    future_due = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    payload = _make_task_payload(
        user.organisation_id,
        title="Task with reminder",
        due_date=future_due,
        reminder="1h_before",
        delivery_channel="slack",
        delivery_team_id="T1",
        delivery_channel_id="C1",
    )

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), payload)

    assert result.success
    p = created_payloads[0]
    assert "reminder" in p
    assert p["reminder"]["enabled"] is True
    assert p["reminder"]["delivery_channel"] == "slack"
    assert p["reminder"]["delivery_context"]["team_id"] == "T1"


@pytest.mark.asyncio
async def test_confirm_task_skips_reminder_when_trigger_in_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _FakeUser(user_id=uuid4(), organisation_id=uuid4())
    _patch_resolve_user(monkeypatch, user)
    _patch_nonce(monkeypatch)

    created_payloads: list[dict] = []
    _patch_task_service(monkeypatch, created_payloads)

    past_due = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    payload = _make_task_payload(
        user.organisation_id,
        title="Overdue task",
        due_date=past_due,
        reminder="at_due",
    )

    handler = ConfirmTaskFormHandler()
    result = await handler.handle(_make_activity(), payload)

    assert result.success
    # Reminder should NOT be in the payload since trigger time is in the past
    assert "reminder" not in created_payloads[0]
