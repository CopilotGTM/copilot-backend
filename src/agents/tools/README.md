# Agent Tools

This directory contains tools that agents can use to retrieve information and perform actions. Tools are organized by category and registered through the `ToolRegistry`.

## Directory Structure

```
tools/
├── __init__.py
├── base.py                 # Base utilities (db_session_lock, execute_with_capabilities)
├── registry.py             # ToolRegistry and agent-tool mappings
├── transparent_tools.py    # AgentCapabilities for SSE event streaming
├── communication/          # Email/communication tools
│   ├── list_emails.py
│   └── get_email_details.py
├── crm/                    # CRM/Stakeholder tools
│   └── list_stakeholders.py
├── intelligence/           # Meeting intelligence tools
│   ├── list_meetings.py
│   ├── get_meeting_details.py
│   └── get_meeting_transcript.py
└── search/                 # Search tools
    ├── internal_knowledge_base.py
    └── internet_search.py
```

---

## Tool Categories

### Search Tools

| Tool | Factory Function | Description |
|------|------------------|-------------|
| `search_internal_knowledge` | `create_search_internal_knowledge` | Search organisation documents and knowledge base |
| `search_internet` | `create_search_internet` | Search the internet for external information |

### CRM Tools

| Tool | Factory Function | Description |
|------|------------------|-------------|
| `list_deal_stakeholders` | `create_list_deal_stakeholders` | List stakeholders for a deal, optionally filtered to one person |

### Intelligence Tools

| Tool | Factory Function | Description |
|------|------------------|-------------|
| `list_deal_meetings` | `create_list_deal_meetings` | List meetings associated with a deal |
| `get_meeting_details` | `create_get_meeting_details` | Get meeting details including AI summary |
| `get_meeting_transcript` | `create_get_meeting_transcript` | Get meeting transcript (full or segmented) |

### Communication Tools

| Tool | Factory Function | Description |
|------|------------------|-------------|
| `list_deal_emails` | `create_list_deal_emails` | List emails associated with a deal |
| `get_email_details` | `create_get_email_details` | Get detailed info about a specific email |

---

## Tool Details

### `list_deal_stakeholders`

**Purpose**: List stakeholders (contacts) associated with a deal, or return one stakeholder when `stakeholder_id` or `query` is provided.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `deal_id` | str | No | context.deal_id | UUID of the deal |
| `stakeholder_id` | str | No | None | CRMDealParticipant UUID to return as a one-item list |
| `query` | str | No | None | Case-insensitive filter for name, email, title, or department |
| `limit` | int | No | 20 | Max results (max: 50) |

**Returns**: List of stakeholder summaries with:

- `participant_id`, `deal_id`, `contact_id`, `email`, `name`, `job_title`, `department`
- `stance`: CHAMPION, SUPPORTER, NEUTRAL, DETRACTOR, BLOCKER, UNKNOWN
- `decision_power`: DECISION_MAKER, INFLUENCER, EVALUATOR, GATEKEEPER, END_USER, UNKNOWN
- `temperature`: HOT, WARM, COLD
- `motivation`, `pain_points`, `keywords`, `external_links`
- `last_contact_at`, `source`, `stakeholder_research`, `last_researched_at`

---

### `list_deal_meetings`

**Purpose**: List all meetings associated with a deal.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `deal_id` | str | No | context.deal_id | UUID of the deal |
| `limit` | int | No | 10 | Max results (max: 50) |
| `include_past` | bool | No | True | Include past meetings |
| `include_upcoming` | bool | No | True | Include future meetings |

**Returns**: List of meeting summaries with:

- `event_id`, `title`, `start_at`, `end_at`, `status`
- `organiser_name`, `organiser_email`, `attendee_count`
- `attendees`: List of {email, name, response_status, is_organiser}
- `conference_link`, `location`
- `has_transcript`, `has_summary`, `has_prep`, `has_followup`
- `association_status`: SUGGESTED or CONFIRMED
- `match_score`: Confidence of deal association

---

### `get_meeting_details`

**Purpose**: Get comprehensive details about a specific meeting.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `event_id` | str | Yes | - | Calendar event UUID |
| `include_transcript` | bool | No | False | Include full transcript |
| `include_summary` | bool | No | True | Include AI summary |
| `include_prep` | bool | No | False | Include meeting prep |
| `include_followup` | bool | No | True | Include follow-up actions |

**Returns**: Meeting details including:

- Basic: `event_id`, `title`, `description`, `start_at`, `end_at`, `duration_minutes`
- `organiser`: {name, email}
- `attendees`: Full attendee list
- `transcript`: {available, status, source, language, full_text}
- `summary`: {status, data} with key_points, decisions, concerns
- `meeting_prep`: {status, data} with stakeholder_insights, suggested_topics
- `followup`: {status, data} with action_items, next_steps

---

### `get_meeting_transcript`

**Purpose**: Get the transcript for a meeting (optimized for large transcripts).

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `event_id` | str | Yes | - | Calendar event UUID |
| `output_format` | str | No | "full" | "full" or "segments" |
| `max_length` | int | No | None | Truncate to N characters |

**Returns** (format="full"):

- `event_id`, `title`, `meeting_date`, `duration_seconds`
- `transcript_available`, `source`, `language`
- `full_text`: Complete transcript
- `truncated`: True if max_length applied
- `total_length`: Original length

