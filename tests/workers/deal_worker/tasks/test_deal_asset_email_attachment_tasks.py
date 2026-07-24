"""Tests for per-deal email attachment extraction task behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from src.workers.deal_worker.services.deal_asset_email_attachment_service import (
    DealAssetEmailAttachmentService,
)
from src.workers.deal_worker.tasks.deal_asset_email_attachment_tasks import (
    extract_email_attachments_deal_task,
)

ORGANISATION_ID = UUID("11111111-1111-1111-1111-111111111111")
DEAL_ID = UUID("22222222-2222-2222-2222-222222222222")


def _stats(*, errors: int) -> dict[str, int | str]:
    return {
        "deal_id": str(DEAL_ID),
        "emails_with_attachments": 2,
        "assets_created": 1,
        "assets_skipped_dedup": 0,
        "errors": errors,
    }


def test_deal_task_returns_success_when_extraction_has_no_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_deal",
        AsyncMock(return_value=_stats(errors=0)),
    )

    result = extract_email_attachments_deal_task.run(
        str(ORGANISATION_ID),
        str(DEAL_ID),
    )

    assert result["status"] == "success"
    assert result["organisation_id"] == str(ORGANISATION_ID)
    assert result["deal_id"] == str(DEAL_ID)
    assert result["assets_created"] == 1
    assert result["errors"] == 0


def test_deal_task_retries_when_extraction_reports_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_deal",
        AsyncMock(return_value=_stats(errors=1)),
    )
    monkeypatch.setattr(extract_email_attachments_deal_task.request, "retries", 0)
    retry = Mock(side_effect=RuntimeError("retry requested"))
    monkeypatch.setattr(extract_email_attachments_deal_task, "retry", retry)

    with pytest.raises(RuntimeError, match="retry requested"):
        extract_email_attachments_deal_task.run(
            str(ORGANISATION_ID),
            str(DEAL_ID),
        )

    retry.assert_called_once()
    assert retry.call_args.kwargs["countdown"] == 60


def test_deal_task_raises_when_error_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_deal",
        AsyncMock(return_value=_stats(errors=2)),
    )
    monkeypatch.setattr(
        extract_email_attachments_deal_task.request,
        "retries",
        extract_email_attachments_deal_task.max_retries,
    )

    with pytest.raises(RuntimeError, match="2 error"):
        extract_email_attachments_deal_task.run(
            str(ORGANISATION_ID),
            str(DEAL_ID),
        )
