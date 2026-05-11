"""Regression tests for email attachment Celery task registration."""

from __future__ import annotations

from src.workers.deal_worker import tasks


def test_email_attachment_tasks_are_exported_for_worker_autodiscovery() -> None:
    assert tasks.extract_email_attachments_all_orgs_task.name == (
        "deal_asset.extract_email_attachments_all_orgs"
    )
    assert (
        tasks.extract_email_attachments_org_task.name == "deal_asset.extract_email_attachments_org"
    )
    assert (
        tasks.extract_email_attachments_deal_task.name
        == "deal_asset.extract_email_attachments_deal"
    )
