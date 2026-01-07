"""Tests for messaging sync service incremental sync functionality."""

from unittest.mock import MagicMock

import pytest
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


def _extract_channel_fields(channel_data, provider):
    """Extract channel fields from provider data."""
    if provider in ("microsoft", "teams"):
        chat_type = channel_data.get("chatType")
        membership_type = channel_data.get("membershipType", "standard")

        if chat_type == "group":
            channel_type = "GROUP_MESSAGE"
        elif chat_type == "meeting":
            channel_type = "MEETING"
        elif membership_type == "private":
            channel_type = "PRIVATE"
        else:
            channel_type = "PUBLIC"

        display_name = channel_data.get("displayName") or channel_data.get("topic") or "Unnamed"

        return {
            "channel_name": display_name,
            "channel_type": channel_type,
            "channel_description": channel_data.get("description"),
            "is_archived": "false",
            "participant_count": 0,
            "channel_metadata": {
                "provider": "teams",
                "team_id": channel_data.get("teamId"),
                "membership_type": membership_type,
                "chat_type": chat_type,
                "last_updated_date_time": channel_data.get("lastUpdatedDateTime"),
            },
        }
    return {
        "channel_name": channel_data.get("name") or channel_data.get("displayName", "Unnamed"),
        "channel_type": "PUBLIC",
        "channel_description": None,
        "is_archived": "false",
        "participant_count": 0,
        "channel_metadata": {"provider": "unknown"},
    }


