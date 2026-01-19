# Deal Channel Messages Sync Worker - Implementation Guide

## Overview

This document provides a comprehensive implementation guide for the **Deal Channel Messages Sync Worker** feature. This worker automatically fetches messages from Teams channels that have been linked to deals, enabling conversation context and insights from deal-related discussions.

## Problem Statement

Currently, users can link Teams channels (including team channels, group chats, and meeting chats) to deals via the `messaging_deal_links` table. However, the system does **NOT** automatically fetch messages from these linked channels.

**Impact:**
- No conversation context available for deals
- Cannot extract insights from deal-related discussions
- No tracking of communication activity within deal channels

## Proposed Solution

Create a new Celery worker task that runs **hourly** for each organisation to:

1. Find all channels linked to deals (via `messaging_deal_links`)
2. Fetch messages from those channels using Teams Graph API
3. Store messages in the existing `messaging_messages` table
4. Update channel sync state

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Celery Beat (Hourly)                                                        │
│  └── deal.trigger_deal_channel_messages_sync_all_orgs                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  For each APPROVED organisation:                                             │
│  └── deal.sync_deal_channel_messages(organisation_id)                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Per-Organisation Task:                                                      │
│  1. Query messaging_deal_links → get unique channel_ids                     │
│  2. Query messaging_channels → get channel details + connection_id          │
│  3. For each channel:                                                        │
│     - If Team channel → teams.fetch_messages(team_id, channel_id)           │
│     - If Chat → teams.fetch_chat_messages(chat_id)                          │
│  4. Upsert messages to messaging_messages                                   │
│  5. Update channel.last_synced_at                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Database Schema Reference

### Tables Involved

#### 1. `messaging_deal_links` (Read)
**Purpose:** Contains channel-to-deal associations created by users.

| Column | Type | Description |
|--------|------|-------------|
| `link_id` | UUID | Primary key |
| `channel_id` | UUID | FK to `messaging_channels` |
| `deal_id` | UUID | FK to `crm_deals` |
| `detection_method` | String | How link was created (manual, auto) |
| `is_deleted` | Boolean | Soft delete flag |

**Query Pattern:**
```sql
SELECT DISTINCT channel_id
FROM messaging_deal_links
WHERE channel_id IS NOT NULL
  AND is_deleted = false
```

#### 2. `messaging_channels` (Read)
**Purpose:** Contains channel metadata needed to call the correct Teams API.

| Column | Type | Description |
|--------|------|-------------|
| `channel_id` | UUID | Primary key (internal) |
| `external_channel_id` | String | Teams channel/chat ID |
| `integration_id` | UUID | FK to `knowledge_integrations` |
| `channel_type` | Enum | PUBLIC, PRIVATE, GROUP_MESSAGE, MEETING |
| `channel_metadata` | JSONB | Contains `team_id`, `chat_type`, `provider` |
| `last_synced_at` | DateTime | Last successful message sync |
| `is_monitored` | String | Whether channel should be synced |

**Key Metadata Fields:**
```json
{
  "provider": "teams",
  "team_id": "uuid-of-team",           // For team channels
  "chat_type": "group" | "meeting",     // For chats
  "membership_type": "standard" | "private"
}
```

#### 3. `messaging_messages` (Write)
**Purpose:** Store fetched messages.

| Column | Type | Description |
|--------|------|-------------|
| `message_id` | UUID | Primary key |
| `channel_id` | UUID | FK to `messaging_channels` |
| `integration_id` | UUID | FK to `knowledge_integrations` |
| `external_message_id` | String | Teams message ID |
| `participant_id` | UUID | Author (resolved to participant) |
| `external_user_id` | String | Teams user ID |
| `content` | Text | Message text content |
| `sent_at` | DateTime | When message was sent |
| `thread_id` | String | For threaded replies |
| `message_type` | Enum | TEXT, SYSTEM_EVENT, FILE, etc. |
| `reactions` | JSONB | Emoji reactions |
| `attachments` | JSONB | File attachments metadata |
| `mentions` | JSONB | List of mentioned user IDs |

