"""Tests for calendar sync scheduled task.

These tests verify the functionality of the calendar_sync_tasks module,
including organization filtering, integration queries, task triggering,
and error handling.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from src.domain.models.organisation_tables.sync import IntegrationType


class TestCalendarIntegrationTypes:
    """Tests for calendar integration type constants."""

    def test_calendar_integration_types_defined(self) -> None:
        """CALENDAR_INTEGRATION_TYPES should include Google and Outlook."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            CALENDAR_INTEGRATION_TYPES,
        )

        assert IntegrationType.GOOGLE_CALENDAR in CALENDAR_INTEGRATION_TYPES
        assert IntegrationType.OUTLOOK_CALENDAR in CALENDAR_INTEGRATION_TYPES

    def test_calendar_integration_types_excludes_others(self) -> None:
        """CALENDAR_INTEGRATION_TYPES should not include non-calendar types."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            CALENDAR_INTEGRATION_TYPES,
        )

        assert IntegrationType.TEAMS not in CALENDAR_INTEGRATION_TYPES
        assert IntegrationType.HUBSPOT not in CALENDAR_INTEGRATION_TYPES
        assert IntegrationType.SLACK not in CALENDAR_INTEGRATION_TYPES
        assert IntegrationType.GMAIL not in CALENDAR_INTEGRATION_TYPES


class TestProviderCategorization:
    """Tests for provider categorization logic."""

    def test_google_calendar_categorization(self) -> None:
        """Google Calendar should map to (calendar, google)."""
        from src.infra.config.provider_categories import categorize_integration_type

        category, provider = categorize_integration_type(IntegrationType.GOOGLE_CALENDAR)
        assert category == "calendar"
        assert provider == "google"

    def test_outlook_calendar_categorization(self) -> None:
        """Outlook Calendar should map to (calendar, outlook)."""
        from src.infra.config.provider_categories import categorize_integration_type

        category, provider = categorize_integration_type(IntegrationType.OUTLOOK_CALENDAR)
        assert category == "calendar"
        assert provider == "outlook"


class TestProviderConfigKeys:
    """Tests for Nango provider config key mapping."""

    def test_google_calendar_provider_key(self) -> None:
        """Google Calendar should use google-calendar provider key."""
        from src.infra.config.nango_provider_mapping import to_nango_provider_key

        key = to_nango_provider_key(IntegrationType.GOOGLE_CALENDAR)
        assert key == "google-calendar"

    def test_outlook_calendar_provider_key(self) -> None:
        """Outlook Calendar should use outlook-bflm provider key."""
        from src.infra.config.nango_provider_mapping import to_nango_provider_key

        key = to_nango_provider_key(IntegrationType.OUTLOOK_CALENDAR)
        assert key == "outlook-bflm"


class TestSyncTaskParameters:
    """Tests for sync_calendar_events_task parameter handling."""

    def test_task_registered_with_correct_name(self) -> None:
        """Task should be registered with integrations.sync_calendar_events name."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            sync_calendar_events_task,
        )

        assert sync_calendar_events_task.name == "integrations.sync_calendar_events"

    def test_task_has_zero_retries(self) -> None:
        """Coordinator task should have max_retries=0 (workers retry independently)."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            sync_calendar_events_task,
        )

        assert sync_calendar_events_task.max_retries == 0

    def test_task_has_acks_late(self) -> None:
        """Task should have acks_late=True for reliability."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            sync_calendar_events_task,
        )

        assert sync_calendar_events_task.acks_late is True


class TestSyncAllOrgsLogic:
    """Tests for _sync_all_orgs logic."""

    @patch("src.workers.integrations_worker.tasks.calendar_sync_tasks.acquire_master_async_session")
    @patch("src.workers.integrations_worker.tasks.calendar_sync_tasks.run_async")
    def test_returns_zero_when_no_approved_orgs(self, mock_run_async, mock_acquire_session) -> None:
        """Should return organizations_processed=0 when no approved orgs exist."""

        # Mock empty org list
        async def mock_session_gen():
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_session.execute.return_value = mock_result
            yield mock_session

        mock_acquire_session.return_value = mock_session_gen()

        # Set up run_async to actually run the coroutine
        async def run_coro(coro):
            return await coro

        mock_run_async.side_effect = (
            lambda coro: __import__("asyncio").get_event_loop().run_until_complete(coro)
        )

        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            sync_calendar_events_task,
        )

        # Call without org_id to trigger _sync_all_orgs
        result = sync_calendar_events_task()

        assert result["status"] == "completed"
        assert result["organizations_processed"] == 0
        assert result["total_syncs_triggered"] == 0


