"""Regression tests for mailbox-draft preview and send boundaries.

These tests exercise the agent-facing contract: reading a draft may produce a
preview, but only an explicitly confirmed call carrying the reviewed content
hash may reach the provider-send service.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import pytest
from src.agents.tools.communication.email_drafts import save_email_draft, send_email_draft
from src.service.email_sends.models import (
    EmailDraftOperationResult,
    EmailDraftSnapshot,
    EmailSendAddress,
)


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None

    @asynccontextmanager
    async def acquire_session(self):
        """Yield the inert session expected by the tool factory."""
        yield object()


def _snapshot() -> EmailDraftSnapshot:
    return EmailDraftSnapshot(
        draft_id="draft-1",
        sender_email="seller@example.com",
        to=[EmailSendAddress(email="buyer@example.com")],
        subject="Proposal",
        body="Please review the attached proposal.",
        content_hash="a" * 64,
    )


@pytest.mark.asyncio
async def test_save_reply_draft_reuses_existing_tool_contract(monkeypatch) -> None:
    """The existing save tool forwards a source message instead of adding another tool."""

    requests: list[object] = []

    class FakeService:
        """Capture the normalized request without crossing a provider boundary."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def save_draft(self, request):
            """Return provider state after recording the reply discriminator."""
            requests.append(request)
            return EmailDraftOperationResult(status="draft_saved", snapshot=_snapshot())

    monkeypatch.setattr(
        "src.agents.tools.communication.email_drafts.EmailSendService",
        FakeService,
    )

    result = await save_email_draft(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        to=["buyer@example.com"],
        subject="Re: Proposal",
        body="Thanks, I will review it.",
        sender_email="seller@example.com",
        reply_to_internet_message_id="<source@example.com>",
        reply_mode="reply",
    )

    request = requests[0]
    assert request.reply_to_internet_message_id == "<source@example.com>"
    assert request.reply_mode == "reply"
    assert result["draft_kind"] == "reply"
    assert result["message"] == "Reply saved in the existing email thread."


@pytest.mark.asyncio
async def test_draft_preview_never_calls_send_service(monkeypatch) -> None:
    """A preview-only call must remain read-only even with a valid draft."""

    calls: list[tuple[str, str]] = []

    class FakeService:
        """Record whether the preview path attempts an irreversible send."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def get_draft(self, *, draft_id, sender_email):
            """Return stable provider state for the preview."""
            return _snapshot()

        async def send_draft(self, **kwargs):
            """Record an unexpected send attempt without contacting a provider."""
            calls.append((kwargs["draft_id"], kwargs["expected_content_hash"]))

    monkeypatch.setattr(
        "src.agents.tools.communication.email_drafts.EmailSendService",
        FakeService,
    )

    result = await send_email_draft(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        draft_id="draft-1",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["content_hash"] == "a" * 64
    assert calls == []


@pytest.mark.asyncio
async def test_confirmed_draft_without_reviewed_hash_stays_read_only(monkeypatch) -> None:
    """The boolean confirmation alone cannot send without the reviewed hash."""

    sent = False

    class FakeService:
        """Expose provider state while tracking whether send is reached."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def get_draft(self, *, draft_id, sender_email):
            """Return the reviewed draft used by the guard."""
            return _snapshot()

        async def send_draft(self, **kwargs):
            """Flag any send that bypasses the required hash."""
            nonlocal sent
            sent = True

    monkeypatch.setattr(
        "src.agents.tools.communication.email_drafts.EmailSendService",
        FakeService,
    )

    result = await send_email_draft(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        draft_id="draft-1",
        confirmed=True,
    )

    assert result["code"] == "email_draft_content_hash_required"
    assert sent is False


@pytest.mark.asyncio
async def test_confirmed_draft_passes_exact_reviewed_hash_to_service(monkeypatch) -> None:
    """A confirmed call sends only through the exact hash-bound service path."""

    calls: list[dict] = []

    class FakeService:
        """Capture the exact arguments forwarded after confirmation."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def get_draft(self, *, draft_id, sender_email):
            """Return the draft whose hash the user reviewed."""
            return _snapshot()

        async def send_draft(self, **kwargs):
            """Capture the approved service call and report provider acceptance."""
            calls.append(kwargs)
            return EmailDraftOperationResult(status="sent", provider_status_code=202)

    monkeypatch.setattr(
        "src.agents.tools.communication.email_drafts.EmailSendService",
        FakeService,
    )

    result = await send_email_draft(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        draft_id="draft-1",
        sender_email="seller@example.com",
        expected_content_hash="a" * 64,
        confirmed=True,
    )

    assert result["status"] == "sent"
    assert calls == [
        {
            "draft_id": "draft-1",
            "sender_email": "seller@example.com",
            "expected_content_hash": "a" * 64,
        }
    ]