**Unique Constraint:** `(integration_id, external_message_id)`

#### 4. `knowledge_integrations` (Read)
**Purpose:** Get Nango connection_id for API calls.

| Column | Type | Description |
|--------|------|-------------|
| `integration_id` | UUID | Primary key |
| `connection_id` | String | Nango connection identifier |
| `organisation_id` | UUID | FK to organisations |
| `integration_type` | String | "teams" |
| `status` | String | ACTIVE, INACTIVE |

---

## Implementation Details

### File Structure

```
src/
├── workers/
│   └── deal_worker/
│       ├── config.py                              # Add task routes + beat schedule
│       └── tasks/
│           └── deal_channel_messages_tasks.py     # NEW: Task implementations
└── service/
    └── crm/
        └── deal_channel_messages_service.py       # NEW: Service class
```

### 1. Task File: `deal_channel_messages_tasks.py`

**Location:** `src/workers/deal_worker/tasks/deal_channel_messages_tasks.py`

```python
"""Celery tasks for syncing messages from deal-linked channels.

This module provides Celery tasks for fetching messages from Teams channels
that have been linked to deals:
- trigger_deal_channel_messages_sync_all_orgs: Celery Beat trigger
- sync_deal_channel_messages_task: Per-organisation worker

Schedule: Runs hourly for all approved organisations.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlalchemy.sql.schema import Table

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.infra.database.factory import get_async_provider
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.deal_channel_messages_service import DealChannelMessagesService
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


@app.task(name="deal.trigger_deal_channel_messages_sync_all_orgs")
def trigger_deal_channel_messages_sync_all_orgs() -> dict[str, int]:
    """Trigger channel message sync for all active organisations.

    This task is scheduled by Celery Beat to run hourly.
    It enqueues one `deal.sync_deal_channel_messages` task per approved organisation.

    Returns:
        dict with organisations_queued count
    """
    logger.info("Triggering deal channel messages sync for all organisations")

    async def _run() -> int:
        provider = get_async_provider()
        async with provider.master_session() as session:
            org_c = cast(Table, cast(Any, Organisation).__table__).c
            stmt = select(Organisation).where(org_c.status == OrganisationStatus.APPROVED)
            result = await session.execute(stmt)
            orgs = result.scalars().all()

        logger.info("Found %d active organisations for channel message sync", len(orgs))

        for org in orgs:
            sync_deal_channel_messages_task.apply_async(
                args=[str(org.organisation_id)],
                headers=correlation_headers(),
            )

        return len(orgs)

    count = run_async(_run())
    logger.info("Enqueued channel message sync for %d organisations", count)
    return {"organisations_queued": count}


@app.task(bind=True, name="deal.sync_deal_channel_messages", acks_late=True, max_retries=3)
def sync_deal_channel_messages_task(self: Task, organisation_id: str) -> dict[str, int]:
    """Sync messages for all deal-linked channels in an organisation.

    For each channel linked to a deal:
    1. Determine channel type (Team channel vs Chat)
    2. Fetch messages using appropriate Teams API
    3. Upsert messages to messaging_messages table
    4. Update channel last_synced_at

    Args:
        organisation_id: Organisation UUID (string for JSON serialization)

    Returns:
        Stats dict: {channels_processed, messages_created, messages_updated, errors}
    """
    logger.info(f"Starting deal channel messages sync for org {organisation_id}")

    org_uuid = UUID(organisation_id)

    async def _run() -> dict[str, int]:
        async for session in acquire_org_async_session(org_uuid):
            service = DealChannelMessagesService(session, org_uuid)
            result = await service.sync_all_deal_channels()
            await session.commit()
            return result

        return {
            "channels_processed": 0,
            "messages_created": 0,
            "messages_updated": 0,
            "errors": 0,
        }

    try:
        result = cast(dict[str, int], run_async(_run()))
        logger.info(f"Channel messages sync completed for org {organisation_id}: {result}")
        return result
    except Exception as e:
        logger.error(
            f"Channel messages sync failed for org {organisation_id}: {e}",
            exc_info=True,
        )
        # Exponential backoff: 60s, 120s, 240s
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries)) from e
```

