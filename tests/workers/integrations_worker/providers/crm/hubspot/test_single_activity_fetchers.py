"""Regression coverage for HubSpot single task/note fetchers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher import (
    HUBSPOT_NOTE_PROPERTIES,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_note_fetcher import (
    HubSpotSingleNoteFetcher,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_task_fetcher import (
    HubSpotSingleTaskFetcher,
)
from src.workers.integrations_worker.providers.crm.hubspot.tasks_fetcher import (
    HUBSPOT_TASK_PROPERTIES,
)


class _Response:
    def __init__(self, *, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        """Return the fake response payload."""

        return self._payload


@pytest.mark.asyncio
async def test_single_task_fetcher_returns_payload(monkeypatch) -> None:
    """Regression test for single task fetcher returns HubSpot object payload."""

    async def fake_retry(token_info, request_factory):  # noqa: ANN001
        """Execute fake retry."""

        return _Response(status_code=200, payload={"id": "task-1"})

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.single_task_fetcher.call_with_token_retry",
        fake_retry,
    )

    result = await HubSpotSingleTaskFetcher(
        token_info=SimpleNamespace(),
        task_id="task-1",
    ).fetch()

    assert result == {"id": "task-1"}


@pytest.mark.asyncio
async def test_single_note_fetcher_returns_none_on_404(monkeypatch) -> None:
    """Regression test for single note fetcher treats HubSpot 404 as missing."""

    async def fake_retry(token_info, request_factory):  # noqa: ANN001
        """Execute fake retry."""

        return _Response(status_code=404, payload={})

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.single_note_fetcher.call_with_token_retry",
        fake_retry,
    )

    result = await HubSpotSingleNoteFetcher(
        token_info=SimpleNamespace(),
        note_id="note-1",
    ).fetch()

    assert result is None


def test_single_note_fetcher_default_properties_include_note_timestamp() -> None:
    """Single-note webhook refreshes fetch timeline and modified timestamps."""
    assert "hs_timestamp" in HUBSPOT_NOTE_PROPERTIES
    assert "hs_lastmodifieddate" in HUBSPOT_NOTE_PROPERTIES


def test_single_task_fetcher_default_properties_include_webhook_fields() -> None:
    """Single-task webhook refreshes fetch task fields used by the allow-list."""
    assert "hs_task_priority" in HUBSPOT_TASK_PROPERTIES
    assert "hs_task_type" in HUBSPOT_TASK_PROPERTIES
    assert "hs_timestamp" in HUBSPOT_TASK_PROPERTIES
    assert "hs_lastmodifieddate" in HUBSPOT_TASK_PROPERTIES
