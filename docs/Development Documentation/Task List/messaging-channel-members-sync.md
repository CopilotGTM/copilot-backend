# Task: Populate MessagingChannelMember Table

## Status: TODO

## Problem Statement

The `messaging_channel_members` table exists in the schema but is **currently not populated**. No code writes to this table during Teams channel or message sync.

This table tracks channel/chat membership for compliance and analytics, linking channels to their participants with join/leave timestamps and roles.

## Table Schema

```python
class MessagingChannelMember(SyncedRecordMixin, table=True):
    """Tracks channel membership for compliance and analytics."""

    __tablename__ = "messaging_channel_members"

    membership_id: UUID          # Primary key
    channel_id: UUID             # FK to messaging_channels
    participant_id: UUID         # FK to integration_participants
    joined_at: datetime          # When participant joined
    left_at: datetime | None     # When left (NULL if still active)
    member_role: str | None      # Role: owner, admin, member, guest
    added_by_participant_id: UUID | None  # Who added this member
    member_metadata: dict        # Provider-specific metadata
```

## Use Cases

### 1. Deal Stakeholder Tracking

- **Description**: Identify all participants in deal-related channels/chats
- **Value**: Automatically discover stakeholders involved in deal conversations
- **Query**: "Who is involved in the Acme Corp deal channel?"

### 2. Channel Participant UI

- **Description**: Display channel members in the frontend
- **Value**: Users can see who has access to specific conversations
- **Query**: "Show me all members of this Teams channel"

### 3. Stakeholder AI Insights

- **Description**: Feed membership data to AI for stakeholder analysis
- **Value**: Identify key participants, decision makers, engagement patterns
- **Query**: "Who are the most active participants in this deal's communications?"

### 4. Access Control Verification

- **Description**: Verify user has legitimate access to channel content
- **Value**: Compliance - ensure users only see channels they're members of
- **Query**: "Is User X a member of Channel Y?"

### 5. Engagement Analytics

- **Description**: Track active vs passive participants
- **Value**: Identify lurkers, measure engagement, find champions
- **Query**: "Who hasn't contributed to this channel in 30 days?"

### 6. Membership History

- **Description**: Track join/leave events over time
- **Value**: Audit trail, understand team changes during deal lifecycle
- **Query**: "Who left this channel during the negotiation phase?"

## Microsoft Graph API Endpoints

### Team Channel Members

```
GET /teams/{teamId}/channels/{channelId}/members
```

Response:

```json
{
  "value": [
    {
      "id": "member-id",
      "roles": ["owner"],
      "displayName": "John Doe",
      "userId": "user-uuid",
      "email": "john@company.com",
      "tenantId": "tenant-uuid",
      "visibleHistoryStartDateTime": "2024-01-15T00:00:00Z"
    }
  ]
}
```

### Chat Members

```
GET /chats/{chatId}/members
```

Response:

```json
{
  "value": [
    {
      "id": "member-id",
      "roles": [],
      "displayName": "Jane Smith",
      "userId": "user-uuid",
      "email": "jane@company.com",
      "tenantId": "tenant-uuid"
    }
  ]
}
```

## Proposed Implementation

### 1. Add `fetch_channel_members()` to `teams.py`

```python
async def fetch_channel_members(
    *,
    nango: NangoClient,
    connection_id: str,
    team_id: str,
    channel_id: str,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch members of a specific Teams channel.
    
    Uses Microsoft Graph API: GET /teams/{teamId}/channels/{channelId}/members
    """
    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)
    
    endpoint = f"/v1.0/teams/{team_id}/channels/{channel_id}/members"
    
    response = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
    )
    
    members = []
    if isinstance(response, dict) and "value" in response:
        members = response["value"]
    
    # Enrich with channel context
    for member in members:
        member["teamId"] = team_id
        member["channelId"] = channel_id
    
    return members
```

### 2. Add `fetch_chat_members()` to `teams.py`