### 2. Service File: `deal_channel_messages_service.py`

**Location:** `src/service/crm/deal_channel_messages_service.py`

```python
"""Service for syncing messages from deal-linked channels.

This service handles:
1. Finding all channels linked to deals
2. Fetching messages from Teams API
3. Storing messages in the database
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync import KnowledgeIntegration, IntegrationType
from src.domain.models.organisation_tables.sync.messaging_models import (
    ChannelType,
    MessagingChannel,
    MessagingDealLink,
    MessagingMessage,
    MessageType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers.messaging import teams

logger = get_logger(__name__)

# Sync configuration
INITIAL_SYNC_DAYS = 90  # Days to look back on initial sync
INCREMENTAL_BUFFER_DAYS = 1  # Buffer for incremental sync


class DealChannelMessagesService:
    """Service to sync messages from deal-linked Teams channels."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id

    async def sync_all_deal_channels(self) -> dict[str, int]:
        """Sync messages for all channels linked to deals in this organisation.

        Returns:
            Stats dict with sync results
        """
        stats = {
            "channels_processed": 0,
            "messages_created": 0,
            "messages_updated": 0,
            "errors": 0,
        }

        # Step 1: Get all unique channel IDs linked to deals
        channel_ids = await self._get_deal_linked_channel_ids()
        if not channel_ids:
            logger.info("No deal-linked channels found for org %s", self.organisation_id)
            return stats

        logger.info(
            "Found %d deal-linked channels to sync for org %s",
            len(channel_ids),
            self.organisation_id,
        )

        # Step 2: Get channel details with integration info
        channels = await self._get_channels_with_integrations(channel_ids)

        # Step 3: Process each channel
        for channel, integration in channels:
            try:
                result = await self._sync_channel_messages(channel, integration)
                stats["channels_processed"] += 1
                stats["messages_created"] += result.get("created", 0)
                stats["messages_updated"] += result.get("updated", 0)
            except Exception as e:
                logger.error(
                    "Failed to sync channel %s: %s",
                    channel.channel_id,
                    str(e),
                    exc_info=True,
                )
                stats["errors"] += 1

        return stats

    async def _get_deal_linked_channel_ids(self) -> list[UUID]:
        """Get all unique channel IDs that are linked to deals."""
        stmt = (
            select(distinct(MessagingDealLink.channel_id))
            .where(
                MessagingDealLink.organisation_id == self.organisation_id,
                MessagingDealLink.channel_id.isnot(None),
                MessagingDealLink.is_deleted == False,  # noqa: E712
            )
        )
        result = await self.db.execute(stmt)
        return [row[0] for row in result.all()]

    async def _get_channels_with_integrations(
        self, channel_ids: list[UUID]
    ) -> list[tuple[MessagingChannel, KnowledgeIntegration]]:
        """Get channel details along with their integration info."""
        stmt = (
            select(MessagingChannel, KnowledgeIntegration)
            .join(
                KnowledgeIntegration,
                MessagingChannel.integration_id == KnowledgeIntegration.integration_id,
            )
            .where(
                MessagingChannel.channel_id.in_(channel_ids),
                MessagingChannel.is_deleted == False,  # noqa: E712
                KnowledgeIntegration.integration_type == IntegrationType.TEAMS.value,
                KnowledgeIntegration.status == "ACTIVE",
            )
        )
        result = await self.db.execute(stmt)
        return list(result.all())

    async def _sync_channel_messages(
        self,
        channel: MessagingChannel,
        integration: KnowledgeIntegration,
    ) -> dict[str, int]:
        """Sync messages for a single channel.

        Determines channel type and calls appropriate Teams API.
        """
        connection_id = integration.connection_id
        if not connection_id:
            raise ValueError(f"No connection_id for integration {integration.integration_id}")

        # Determine sync time range
        is_initial = channel.last_synced_at is None
        last_synced_at = channel.last_synced_at

        # Get channel metadata
        metadata = channel.channel_metadata or {}
        provider = metadata.get("provider", "teams")

        if provider != "teams":
            logger.warning(
                "Skipping non-Teams channel %s (provider: %s)",
                channel.channel_id,
                provider,
            )
            return {"created": 0, "updated": 0}

        # Initialize Nango client
        async with NangoClient() as nango:
            messages = await self._fetch_channel_messages(
                nango=nango,
                connection_id=connection_id,
                channel=channel,
                is_initial=is_initial,
                last_synced_at=last_synced_at,
            )

        # Store messages
        result = await self._store_messages(
            messages=messages,
            channel=channel,
            integration=integration,
        )

        # Update channel last_synced_at
        channel.last_synced_at = datetime.now(UTC)
        self.db.add(channel)
        await self.db.flush()

        return result

    async def _fetch_channel_messages(
        self,
        nango: NangoClient,
        connection_id: str,
        channel: MessagingChannel,
        is_initial: bool,
        last_synced_at: datetime | None,
    ) -> list[dict[str, Any]]:
        """Fetch messages from Teams using appropriate API based on channel type."""
        metadata = channel.channel_metadata or {}
        chat_type = metadata.get("chat_type")
        team_id = metadata.get("team_id")
        external_id = channel.external_channel_id

        # Determine which API to call based on channel type
        if chat_type in ("group", "meeting"):
            # This is a chat (group or meeting chat)
            logger.debug(
                "Fetching chat messages for channel %s (chat_id: %s)",
                channel.channel_id,
                external_id,
            )
            return await teams.fetch_chat_messages(
                nango=nango,
                connection_id=connection_id,
                is_initial=is_initial,
                last_synced_at=last_synced_at,
                chat_id=external_id,
            )
        elif team_id:
            # This is a Team channel
            logger.debug(
                "Fetching team channel messages for channel %s (team_id: %s, channel_id: %s)",
                channel.channel_id,
                team_id,
                external_id,
            )
            return await teams.fetch_messages(
                nango=nango,
                connection_id=connection_id,
                is_initial=is_initial,
                last_synced_at=last_synced_at,
                team_id=team_id,
                channel_id=external_id,
            )
        else:
            logger.warning(
                "Unable to determine channel type for %s (metadata: %s)",
                channel.channel_id,
                metadata,
            )
            return []

    async def _store_messages(
        self,
        messages: list[dict[str, Any]],
        channel: MessagingChannel,
        integration: KnowledgeIntegration,
    ) -> dict[str, int]:
        """Store fetched messages in the database.

        Uses upsert logic to handle duplicates.
        """
        created = 0
        updated = 0

        for msg_data in messages:
            external_id = msg_data.get("id")
            if not external_id:
                continue

            # Check if message exists
            existing = await self._get_message_by_external_id(
                integration.integration_id,
                external_id,
            )

            if existing:
                # Update existing message
                self._update_message_fields(existing, msg_data)
                self.db.add(existing)
                updated += 1
            else:
                # Create new message
                new_message = self._create_message(
                    msg_data=msg_data,
                    external_id=external_id,
                    channel=channel,
                    integration=integration,
                )
                self.db.add(new_message)
                created += 1

        await self.db.flush()

        logger.info(
            "Stored messages for channel %s: created=%d, updated=%d",
            channel.channel_id,
            created,
            updated,
        )

        return {"created": created, "updated": updated}

    async def _get_message_by_external_id(
        self,
        integration_id: UUID,
        external_id: str,
    ) -> MessagingMessage | None:
        """Get existing message by external ID."""
        stmt = select(MessagingMessage).where(
            MessagingMessage.integration_id == integration_id,
            MessagingMessage.external_message_id == external_id,
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    def _create_message(
        self,
        msg_data: dict[str, Any],
        external_id: str,
        channel: MessagingChannel,
        integration: KnowledgeIntegration,
    ) -> MessagingMessage:
        """Create a new MessagingMessage from Teams data."""
        fields = self._extract_message_fields(msg_data)

        return MessagingMessage(
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            channel_id=channel.channel_id,
            external_message_id=external_id,
            synced_at=datetime.now(UTC),
            **fields,
        )

    def _update_message_fields(
        self,
        message: MessagingMessage,
        msg_data: dict[str, Any],
    ) -> None:
        """Update message fields from Teams data."""
        fields = self._extract_message_fields(msg_data)
        for key, value in fields.items():
            setattr(message, key, value)
        message.synced_at = datetime.now(UTC)

    @staticmethod
    def _extract_message_fields(msg_data: dict[str, Any]) -> dict[str, Any]:
        """Extract message fields from Teams message data."""
        from_user = msg_data.get("from", {}).get("user", {})
        body = msg_data.get("body", {})

        # Determine message type
        message_type = MessageType.TEXT
        msg_type_str = msg_data.get("messageType", "message")
        if msg_type_str == "systemEventMessage":
            message_type = MessageType.SYSTEM_EVENT

        # Parse timestamps
        sent_at = DealChannelMessagesService._parse_iso_datetime(
            msg_data.get("createdDateTime")
        )
        edited_at = DealChannelMessagesService._parse_iso_datetime(
            msg_data.get("lastEditedDateTime")
        )

        return {
            "external_user_id": from_user.get("id"),
            "thread_id": msg_data.get("replyToId"),
            "message_type": message_type,
            "content": body.get("content"),
            "formatted_content": body.get("content") if body.get("contentType") == "html" else None,
            "sent_at": sent_at or datetime.now(UTC),
            "edited_at": edited_at,
            "reactions": msg_data.get("reactions", {}),
            "attachments": msg_data.get("attachments", []),
            "mentions": [
                m.get("mentioned", {}).get("user", {}).get("id")
                for m in msg_data.get("mentions", [])
            ],
            "is_deleted": "true" if msg_data.get("deletedDateTime") else "false",
            "is_bot_message": "false",
            "is_important": "true" if msg_data.get("importance") == "urgent" else "false",
            "message_metadata": {
                "provider": "teams",
                "importance": msg_data.get("importance"),
                "message_type": msg_type_str,
                "subject": msg_data.get("subject"),
                "team_id": msg_data.get("teamId"),
                "channel_id": msg_data.get("channelId"),
                "chat_id": msg_data.get("chatId"),
            },
        }

    @staticmethod
    def _parse_iso_datetime(value: str | None) -> datetime | None:
        """Parse ISO datetime string to datetime object."""
        if not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except (ValueError, AttributeError):
            return None
```

