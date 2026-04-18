from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import MultipleResultsFound
from src.agents.tools.communication.get_email_details import get_email_details
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailDirection,
    EmailDirection,
    EmailPriority,
)


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalar_one_or_none(self):
        if len(self._items) > 1:
            raise MultipleResultsFound("Multiple rows were found when one or none was required")
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _FakeSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)

    async def execute(self, _stmt):
        return _FakeResult(self._execute_results.pop(0))


class _FakeContext:
    def __init__(self, organisation_id, session):
        self.organisation_id = organisation_id
        self._session = session

    @asynccontextmanager
    async def acquire_session(self):
        yield self._session


def _make_email(*, organisation_id, internet_message_id, received_at, subject, body_text, snippet):
    return SimpleNamespace(
        email_id=uuid4(),
        organisation_id=organisation_id,
        internet_message_id=internet_message_id,
        thread_id="thread-1",
        subject=subject,
        from_email="sender@example.com",
        from_name="Sender",
        to_emails=["recipient@example.com"],
        cc_emails=[],
        received_at=received_at,
        sent_at=received_at,
        direction=EmailDirection.INBOUND,
        priority=EmailPriority.NORMAL,
        is_read=True,
        is_starred=False,
        is_important=False,
        has_attachments=False,
        labels=["inbox"],
        attachments=[],
        body_text=body_text,
        body_html=None,
        snippet=snippet,
    )


def _make_association(deal_id):
    return SimpleNamespace(
        deal_id=deal_id,
        direction=DealEmailDirection.INBOUND,
        status=AssociationStatus.ACTIVE,
        match_score=0.98,
        matched_stakeholder_emails=["buyer@example.com"],
    )


@pytest.mark.asyncio
async def test_get_email_details_returns_deterministic_copy_when_message_id_is_duplicated():
    organisation_id = uuid4()
    internet_message_id = "<message-id@example.com>"
    newer = _make_email(
        organisation_id=organisation_id,
        internet_message_id=internet_message_id,
        received_at=datetime.now(UTC),
        subject="Newest copy",
        body_text="latest body",
        snippet="latest snippet",
    )
    older = _make_email(
        organisation_id=organisation_id,
        internet_message_id=internet_message_id,
        received_at=datetime.now(UTC) - timedelta(hours=1),
        subject="Older copy",
        body_text="older body",
        snippet="older snippet",
    )
    association = _make_association(uuid4())

    context = _FakeContext(
        organisation_id=organisation_id,
        session=_FakeSession([[newer, older], [association]]),
    )

    result = await get_email_details(internet_message_id, context=context)

    assert result is not None
    assert result["internet_message_id"] == internet_message_id
    assert result["subject"] == "Newest copy"
    assert result["body_text"] == "latest body"
    assert result["deal_associations"] == [
        {
            "deal_id": str(association.deal_id),
            "direction": DealEmailDirection.INBOUND.value,
            "status": AssociationStatus.ACTIVE.value,
            "match_score": association.match_score,
            "matched_stakeholders": association.matched_stakeholder_emails,
        }
    ]