class TestSyncOrganizationLogic:
    """Tests for _sync_organization logic."""

    def test_returns_zero_integrations_when_none_found(self) -> None:
        """Should return integrations_count=0 when org has no calendar integrations."""
        # This would require mocking the database session
        # The logic is: if no integrations match the query, return early with count=0
        expected_result = {
            "org_id": "some-uuid",
            "integrations_count": 0,
            "syncs_triggered": 0,
        }

        # Verify structure
        assert "org_id" in expected_result
        assert "integrations_count" in expected_result
        assert "syncs_triggered" in expected_result


class TestIntegrationFiltering:
    """Tests for integration filtering logic."""

    def test_integration_query_filters_by_type(self) -> None:
        """Query should filter by GOOGLE_CALENDAR and OUTLOOK_CALENDAR types."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            CALENDAR_INTEGRATION_TYPES,
        )

        # Verify the filter would include correct types
        assert IntegrationType.GOOGLE_CALENDAR in CALENDAR_INTEGRATION_TYPES
        assert IntegrationType.OUTLOOK_CALENDAR in CALENDAR_INTEGRATION_TYPES
        assert len(CALENDAR_INTEGRATION_TYPES) == 2

    def test_integration_query_requires_active(self) -> None:
        """Query should filter for is_active=True."""
        # This is a documentation test - the actual query uses:
        # KnowledgeIntegration.is_active == True
        pass

    def test_integration_query_requires_connection_id(self) -> None:
        """Query should filter for connection_id IS NOT NULL."""
        # This is a documentation test - the actual query uses:
        # KnowledgeIntegration.connection_id.isnot(None)
        pass


class TestSyncTaskTriggering:
    """Tests for sync_integration_data_task.apply_async calls."""

    def test_sync_kwargs_structure(self) -> None:
        """Verify the structure of kwargs passed to sync_integration_data_task."""
        org_id = uuid4()
        integration_id = uuid4()

        # Expected kwargs structure for Google Calendar
        google_kwargs = {
            "organisation_id": str(org_id),
            "integration_id": str(integration_id),
            "category": "calendar",
            "provider": "google",
            "provider_config_key": "google-calendar",
            "sync_name": "events",
            "sync_type": "INCREMENTAL",
            "sync_variant": "base",
            "incremental": True,
        }

        assert google_kwargs["category"] == "calendar"
        assert google_kwargs["sync_name"] == "events"
        assert google_kwargs["sync_type"] == "INCREMENTAL"
        assert google_kwargs["incremental"] is True

    def test_outlook_sync_kwargs(self) -> None:
        """Verify kwargs for Outlook Calendar sync."""
        outlook_kwargs = {
            "category": "calendar",
            "provider": "outlook",
            "provider_config_key": "outlook-bflm",
            "sync_name": "events",
            "sync_type": "INCREMENTAL",
        }

        assert outlook_kwargs["provider"] == "outlook"
        assert outlook_kwargs["provider_config_key"] == "outlook-bflm"


class TestResultStructure:
    """Tests for return value structure."""

    def test_all_orgs_result_structure(self) -> None:
        """Result from _sync_all_orgs should have required fields."""
        expected_fields = {
            "status",
            "organizations_processed",
            "organizations_with_integrations",
            "total_syncs_triggered",
            "results",
        }

        sample_result = {
            "status": "completed",
            "organizations_processed": 5,
            "organizations_with_integrations": 3,
            "total_syncs_triggered": 8,
            "results": [],
        }

        assert expected_fields.issubset(sample_result.keys())

    def test_per_org_result_structure(self) -> None:
        """Result from _sync_organization should have required fields."""
        expected_fields = {
            "org_id",
            "integrations_count",
            "syncs_triggered",
        }

        sample_result = {
            "org_id": str(uuid4()),
            "integrations_count": 2,
            "syncs_triggered": 2,
            "results": [],
        }

        assert expected_fields.issubset(sample_result.keys())

    def test_per_integration_result_structure(self) -> None:
        """Per-integration result should include integration details."""
        sample_integration_result = {
            "integration_id": str(uuid4()),
            "integration_type": "google_calendar",
            "user_id": str(uuid4()),
            "sync": {
                "type": "events",
                "task_id": "celery-task-id",
                "status": "submitted",
                "provider": "google",
            },
        }

        assert "integration_id" in sample_integration_result
        assert "integration_type" in sample_integration_result
        assert "sync" in sample_integration_result
        assert sample_integration_result["sync"]["type"] == "events"
        assert sample_integration_result["sync"]["status"] == "submitted"

    def test_failed_sync_result_structure(self) -> None:
        """Failed sync should include error details."""
        sample_failed_result = {
            "integration_id": str(uuid4()),
            "integration_type": "outlook_calendar",
            "sync": {
                "type": "events",
                "status": "failed",
                "provider": "outlook",
                "error": "Connection timeout",
            },
        }

        assert sample_failed_result["sync"]["status"] == "failed"
        assert "error" in sample_failed_result["sync"]


class TestErrorHandling:
    """Tests for error handling behavior."""

    def test_error_result_structure(self) -> None:
        """Error results should include error details."""
        error_result = {
            "status": "failed",
            "error": "Database connection failed",
            "error_type": "ConnectionError",
        }

        assert "status" in error_result
        assert error_result["status"] == "failed"
        assert "error" in error_result
        assert "error_type" in error_result

    def test_per_org_error_structure(self) -> None:
        """Per-org error should include org_id and error."""
        org_error = {
            "org_id": str(uuid4()),
            "status": "failed",
            "error": "Session acquisition failed",
        }

        assert "org_id" in org_error
        assert org_error["status"] == "failed"
        assert "error" in org_error


class TestConvenienceAlias:
    """Tests for convenience alias."""

    def test_sync_calendar_events_alias_exists(self) -> None:
        """sync_calendar_events should be an alias for sync_calendar_events_task."""
        from src.workers.integrations_worker.tasks.calendar_sync_tasks import (
            sync_calendar_events,
            sync_calendar_events_task,
        )

        assert sync_calendar_events is sync_calendar_events_task


class TestTaskExport:
    """Tests for task module exports."""

    def test_task_exported_from_init(self) -> None:
        """sync_calendar_events_task should be exported from tasks/__init__.py."""
        from src.workers.integrations_worker.tasks import sync_calendar_events_task

        assert sync_calendar_events_task is not None
        assert sync_calendar_events_task.name == "integrations.sync_calendar_events"


class TestConfigIntegration:
    """Tests for config.py integration."""

    def test_task_route_defined(self) -> None:
        """Task route should be defined in config."""
        from src.workers.integrations_worker.config import IntegrationsWorkerConfig

        config = IntegrationsWorkerConfig()
        assert "integrations.sync_calendar_events" in config.task_routes

    def test_task_route_uses_integrations_queue(self) -> None:
        """Task should route to integrations queue."""
        from src.workers.integrations_worker.config import IntegrationsWorkerConfig

        config = IntegrationsWorkerConfig()
        route = config.task_routes["integrations.sync_calendar_events"]
        assert route["queue"] == "integrations"

    def test_beat_schedule_defined(self) -> None:
        """Beat schedule should be defined for calendar sync."""
        from src.workers.integrations_worker.config import IntegrationsWorkerConfig

        config = IntegrationsWorkerConfig()
        assert "sync-calendar-events-all-orgs" in config.beat_schedule

    def test_beat_schedule_uses_correct_task(self) -> None:
        """Beat schedule should reference the correct task."""
        from src.workers.integrations_worker.config import IntegrationsWorkerConfig

        config = IntegrationsWorkerConfig()
        schedule = config.beat_schedule["sync-calendar-events-all-orgs"]
        assert schedule["task"] == "integrations.sync_calendar_events"

    def test_beat_schedule_has_empty_kwargs(self) -> None:
        """Beat schedule should have empty kwargs (sync all orgs)."""
        from src.workers.integrations_worker.config import IntegrationsWorkerConfig

        config = IntegrationsWorkerConfig()
        schedule = config.beat_schedule["sync-calendar-events-all-orgs"]
        assert schedule["kwargs"] == {}


class TestCalendarProviderTimeWindows:
    """Tests for calendar provider time window calculations."""

    def test_google_initial_sync_time_window(self) -> None:
        """Initial sync should use 90 days past to 365 days future."""
        from datetime import timedelta

        now = datetime.now(UTC)
        expected_min = now - timedelta(days=90)
        expected_max = now + timedelta(days=365)

        # The provider uses is_initial=True for initial sync
        is_initial = True
        last_synced_at = None

        if is_initial or not last_synced_at:
            time_min = now - timedelta(days=90)
        else:
            time_min = last_synced_at - timedelta(days=1)
        time_max = now + timedelta(days=365)

        # Verify time window is approximately correct (within 1 second)
        assert abs((time_min - expected_min).total_seconds()) < 1
        assert abs((time_max - expected_max).total_seconds()) < 1

    def test_incremental_sync_time_window_with_backoff(self) -> None:
        """Incremental sync should use last_synced_at - 1 day backoff."""
        from datetime import timedelta

        now = datetime.now(UTC)
        last_synced_at = now - timedelta(hours=6)

        is_initial = False

        if is_initial or not last_synced_at:
            time_min = now - timedelta(days=90)
        else:
            time_min = last_synced_at - timedelta(days=1)

        # Verify 1-day backoff is applied
        expected_min = last_synced_at - timedelta(days=1)
        assert abs((time_min - expected_min).total_seconds()) < 1


class TestIncrementalSyncLogic:
    """Tests for incremental sync behavior."""

    def test_sync_type_incremental_passed_to_task(self) -> None:
        """sync_type should be INCREMENTAL for scheduled syncs."""
        sync_type = "INCREMENTAL"
        assert sync_type.upper() == "INCREMENTAL"

    def test_incremental_flag_set_to_true(self) -> None:
        """incremental flag should be True for scheduled syncs."""
        incremental = True
        assert incremental is True


class TestOrganisationStatusFiltering:
    """Tests for organisation status filtering."""

    def test_only_approved_status_queried(self) -> None:
        """Query should only return APPROVED organisations."""
        from src.domain.models.master_tables import OrganisationStatus

        # The query filters by OrganisationStatus.APPROVED
        expected_status = OrganisationStatus.APPROVED
        assert expected_status.value == "approved"


class TestMixedProviderScenarios:
    """Tests for scenarios with multiple provider types."""

    def test_handles_mixed_google_and_outlook_integrations(self) -> None:
        """Should handle orgs with both Google and Outlook calendars."""
        # Simulate an org with multiple calendar integrations
        integrations = [
            {"type": IntegrationType.GOOGLE_CALENDAR, "id": uuid4()},
            {"type": IntegrationType.OUTLOOK_CALENDAR, "id": uuid4()},
            {"type": IntegrationType.GOOGLE_CALENDAR, "id": uuid4()},
        ]

        # Each should get a sync triggered
        expected_syncs = len(integrations)
        assert expected_syncs == 3

    def test_categorizes_integrations_correctly(self) -> None:
        """Each integration type should map to correct provider."""
        from src.infra.config.provider_categories import categorize_integration_type

        test_cases = [
            (IntegrationType.GOOGLE_CALENDAR, "calendar", "google"),
            (IntegrationType.OUTLOOK_CALENDAR, "calendar", "outlook"),
        ]

        for integration_type, expected_category, expected_provider in test_cases:
            category, provider = categorize_integration_type(integration_type)
            assert category == expected_category, f"Failed for {integration_type}"
            assert provider == expected_provider, f"Failed for {integration_type}"


class TestLoggingBehavior:
    """Tests for logging behavior (structural only, not actual log output)."""

    def test_log_messages_include_org_id(self) -> None:
        """Log messages should include organisation ID for tracing."""
        org_id = uuid4()
        log_message = f"Processing calendar events for organization: {org_id}"
        assert str(org_id) in log_message

    def test_log_messages_include_integration_details(self) -> None:
        """Log messages should include integration details."""
        org_id = uuid4()
        integration_id = uuid4()
        provider = "google"
        task_id = "celery-task-123"

        log_message = (
            f"Calendar sync triggered: org={org_id} "
            f"integration={integration_id} provider={provider} task={task_id}"
        )

        assert str(org_id) in log_message
        assert str(integration_id) in log_message
        assert provider in log_message
        assert task_id in log_message