### 3. Worker Config Updates: `config.py`

**Location:** `src/workers/deal_worker/config.py`

Add to `task_routes`:

```python
# Deal channel message sync tasks
"deal.trigger_deal_channel_messages_sync_all_orgs": {
    "queue": "deal",
    "routing_key": "deal",
},
"deal.sync_deal_channel_messages": {
    "queue": "deal",
    "routing_key": "deal",
},
```

Add to `beat_schedule`:

```python
"sync-deal-channel-messages-all-orgs": {
    "task": "deal.trigger_deal_channel_messages_sync_all_orgs",
    "schedule": crontab(minute=0),  # Run every hour at :00
    "options": {
        "queue": "deal",
        "routing_key": "deal",
    },
},
```

---

## Sync Strategy

### Initial Sync
- **Timeframe:** Last 90 days of messages
- **Trigger:** First time a channel is synced (when `last_synced_at` is NULL)

### Incremental Sync
- **Timeframe:** Since `last_synced_at` minus 1-day buffer
- **Buffer Purpose:** Catches any messages that might have been missed due to timing

### Message Deduplication
- Uses unique constraint on `(integration_id, external_message_id)`
- Existing messages are updated with latest data
- New messages are created

---

## Error Handling

### Retry Strategy
- **Max Retries:** 3
- **Backoff:** Exponential (60s, 120s, 240s)
- **acks_late:** True (task acknowledged only after successful completion)

