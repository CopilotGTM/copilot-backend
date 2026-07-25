"""Tests for per-deal email attachment extraction task behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest
import src.workers.deal_worker.tasks.deal_asset_email_attachment_tasks as attachment_tasks
from src.service.crm.assets.email_attachment_service import (
    DealAssetEmailAttachmentService,
)
from src.workers.deal_worker.tasks.deal_asset_email_attachment_tasks import (
    extract_email_attachments_deal_task,
    extract_email_attachments_org_task,
)

ORGANISATION_ID = UUID("11111111-1111-1111-1111-111111111111")
DEAL_ID = UUID("22222222-2222-2222-2222-222222222222")


def _stats(
    *,
    errors: int,
    post_upload_asset_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "deal_id": str(DEAL_ID),
        "emails_with_attachments": 2,
        "assets_created": 1,
        "assets_skipped_dedup": 0,
        "errors": errors,
        "limit_reached": False,
        "remaining_email_ids": [],
        "post_upload_asset_ids": post_upload_asset_ids or [],
    }


def test_deal_task_rejects_empty_scoped_payload() -> None:
    """An empty automatic payload must never fall through to broad repair mode."""
    with pytest.raises(ValueError, match="email_ids"):
        extract_email_attachments_deal_task.run(
            str(ORGANISATION_ID),
            str(DEAL_ID),
            email_ids=[],
        )


def test_deal_task_rejects_oversized_scoped_payload() -> None:
    with pytest.raises(ValueError, match="cannot exceed 100"):
        extract_email_attachments_deal_task.run(
            str(ORGANISATION_ID),
            str(DEAL_ID),
            email_ids=[str(UUID(int=index + 1)) for index in range(101)],
        )


def test_deal_task_returns_success_when_extraction_has_no_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_id = UUID("33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_deal",
        AsyncMock(return_value=_stats(errors=0)),
    )

    result = extract_email_attachments_deal_task.run(
        str(ORGANISATION_ID),
        str(DEAL_ID),
        email_ids=[str(email_id)],
    )

    DealAssetEmailAttachmentService.process_deal.assert_awaited_once_with(
        DEAL_ID,
        email_ids=[email_id],
    )
    assert result["status"] == "success"
    assert result["organisation_id"] == str(ORGANISATION_ID)
    assert result["deal_id"] == str(DEAL_ID)
    assert result["assets_created"] == 1
    assert result["errors"] == 0


def test_deal_task_publishes_returned_post_upload_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed processing rows must reach post-upload or fail the extraction task."""
    asset_id = UUID("44444444-4444-4444-4444-444444444444")
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_deal",
        AsyncMock(return_value=_stats(errors=0, post_upload_asset_ids=[str(asset_id)])),
    )
    publish = Mock(return_value="post-upload-task")
    monkeypatch.setattr(attachment_tasks, "send_deal_task", publish, raising=False)

    extract_email_attachments_deal_task.run(
        str(ORGANISATION_ID),
        str(DEAL_ID),
        email_ids=[str(UUID("33333333-3333-3333-3333-333333333333"))],
    )

    publish.assert_called_once_with(
        "deal_asset.post_upload",
        args=[str(ORGANISATION_ID), str(asset_id)],
        queue="deal",
    )


def test_deal_task_publishes_bounded_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker limit is an execution bound, not silent permanent truncation."""
    remaining_email_id = UUID("55555555-5555-5555-5555-555555555555")
    stats = _stats(errors=0)
    stats["limit_reached"] = True
    stats["remaining_email_ids"] = [str(remaining_email_id)]
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_deal",
        AsyncMock(return_value=stats),
    )
    publish = Mock(return_value="continuation-task")
    monkeypatch.setattr(attachment_tasks, "send_deal_task", publish)

    extract_email_attachments_deal_task.run(
        str(ORGANISATION_ID),
        str(DEAL_ID),
        email_ids=[str(UUID("33333333-3333-3333-3333-333333333333"))],
    )

    publish.assert_called_once_with(
        "deal_asset.extract_email_attachments_deal",
        kwargs={
            "organisation_id": str(ORGANISATION_ID),
            "deal_id": str(DEAL_ID),
            "email_ids": [str(remaining_email_id)],
        },
        queue="deal",
    )


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


def test_org_task_retries_when_service_reports_partial_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider failures are task failures, not successful-looking org results."""
    monkeypatch.setattr(attachment_tasks, "acquire_task_lock", Mock(return_value=True))
    monkeypatch.setattr(attachment_tasks, "release_task_lock", Mock())
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_organisation",
        AsyncMock(
            return_value={
                "organisation_id": str(ORGANISATION_ID),
                "deals_processed": 1,
                "total_assets_created": 0,
                "total_errors": 1,
            }
        ),
    )
    monkeypatch.setattr(extract_email_attachments_org_task.request, "retries", 0)
    retry = Mock(side_effect=RuntimeError("retry requested"))
    monkeypatch.setattr(extract_email_attachments_org_task, "retry", retry)

    with pytest.raises(RuntimeError, match="retry requested"):
        extract_email_attachments_org_task.run(str(ORGANISATION_ID))

    retry.assert_called_once()


def test_org_task_dispatches_committed_post_upload_and_continuation_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator repair mode must not strand bounded or already committed work."""
    asset_id = UUID("44444444-4444-4444-4444-444444444444")
    remaining_email_id = UUID("55555555-5555-5555-5555-555555555555")
    monkeypatch.setattr(attachment_tasks, "acquire_task_lock", Mock(return_value=True))
    monkeypatch.setattr(attachment_tasks, "release_task_lock", Mock())
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_organisation",
        AsyncMock(
            return_value={
                "organisation_id": str(ORGANISATION_ID),
                "deals_processed": 1,
                "total_assets_created": 1,
                "total_errors": 0,
                "post_upload_asset_ids": [str(asset_id)],
                "continuations": [
                    {
                        "deal_id": str(DEAL_ID),
                        "email_ids": [str(remaining_email_id)],
                    }
                ],
            }
        ),
    )
    publish = Mock()
    monkeypatch.setattr(attachment_tasks, "send_deal_task", publish)

    result = extract_email_attachments_org_task.run(str(ORGANISATION_ID))

    assert result["status"] == "success"
    assert publish.call_args_list == [
        call(
            "deal_asset.post_upload",
            args=[str(ORGANISATION_ID), str(asset_id)],
            queue="deal",
        ),
        call(
            "deal_asset.extract_email_attachments_deal",
            kwargs={
                "organisation_id": str(ORGANISATION_ID),
                "deal_id": str(DEAL_ID),
                "email_ids": [str(remaining_email_id)],
            },
            queue="deal",
        ),
    ]


def test_org_task_raises_when_partial_error_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted operator repair must be a failed Celery result."""
    monkeypatch.setattr(attachment_tasks, "acquire_task_lock", Mock(return_value=True))
    monkeypatch.setattr(attachment_tasks, "release_task_lock", Mock())
    monkeypatch.setattr(
        DealAssetEmailAttachmentService,
        "process_organisation",
        AsyncMock(
            return_value={
                "organisation_id": str(ORGANISATION_ID),
                "deals_processed": 1,
                "total_assets_created": 0,
                "total_errors": 1,
                "post_upload_asset_ids": [],
                "continuations": [],
            }
        ),
    )
    monkeypatch.setattr(
        extract_email_attachments_org_task.request,
        "retries",
        extract_email_attachments_org_task.max_retries,
    )

    with pytest.raises(RuntimeError, match="1 error"):
        extract_email_attachments_org_task.run(str(ORGANISATION_ID))