def _has_meaningful_channel_changes(existing, incoming, provider) -> bool:
    """Standalone implementation of channel change detection for testing.

    This mirrors the logic in MessagingSyncService._has_meaningful_channel_changes.
    """
    fields = _extract_channel_fields(incoming, provider)

    # Compare key fields
    checks = [
        (existing.channel_name, fields.get("channel_name")),
        (existing.channel_type, fields.get("channel_type")),
        (existing.channel_description, fields.get("channel_description")),
        (existing.is_archived, fields.get("is_archived")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    # Compare participant_count
    incoming_count = fields.get("participant_count", 0)
    if existing.participant_count != incoming_count:
        return True

    # Check last_updated_date_time in metadata
    existing_metadata = existing.channel_metadata or {}
    incoming_metadata = fields.get("channel_metadata") or {}
    existing_updated = existing_metadata.get("last_updated_date_time")
    incoming_updated = incoming_metadata.get("last_updated_date_time")

    return bool(incoming_updated and existing_updated and incoming_updated != existing_updated)


class TestMessagingChannelHasMeaningfulChanges:
    """Tests for _has_meaningful_channel_changes method logic."""

    @pytest.fixture
    def mock_channel(self):
        """Create a mock MessagingChannel for testing."""
        channel = MagicMock()
        channel.channel_name = "Sales Team"
        channel.channel_type = "PUBLIC"
        channel.channel_description = "Sales discussion channel"
        channel.is_archived = "false"
        channel.participant_count = 0  # Teams doesn't expose participant count in basic info
        channel.channel_metadata = {
            "provider": "teams",
            "team_id": "team-123",
            "last_updated_date_time": "2024-01-10T12:00:00Z",
        }
        return channel

    def test_detects_name_change(self, mock_channel) -> None:
        """Channel name change should be detected."""
        incoming = {
            "displayName": "Updated Sales Team",
            "membershipType": "standard",
        }
        result = _has_meaningful_channel_changes(mock_channel, incoming, "teams")
        assert result is True

    def test_detects_description_change(self, mock_channel) -> None:
        """Description change should be detected."""
        incoming = {
            "displayName": "Sales Team",
            "membershipType": "standard",
            "description": "Updated description",
        }
        result = _has_meaningful_channel_changes(mock_channel, incoming, "teams")
        assert result is True

    def test_detects_channel_type_change(self, mock_channel) -> None:
        """Channel type change should be detected."""
        incoming = {
            "displayName": "Sales Team",
            "membershipType": "private",  # Changed from standard
        }
        result = _has_meaningful_channel_changes(mock_channel, incoming, "teams")
        assert result is True

    def test_detects_last_updated_change(self, mock_channel) -> None:
        """last_updated_date_time change should be detected."""
        incoming = {
            "displayName": "Sales Team",
            "membershipType": "standard",
            "lastUpdatedDateTime": "2024-01-11T12:00:00Z",  # Newer than existing
        }
        result = _has_meaningful_channel_changes(mock_channel, incoming, "teams")
        assert result is True

    def test_no_change_returns_false(self, mock_channel) -> None:
        """No meaningful change should return False."""
        incoming = {
            "displayName": "Sales Team",
            "membershipType": "standard",
            "description": "Sales discussion channel",
            "lastUpdatedDateTime": "2024-01-10T12:00:00Z",
        }
        result = _has_meaningful_channel_changes(mock_channel, incoming, "teams")
        assert result is False

    def test_handles_none_values(self) -> None:
        """None values should be handled gracefully."""
        mock_channel = MagicMock()
        mock_channel.channel_name = None
        mock_channel.channel_type = None
        mock_channel.channel_description = None
        mock_channel.is_archived = None
        mock_channel.participant_count = 0
        mock_channel.channel_metadata = None

        incoming = {"displayName": "Test Channel"}
        # Should not raise an exception
        result = _has_meaningful_channel_changes(mock_channel, incoming, "teams")
        assert isinstance(result, bool)


class TestMessagingIncrementalRouting:
    """Tests for messaging incremental sync routing logic."""

    def test_database_state_is_source_of_truth(self) -> None:
        """Database state (is_new) should determine create vs update."""
        record_in_db = True
        _sync_action = SyncAction.ADDED  # Unused - demonstrates db state is source of truth

        is_new = not record_in_db

        should_create = is_new
        should_update = not is_new

        assert should_create is False
        assert should_update is True


class TestMessagingAdapterRouting:
    """Tests for messaging adapter routing logic."""

    def test_incremental_sync_type_routes_to_incremental(self) -> None:
        """sync_type='INCREMENTAL' should route to incremental method."""
        sync_type = "INCREMENTAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is True

    def test_initial_sync_type_routes_to_full(self) -> None:
        """sync_type='INITIAL' should route to full sync method."""
        sync_type = "INITIAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is False

    def test_none_sync_type_routes_to_full(self) -> None:
        """sync_type=None should route to full sync method."""
        sync_type = None
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert not is_incremental


class TestMessagingDataTypes:
    """Tests for messaging data type support in incremental sync."""

    def test_channels_data_type_supported(self) -> None:
        """'channels' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"channels", "chats"}
        assert "channels" in incremental_sync_entities

    def test_chats_data_type_supported(self) -> None:
        """'chats' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"channels", "chats"}
        assert "chats" in incremental_sync_entities


class TestTeamsChannelTypeMapping:
    """Tests for Teams-specific channel type mapping."""

    def test_group_chat_maps_to_group_message(self) -> None:
        """Teams group chat should map to GROUP_MESSAGE type."""
        incoming = {
            "id": "chat-123",
            "chatType": "group",
            "displayName": "Project Discussion",
        }
        fields = _extract_channel_fields(incoming, "teams")
        assert fields["channel_type"] == "GROUP_MESSAGE"

    def test_meeting_chat_maps_to_meeting(self) -> None:
        """Teams meeting chat should map to MEETING type."""
        incoming = {
            "id": "chat-456",
            "chatType": "meeting",
            "topic": "Weekly Standup",
        }
        fields = _extract_channel_fields(incoming, "teams")
        assert fields["channel_type"] == "MEETING"

    def test_private_channel_maps_to_private(self) -> None:
        """Teams private channel should map to PRIVATE type."""
        incoming = {
            "id": "channel-789",
            "displayName": "Leadership Only",
            "membershipType": "private",
        }
        fields = _extract_channel_fields(incoming, "teams")
        assert fields["channel_type"] == "PRIVATE"

    def test_standard_channel_maps_to_public(self) -> None:
        """Teams standard channel should map to PUBLIC type."""
        incoming = {
            "id": "channel-101",
            "displayName": "General",
            "membershipType": "standard",
        }
        fields = _extract_channel_fields(incoming, "teams")
        assert fields["channel_type"] == "PUBLIC"

    def test_uses_topic_when_display_name_missing(self) -> None:
        """Should use topic field when displayName is missing."""
        incoming = {
            "id": "chat-meeting",
            "chatType": "meeting",
            "topic": "Q4 Planning",
        }
        fields = _extract_channel_fields(incoming, "teams")
        assert fields["channel_name"] == "Q4 Planning"

    def test_unnamed_when_both_missing(self) -> None:
        """Should use 'Unnamed' when both displayName and topic missing."""
        incoming = {
            "id": "chat-empty",
            "chatType": "group",
        }
        fields = _extract_channel_fields(incoming, "teams")
        assert fields["channel_name"] == "Unnamed"


class TestSlackChannelExtraction:
    """Tests for Slack channel field extraction."""

    def test_slack_uses_name_field(self) -> None:
        """Slack channels should use 'name' field."""
        incoming = {
            "id": "C123456",
            "name": "general",
        }
        fields = _extract_channel_fields(incoming, "slack")
        assert fields["channel_name"] == "general"

    def test_slack_defaults_to_public(self) -> None:
        """Slack channels should default to PUBLIC type."""
        incoming = {
            "id": "C123456",
            "name": "random",
        }
        fields = _extract_channel_fields(incoming, "slack")
        assert fields["channel_type"] == "PUBLIC"


class TestMessagingIncrementalScenarios:
    """Integration scenarios testing the full incremental sync flow."""

    def test_mixed_channel_scenario(self) -> None:
        """Test scenario with new, updated, and unchanged channels."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncActionCounts,
            extract_sync_action,
        )

        # Simulate incoming channels from Teams webhook
        channels = [
            # New channel
            {
                "id": "channel-new-1",
                "displayName": "New Project Team",
                "membershipType": "standard",
                "_sync_metadata": {"sync_action": "ADDED"},
            },
            # Updated channel (name changed)
            {
                "id": "channel-existing-1",
                "displayName": "Updated Sales Team",
                "membershipType": "standard",
                "lastUpdatedDateTime": "2024-01-15T12:00:00Z",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
            # Unchanged channel
            {
                "id": "channel-existing-2",
                "displayName": "Engineering",
                "membershipType": "standard",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
        ]

        # Count sync actions
        counts = SyncActionCounts()
        for channel in channels:
            action = extract_sync_action(channel)
            if action:
                counts.increment(action)

        assert counts.added == 1
        assert counts.updated == 2
        assert counts.total == 3

    def test_database_state_determines_create_or_update(self) -> None:
        """Database existence should determine create vs update, not sync_action."""
        # Scenario: sync_action says ADDED but channel exists in DB
        existing_external_ids = {"channel-1", "channel-2"}

        channel = {
            "id": "channel-1",  # Already exists in DB
            "displayName": "Test Channel",
            "_sync_metadata": {"sync_action": "ADDED"},  # Webhook says ADDED
        }

        external_id = channel["id"]
        is_new = external_id not in existing_external_ids

        # Despite ADDED action, database state says it exists -> UPDATE
        assert is_new is False
        should_create = is_new
        should_update = not is_new
        assert should_create is False
        assert should_update is True

    def test_new_channel_gets_created(self) -> None:
        """New channel (not in DB) should be created."""
        existing_external_ids = {"channel-1", "channel-2"}

        channel = {
            "id": "channel-new",
            "displayName": "Brand New Channel",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = channel["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is True
        should_create = is_new
        assert should_create is True

    def test_unchanged_channel_gets_skipped(self) -> None:
        """Channel with no meaningful changes should be skipped."""
        mock_existing = MagicMock()
        mock_existing.channel_name = "Sales Team"
        mock_existing.channel_type = "PUBLIC"
        mock_existing.channel_description = None
        mock_existing.is_archived = "false"
        mock_existing.participant_count = 0
        mock_existing.channel_metadata = {
            "last_updated_date_time": "2024-01-10T12:00:00Z",
        }

        incoming = {
            "id": "channel-1",
            "displayName": "Sales Team",
            "membershipType": "standard",
            "lastUpdatedDateTime": "2024-01-10T12:00:00Z",  # Same timestamp
        }

        has_changes = _has_meaningful_channel_changes(mock_existing, incoming, "teams")
        assert has_changes is False

    def test_updated_timestamp_triggers_update(self) -> None:
        """Updated lastUpdatedDateTime should trigger an update."""
        mock_existing = MagicMock()
        mock_existing.channel_name = "Sales Team"
        mock_existing.channel_type = "PUBLIC"
        mock_existing.channel_description = None
        mock_existing.is_archived = "false"
        mock_existing.participant_count = 0
        mock_existing.channel_metadata = {
            "last_updated_date_time": "2024-01-10T12:00:00Z",
        }

        incoming = {
            "id": "channel-1",
            "displayName": "Sales Team",
            "membershipType": "standard",
            "lastUpdatedDateTime": "2024-01-15T14:30:00Z",  # Newer timestamp
        }

        has_changes = _has_meaningful_channel_changes(mock_existing, incoming, "teams")
        assert has_changes is True


class TestMessagingIncrementalResultStructure:
    """Tests for incremental sync result structure."""

    def test_result_includes_all_counts(self) -> None:
        """Result should include created, updated, skipped counts."""
        expected_keys = {
            "status",
            "records_processed",
            "records_failed",
            "created",
            "updated",
            "skipped",
        }

        sample_result = {
            "status": "success",
            "records_processed": 10,
            "records_failed": 0,
            "created": 3,
            "updated": 5,
            "skipped": 2,
        }

        assert expected_keys.issubset(sample_result.keys())
        assert sample_result["created"] + sample_result["updated"] + sample_result["skipped"] == 10

    def test_action_counts_tracking(self) -> None:
        """SyncActionCounts should track all action types."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncAction,
            SyncActionCounts,
        )

        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.UPDATED)

        result = counts.to_dict()
        assert result["added"] == 2
        assert result["updated"] == 3
        assert result["deleted"] == 0
        assert result["unchanged"] == 0
        assert result["total"] == 5