### Per-Channel Error Isolation
- Errors in one channel don't stop processing of other channels
- Error count tracked in stats
- Failed channels can be retried in next hourly run

### Logging
- INFO level for sync progress and completion
- ERROR level for failures with full exception info
- DEBUG level for detailed message processing

---

## Testing Guide

### Unit Tests

1. **Service Tests** (`test_deal_channel_messages_service.py`)
   - Test `_get_deal_linked_channel_ids` returns correct channels
   - Test `_fetch_channel_messages` calls correct API based on channel type
   - Test `_store_messages` creates new messages correctly
   - Test `_store_messages` updates existing messages correctly
   - Test `_extract_message_fields` extracts all fields correctly

2. **Task Tests** (`test_deal_channel_messages_tasks.py`)
   - Test trigger task enqueues per-org tasks
   - Test per-org task handles errors and retries
   - Test task returns correct stats

### Integration Tests

1. Test full sync flow with mocked Teams API
2. Test channel type detection (Team channel vs Chat)
3. Test incremental sync respects `last_synced_at`

### Manual Testing

```bash
# Trigger sync for all orgs (from Celery shell)
from src.workers.deal_worker.tasks.deal_channel_messages_tasks import (
    trigger_deal_channel_messages_sync_all_orgs,
    sync_deal_channel_messages_task,
)

# Trigger for all orgs
trigger_deal_channel_messages_sync_all_orgs.delay()

# Trigger for specific org
sync_deal_channel_messages_task.delay("org-uuid-here")
```

