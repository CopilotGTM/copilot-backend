"""Tests for deal-asset post-upload publication and retry ownership."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import src.workers.deal_worker.tasks.deal_asset_post_upload_task as task_module
from src.infra.messaging.event_models import DealAssetEventUpdate
from src.service.crm.assets.post_upload_service import (
    PostUploadOutcome,
    PostUploadStatus,
)
from src.workers.deal_worker.tasks.deal_asset_post_upload_task import (
    deal_asset_post_upload,
)

ORGANISATION_ID = "11111111-1111-1111-1111-111111111111"
ASSET_ID = "22222222-2222-2222-2222-222222222222"
DEAL_ID = "33333333-3333-3333-3333-333333333333"


def _event(status: str = "active") -> DealAssetEventUpdate:
    return DealAssetEventUpdate(
        asset_id=ASSET_ID,
        deal_id=DEAL_ID,
        status=status,
    )


def _install_service(monkeypatch: pytest.MonkeyPatch, outcome: PostUploadOutcome) -> None:
    service = Mock()
    service.process.return_value = outcome
    monkeypatch.setattr(
        task_module,
        "DealAssetPostUploadService",
        Mock(return_value=service),
    )


def test_task_publishes_event_after_service_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    _install_service(
        monkeypatch,
        PostUploadOutcome(PostUploadStatus.PROCESSED, event=event),
    )
    publish = Mock()
    monkeypatch.setattr(task_module, "publish_deal_asset_status", publish)

    result = deal_asset_post_upload.run(ORGANISATION_ID, ASSET_ID)

    publish.assert_called_once_with(ORGANISATION_ID, event)
    assert result == {"status": "processed", "asset_id": ASSET_ID}


def test_task_publishes_failed_state_then_retries_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("failed")
    _install_service(
        monkeypatch,
        PostUploadOutcome(
            PostUploadStatus.RETRYABLE_FAILURE,
            event=event,
            error="blob unavailable",
        ),
    )
    publish = Mock()
    monkeypatch.setattr(task_module, "publish_deal_asset_status", publish)
    monkeypatch.setattr(deal_asset_post_upload.request, "retries", 0)
    retry = Mock(side_effect=RuntimeError("retry requested"))
    monkeypatch.setattr(deal_asset_post_upload, "retry", retry)

    with pytest.raises(RuntimeError, match="retry requested"):
        deal_asset_post_upload.run(ORGANISATION_ID, ASSET_ID)

    publish.assert_called_once_with(ORGANISATION_ID, event)
    retry.assert_called_once()
    assert retry.call_args.kwargs["countdown"] == 60


def test_task_retries_when_event_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(
        monkeypatch,
        PostUploadOutcome(PostUploadStatus.PROCESSED, event=_event()),
    )
    monkeypatch.setattr(
        task_module,
        "publish_deal_asset_status",
        Mock(side_effect=OSError("redis unavailable")),
    )
    monkeypatch.setattr(deal_asset_post_upload.request, "retries", 0)
    retry = Mock(side_effect=RuntimeError("retry requested"))
    monkeypatch.setattr(deal_asset_post_upload, "retry", retry)

    with pytest.raises(RuntimeError, match="retry requested"):
        deal_asset_post_upload.run(ORGANISATION_ID, ASSET_ID)

    retry.assert_called_once()


def test_invalid_asset_is_terminal_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(
        monkeypatch,
        PostUploadOutcome(PostUploadStatus.INVALID, error="Asset not found"),
    )
    publish = Mock()
    monkeypatch.setattr(task_module, "publish_deal_asset_status", publish)

    result = deal_asset_post_upload.run(ORGANISATION_ID, ASSET_ID)

    assert result == {"status": "invalid", "asset_id": ASSET_ID}
    publish.assert_not_called()


def test_retryable_failure_raises_after_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(
        monkeypatch,
        PostUploadOutcome(
            PostUploadStatus.RETRYABLE_FAILURE,
            error="blob unavailable",
        ),
    )
    monkeypatch.setattr(task_module, "publish_deal_asset_status", Mock())
    monkeypatch.setattr(
        deal_asset_post_upload.request,
        "retries",
        deal_asset_post_upload.max_retries,
    )

    with pytest.raises(RuntimeError, match="exhausted retries"):
        deal_asset_post_upload.run(ORGANISATION_ID, ASSET_ID)