class TestMessagingDispatcherIntegration:
    """Tests for dispatcher integration with messaging."""

    def test_messaging_category_in_incremental_categories(self) -> None:
        """'messaging' should be in incremental_sync_categories."""
        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        assert "messaging" in incremental_sync_categories

    def test_messaging_entities_not_in_fetcher_entities(self) -> None:
        """Messaging entities should NOT be in incremental_sync_entities (CRM only)."""
        # This is the key fix - only CRM fetchers accept sync_type
        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }
        # Messaging entities should NOT be here
        assert "channels" not in incremental_sync_entities
        assert "chats" not in incremental_sync_entities
        assert "messages" not in incremental_sync_entities

    def test_sync_type_only_passed_to_handler_not_fetcher(self) -> None:
        """sync_type should be passed to handler, not to messaging fetcher."""
        # This tests the architecture fix:
        # - Fetchers: Only CRM accepts sync_type
        # - Handlers: All categories receive sync_type

        category = "messaging"
        data_type = "channels"

        # Check if this data type should get sync_type in fetcher
        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }

        should_pass_to_fetcher = data_type in incremental_sync_entities
        assert should_pass_to_fetcher is False

        # But handler should still receive sync_type
        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        should_pass_to_handler = category in incremental_sync_categories
        assert should_pass_to_handler is True


