"""Regression tests for integration sync not triggering broad deal matching."""

from __future__ import annotations

import inspect

from src.workers.integrations_worker.tasks.calendar import (
    integration_task as calendar_integration_task,
)
from src.workers.integrations_worker.tasks.calendar import org_task as calendar_org_task
from src.workers.integrations_worker.tasks.email import integration_task as email_integration_task
from src.workers.integrations_worker.tasks.email import org_task as email_org_task


def test_calendar_org_sync_does_not_dispatch_broad_deal_matching() -> None:
    source = inspect.getsource(calendar_org_task.sync_calendar_org_task)

    assert '"deal.match_calendar_to_deals_org"' not in source


def test_email_org_sync_does_not_dispatch_broad_deal_matching() -> None:
    source = inspect.getsource(email_org_task.sync_email_org_task)

    assert '"deal.match_email_to_deals_org"' not in source


def test_email_org_sync_uses_communication_deal_mapping_trigger_service() -> None:
    source = inspect.getsource(email_org_task.sync_email_org_task)

    assert "CommunicationDealMappingTriggerService" in source
    assert "trigger_for_changed_communications" in source
    assert 'changed_record_ids=sync_result.get("changed_email_ids", [])' in source


def test_email_integration_sync_uses_communication_deal_mapping_trigger_service() -> None:
    source = inspect.getsource(email_integration_task.sync_email_integration_task)

    assert "CommunicationDealMappingTriggerService" in source
    assert "trigger_for_changed_communications" in source
    assert 'changed_record_ids=result.get("changed_email_ids", [])' in source


def test_calendar_org_sync_uses_communication_deal_mapping_trigger_service() -> None:
    source = inspect.getsource(calendar_org_task.sync_calendar_org_task)

    assert "CommunicationDealMappingTriggerService" in source
    assert "trigger_for_changed_communications" in source
    assert 'changed_record_ids=sync_result.get("changed_event_ids", [])' in source


def test_calendar_integration_sync_uses_communication_deal_mapping_trigger_service() -> None:
    source = inspect.getsource(calendar_integration_task.sync_calendar_integration_task)

    assert "CommunicationDealMappingTriggerService" in source
    assert "trigger_for_changed_communications" in source
    assert 'changed_record_ids=sync_result.get("changed_event_ids", [])' in source
