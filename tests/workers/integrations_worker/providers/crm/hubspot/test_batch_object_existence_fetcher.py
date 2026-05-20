"""Regression coverage for HubSpot batch existence checks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.workers.integrations_worker.providers.crm.hubspot.reconciliation.batch_object_existence_fetcher import (
    HubSpotBatchObjectExistenceFetcher,
)


@pytest.mark.asyncio
async def test_batch_object_existence_fetcher_returns_ids_present_in_hubspot(monkeypatch) -> None:
    """Regression test for batch object existence fetcher returns ids present in hubspot."""

    calls: list[tuple[object, object]] = []

    class Response:
        """HTTP response shim used by the fake retry transport."""

        status_code = 200

        def raise_for_status(self) -> None:
            """Execute raise for status."""

            return None

        def json(self) -> dict:
            """Execute json."""

            return {"results": [{"id": "101"}, {"id": "103"}]}

    async def fake_retry(token_info, request_factory):  # noqa: ANN001
        """Execute fake retry."""

        calls.append((token_info, request_factory))
        return Response()

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.reconciliation.batch_object_existence_fetcher.call_with_token_retry",
        fake_retry,
    )

    result = await HubSpotBatchObjectExistenceFetcher(
        token_info=SimpleNamespace(),
        object_type="deals",
        object_ids=["101", "102", "103"],
    ).fetch_existing_ids()

    assert result == {"101", "103"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_batch_object_existence_fetcher_rejects_malformed_results(monkeypatch) -> None:
    """Regression test for batch object existence fetcher rejects malformed results."""

    class Response:
        """HTTP response shim used by the fake retry transport."""

        status_code = 200

        def raise_for_status(self) -> None:
            """Execute raise for status."""

            return None

        def json(self) -> dict:
            """Execute json."""

            return {"results": {"id": "not-a-list"}}

    async def fake_retry(token_info, request_factory):  # noqa: ANN001
        """Execute fake retry."""

        return Response()

    monkeypatch.setattr(
        "src.workers.integrations_worker.providers.crm.hubspot.reconciliation.batch_object_existence_fetcher.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(ValueError, match="non-list"):
        await HubSpotBatchObjectExistenceFetcher(
            token_info=SimpleNamespace(),
            object_type="contacts",
            object_ids=["1"],
        ).fetch_existing_ids()


def test_batch_object_existence_fetcher_rejects_unsupported_object_type() -> None:
    """Regression test for batch object existence fetcher rejects unsupported object type."""

    with pytest.raises(ValueError, match="Unsupported HubSpot reconciliation object"):
        HubSpotBatchObjectExistenceFetcher(
            token_info=SimpleNamespace(),
            object_type="tickets",
            object_ids=["1"],
        )