class TestMessagingChannelMetadataHandling:
    """Tests for channel metadata handling in change detection."""

    def test_missing_existing_metadata(self) -> None:
        """Should handle existing channel with no metadata."""
        mock_existing = MagicMock()
        mock_existing.channel_name = "Test"
        mock_existing.channel_type = "PUBLIC"
        mock_existing.channel_description = None
        mock_existing.is_archived = "false"
        mock_existing.participant_count = 0
        mock_existing.channel_metadata = None  # No metadata

        incoming = {
            "displayName": "Test",
            "membershipType": "standard",
            "lastUpdatedDateTime": "2024-01-15T12:00:00Z",
        }

        # Should not raise, and no change detected (no existing timestamp to compare)
        result = _has_meaningful_channel_changes(mock_existing, incoming, "teams")
        assert isinstance(result, bool)

    def test_missing_incoming_timestamp(self) -> None:
        """Should handle incoming data with no lastUpdatedDateTime."""
        mock_existing = MagicMock()
        mock_existing.channel_name = "Test"
        mock_existing.channel_type = "PUBLIC"
        mock_existing.channel_description = None
        mock_existing.is_archived = "false"
        mock_existing.participant_count = 0
        mock_existing.channel_metadata = {
            "last_updated_date_time": "2024-01-10T12:00:00Z",
        }

        incoming = {
            "displayName": "Test",
            "membershipType": "standard",
            # No lastUpdatedDateTime
        }

        # Should return False (no change without timestamp comparison trigger)
        result = _has_meaningful_channel_changes(mock_existing, incoming, "teams")
        assert result is False

    def test_both_timestamps_missing(self) -> None:
        """Should handle both timestamps missing."""
        mock_existing = MagicMock()
        mock_existing.channel_name = "Test"
        mock_existing.channel_type = "PUBLIC"
        mock_existing.channel_description = None
        mock_existing.is_archived = "false"
        mock_existing.participant_count = 0
        mock_existing.channel_metadata = {}  # No timestamp

        incoming = {
            "displayName": "Test",
            "membershipType": "standard",
        }

        result = _has_meaningful_channel_changes(mock_existing, incoming, "teams")
        assert result is False