**Returns** (format="segments"):

- Same metadata fields
- `segments`: List of {index, speaker_name, content, start_time_seconds, end_time_seconds}
- `segment_count`: Number of segments

---

### `list_deal_emails`

**Purpose**: List emails associated with a deal via DealEmailAssociation.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `deal_id` | str | No | context.deal_id | UUID of the deal |
| `limit` | int | No | 20 | Max results |
| `direction` | str | No | None | Filter: "inbound", "outbound", "internal" |
| `status` | str | No | None | Filter: "suggested", "confirmed", "rejected" |
| `include_body_preview` | bool | No | False | Include email snippet |

**Returns**: List of email summaries with:

- `association_id`, `internet_message_id`, `subject`, `email_date`
- `direction`: INBOUND (from contacts), OUTBOUND (from team), INTERNAL
- `status`: SUGGESTED (auto-matched) or CONFIRMED (user-verified)
- `match_score`: 0-1.0 confidence score
- `matched_stakeholders`: List of emails that matched
- `from_email`, `from_name`, `to_emails`, `cc_emails`
- `has_attachments`, `is_read`, `is_important`, `labels`
- `snippet` (if include_body_preview=True)

---

### `get_email_details`

**Purpose**: Get full details of a specific email by Message-ID.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `internet_message_id` | str | Yes | - | RFC 822 Message-ID |
| `include_body` | bool | No | True | Include plain text body |
| `include_html` | bool | No | False | Include HTML body |
| `max_body_length` | int | No | 5000 | Truncate body to N chars |

**Returns**: Full email details:

- `email_id`, `internet_message_id`, `thread_id`, `subject`
- `from_email`, `from_name`, `to_emails`, `cc_emails`
- `received_at`, `sent_at`, `direction`, `priority`
- `is_read`, `is_starred`, `is_important`, `labels`
- `has_attachments`, `attachments`: List of {name, size, mime_type}
- `body_text`, `body_truncated`, `body_length`
- `body_html` (if include_html=True)
- `deal_associations`: List of linked deals with direction/status/score

---

## Agent Access Control

Tools are assigned to agents via `AGENT_TOOLS` in `registry.py`:

| Agent | Available Tools |
|-------|-----------------|
| **GeneralAgent** | search_internal_knowledge, search_internet, list_deal_stakeholders, list_deal_meetings, get_meeting_details, list_deal_emails, get_email_details |
| **DealAgent** | All CRM tools + all meeting tools + all email tools + search |
| **MeetingAgent** | All meeting tools + list_deal_stakeholders |
| **BriefingAgent** | All tools for comprehensive deal briefings including emails |
| **FollowUpAgent** | Meeting tools for follow-up tracking |

---

## Creating New Tools

### 1. Create the tool file

```python
# tools/category/my_tool.py
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def my_tool(
    param: str,
    *,
    context: AgentContext,
) -> dict[str, Any] | None:
    """Core tool logic."""
    # Use cast(Any, Model).__table__.c for type-safe column access
    c = cast(Any, MyModel).__table__.c
    
    stmt = select(MyModel).where(
        c.organisation_id == context.organisation_id,
        # ... filters
    )
    
    result = await context.session.execute(stmt)
    return result.scalar_one_or_none()


def create_my_tool(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create tool bound to context."""
    
    async def my_tool_impl(param: str) -> dict[str, Any] | None:
        """Tool docstring - shown to the agent."""
        async with db_session_lock:
            return await execute_with_capabilities(
                capabilities_getter(),
                tool_name="MyTool",
                tool_func=my_tool,
                parameters={"param": param, "context": context},
                description=f"Doing something with {param}",
                fallback_value=None,
            )
    
    return my_tool_impl
```

### 2. Export from `__init__.py`

```python
# tools/category/__init__.py
from src.agents.tools.category.my_tool import create_my_tool

__all__ = ["create_my_tool"]
```

### 3. Register in `registry.py`

```python
# Add to ToolName enum
class ToolName(str, Enum):
    MY_TOOL = "my_tool"

# Add to AGENT_TOOLS
AGENT_TOOLS = {
    "MyAgent": {ToolName.MY_TOOL, ...},
}

# Add to TOOL_CATEGORIES
TOOL_CATEGORIES = {
    ToolName.MY_TOOL: ToolCategory.CRM,
}

# Register factory in _register_tool_factories
def _register_tool_factories(self) -> None:
    from src.agents.tools.category import create_my_tool
    
    self._tool_factories[ToolName.MY_TOOL] = (
        lambda: create_my_tool(self.context, self.capabilities_getter)
    )
```

---

## Key Patterns

### Database Access

Always use `cast(Any, Model).__table__.c` for SQLModel column access to satisfy type checker:

```python
c = cast(Any, CRMDealParticipant).__table__.c
stmt = select(CRMDealParticipant).where(c.organisation_id == org_id)
```

### Session Locking

Use `db_session_lock` to prevent concurrent AsyncSession access:

```python
async with db_session_lock:
    result = await execute_with_capabilities(...)
```

### Organisation Isolation

Always filter by `organisation_id`:

```python
stmt = select(Model).where(c.organisation_id == context.organisation_id)
```

### Fallback to Context

Allow `deal_id` to fall back to context:

```python
effective_deal_id = deal_id or context.deal_id
```