---

## Implementation Checklist

### Phase 1: Core Implementation
- [ ] Create `deal_channel_messages_tasks.py` with both tasks
- [ ] Create `DealChannelMessagesService` class
- [ ] Add task routes to `config.py`
- [ ] Add beat schedule to `config.py`
- [ ] Write unit tests for service
- [ ] Write unit tests for tasks

### Phase 2: Testing & Validation
- [ ] Test with real Teams integration locally
- [ ] Verify initial sync fetches 90 days
- [ ] Verify incremental sync works correctly
- [ ] Verify error handling and retries
- [ ] Check message deduplication

### Phase 3: Deployment
- [ ] Deploy to staging
- [ ] Monitor first few hourly runs
- [ ] Verify messages appearing in database
- [ ] Deploy to production

---

## Dependencies

### Existing Code Used
| Module | Purpose |
|--------|---------|
| `src/workers/deal_worker/celery_app` | Celery app instance |
| `src/api/dependencies/organisation_dependencies` | Session acquisition |
| `src/domain/models/organisation_tables/sync/messaging_models` | Data models |
| `src/workers/integrations_worker/providers/messaging/teams` | Teams API functions |
| `src/service/integrations/nango_client` | Nango API client |
| `src/infra/utils/async_runner` | Async to sync bridge |
| `src/infra/utils/logger` | Logging |

### Configuration Required
- Celery Beat must be running
- Teams integration must be active for the organisation
- Channels must be linked to deals via `messaging_deal_links`

---

## Open Questions / Decisions

1. **Participant Resolution:** Should we resolve `external_user_id` to `participant_id` during sync, or defer to a separate job?
   - **Recommendation:** Defer - keep sync fast, resolve participants separately

2. **Message Limits:** Teams API returns max 50 messages per request. Should we implement pagination for initial sync?
   - **Recommendation:** Start without pagination (50 per channel is reasonable), add later if needed

3. **Rate Limiting:** Should we add rate limiting between channel syncs?
   - **Recommendation:** Not initially - Teams Graph API has generous limits, add if needed

4. **Channel Activity Update:** Should we update `last_activity_at` on channels based on fetched messages?
   - **Recommendation:** Yes, update based on most recent message `sent_at`

---

## Success Criteria

1. Worker runs hourly without errors for all approved organisations
2. Messages from deal-linked channels are stored in `messaging_messages`
3. Incremental sync only fetches new messages (not duplicates)
4. Error handling prevents one channel failure from stopping entire org sync
5. Stats are logged for monitoring and debugging