```python
async def fetch_chat_members(
    *,
    nango: NangoClient,
    connection_id: str,
    chat_id: str,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch members of a specific Teams chat.
    
    Uses Microsoft Graph API: GET /chats/{chatId}/members
    """
    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)
    
    endpoint = f"/v1.0/chats/{chat_id}/members"
    
    response = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
    )
    
    members = []
    if isinstance(response, dict) and "value" in response:
        members = response["value"]
    
    # Enrich with chat context
    for member in members:
        member["chatId"] = chat_id
    
    return members
```

### 3. Update Channel Sync Handler

Modify the channel sync dispatcher to also fetch and store members:

```python
# In dispatcher.py or sync handler
async def process_channel_with_members(channel_data, ...):
    # 1. Save channel to messaging_channels
    channel = await save_channel(channel_data)
    
    # 2. Fetch members for this channel
    members = await fetch_channel_members(
        nango=nango,
        connection_id=connection_id,
        team_id=channel_data["teamId"],
        channel_id=channel_data["id"],
    )
    
    # 3. For each member, ensure IntegrationParticipant exists
    for member in members:
        participant = await get_or_create_participant(member)
        
        # 4. Create/update MessagingChannelMember
        await upsert_channel_member(
            channel_id=channel.channel_id,
            participant_id=participant.participant_id,
            joined_at=member.get("visibleHistoryStartDateTime"),
            member_role=member.get("roles", [])[0] if member.get("roles") else "member",
        )
```

### 4. Register in Provider Registry

```python
# In registry.py
from .messaging.teams import (
    fetch_channels as teams_fetch_channels,
    fetch_channel_members as teams_fetch_channel_members,
    fetch_chat_members as teams_fetch_chat_members,
    ...
)

MESSAGING_REGISTRY = {
    ("messaging", "teams"): {
        "channels": teams_fetch_channels,
        "channel_members": teams_fetch_channel_members,  # NEW
        "chat_members": teams_fetch_chat_members,        # NEW
        ...
    }
}
```

## Sync Strategy

### Option A: Sync with Channels (Recommended)

- **When**: During `sync_teams_channels_task`
- **How**: After fetching each channel, immediately fetch its members
- **Pros**: Members are always up-to-date with channels, single scheduled task
- **Cons**: Additional API calls (1 per channel)

### Option B: Separate Scheduled Task

- **When**: Separate Celery Beat task, less frequently (daily)
- **How**: Query all channels, fetch members for each
- **Pros**: Doesn't slow down channel sync
- **Cons**: Members may be stale between syncs

### Recommendation

**Use Option A** - Sync members during channel sync because:

1. Membership changes less frequently than messages
2. Members are essential context when viewing channels
3. Single source of truth for channel + member data
4. Avoids orphaned member records

## API Rate Limiting Considerations

Microsoft Graph rate limits:

- Per-app: 2000 requests per second
- Per-tenant: 10,000 requests per 10 minutes

With 50 channels:

- Channel fetch: 50 API calls
- Member fetch: 50 additional API calls (1 per channel)
- Total: ~100 API calls per sync

This is well within limits. For large tenants with 500+ channels, consider:

- Batching requests
- Caching member data (members don't change often)
- Delta queries if supported

## Acceptance Criteria

- [ ] `fetch_channel_members()` function implemented in `teams.py`
- [ ] `fetch_chat_members()` function implemented in `teams.py`
- [ ] Channel sync handler populates `messaging_channel_members` table
- [ ] Chat sync handler populates `messaging_channel_members` table
- [ ] `IntegrationParticipant` records created for new members
- [ ] Member roles correctly mapped (owner, admin, member, guest)
- [ ] Join timestamps captured from API response
- [ ] Test script updated to verify member sync

## Dependencies

- Teams integration working (`fetch_channels` functional)
- `IntegrationParticipant` table populated for identity resolution
- Nango proxy working for Microsoft Graph API calls

## Related Tasks

- [stakeholder-ai-insights-generation.md](stakeholder-ai-insights-generation.md) - Uses member data for stakeholder analysis
- [stakeholder-tracking-enhancements.md](stakeholder-tracking-enhancements.md) - Links channel members to deal stakeholders
