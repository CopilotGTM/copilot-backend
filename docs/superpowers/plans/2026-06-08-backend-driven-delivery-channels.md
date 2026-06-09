# Backend-Driven Delivery Channels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backend the source of truth for reminder and scheduled-agent delivery channels so the frontend can render all channel options, labels, icons, availability, and IDs directly from backend responses.

**Architecture:** `OutboundChannelService` owns channel metadata and availability. Reminder and scheduled-agent channel endpoints expose that service contract. The frontend removes channel business-rule hardcoding and renders channel picker options from the backend response.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLAlchemy/SQLModel, pytest, Ruff, Next.js/React frontend, Playwright for UI verification.

---

## Current Problem

The backend channel endpoint already returns Teams, Slack, and WhatsApp for the signed-in user, but the frontend create-agent channel dropdown only shows Teams and Slack because frontend types and picker options are hardcoded.

Production-level direction:

- Backend owns which channels exist.
- Backend owns labels, icons, availability, mapping IDs, validation, delivery context, and send behavior.
- Frontend renders the backend-provided list and only applies UI presentation.
- Frontend must not decide that only Teams and Slack are valid channels.

## Target API Contract

Both endpoints must return the same channel item shape:

```http
GET /users/reminders/channels
GET /users/agents/channels
```

```json
{
  "status": "SUCCESS",
  "message": "Available channels retrieved successfully",
  "data": {
    "channels": [
      {
        "channel": "teams",
        "available": true,
        "label": "Microsoft Teams",
        "icon_url": "/integrations/microsoft-teams.png",
        "mapping_id": "aeee7cc0-ff9a-40a0-8c74-0e51ec3adca6"
      },
      {
        "channel": "slack",
        "available": true,
        "label": "Slack",
        "icon_url": "/integrations/slack.png",
        "mapping_id": "18835954-d1d9-47db-8a75-09677c784180"
      },
      {
        "channel": "whatsapp",
        "available": true,
        "label": "WhatsApp",
        "icon_url": "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
        "mapping_id": "b33a4e3b-7d88-408d-95d6-3bc9b500b648"
      }
    ]
  }
}
```

`icon_url` is additive and must not break existing clients.

## Backend Tasks

### Task 1: Add Channel Metadata To The Channel Service

**Files:**

- Modify: `src/service/channels/outbound_channel_service.py`
- Test: `tests/service/channels/test_outbound_channel_service.py`

- [ ] Add a failing test that asserts all listed channels include the expected icon URL.

```python
@pytest.mark.asyncio
async def test_list_user_channels_includes_display_metadata() -> None:
    channels = await OutboundChannelService().list_user_channels(
        _Session([]),
        uuid4(),
        uuid4(),
    )

    by_channel = {item.channel: item for item in channels}
    assert by_channel["teams"].label == "Microsoft Teams"
    assert by_channel["teams"].icon_url == "/integrations/microsoft-teams.png"
    assert by_channel["slack"].label == "Slack"
    assert by_channel["slack"].icon_url == "/integrations/slack.png"
    assert by_channel["whatsapp"].label == "WhatsApp"
    assert (
        by_channel["whatsapp"].icon_url
        == "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg"
    )
```

- [ ] Run the focused test and verify it fails before implementation.

```bash
uv run pytest tests/service/channels/test_outbound_channel_service.py::test_list_user_channels_includes_display_metadata -q
```

Expected: failure because `OutboundChannelInfo` does not yet expose `icon_url`.

- [ ] Replace `_CHANNEL_LABELS` with a central metadata map and extend `OutboundChannelInfo`.

```python
_CHANNEL_METADATA = {
    "teams": {
        "label": "Microsoft Teams",
        "icon_url": "/integrations/microsoft-teams.png",
    },
    "slack": {
        "label": "Slack",
        "icon_url": "/integrations/slack.png",
    },
    "whatsapp": {
        "label": "WhatsApp",
        "icon_url": "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
    },
}


@dataclass(frozen=True)
class OutboundChannelInfo:
    """Availability summary for one user-linked bot channel."""

    channel: str
    available: bool
    label: str
    icon_url: str
    mapping_id: UUID | None = None
```

- [ ] Update `list_user_channels` to filter on `_CHANNEL_METADATA` and populate `icon_url`.

```python
supported_channels = tuple(channel for channel in channels if channel in _CHANNEL_METADATA)
```

```python
metadata = _CHANNEL_METADATA[channel]
OutboundChannelInfo(
    channel=channel,
    available=False,
    label=metadata["label"],
    icon_url=metadata["icon_url"],
    mapping_id=None,
)
```

```python
provider_map[provider] = OutboundChannelInfo(
    channel=provider,
    available=current.available or context_ready,
    label=current.label,
    icon_url=current.icon_url,
    mapping_id=self._row_value(row, "mapping_id", 1)
    if context_ready and not current.available
    else current.mapping_id,
)
```

- [ ] Run the service tests.

```bash
uv run pytest tests/service/channels/test_outbound_channel_service.py -q
```

Expected: all service channel tests pass.

### Task 2: Expose `icon_url` Through Reminder And Agent Channel APIs

**Files:**

- Modify: `src/api/schemas/reminder_models.py`
- Modify: `src/api/routers/reminders/reminders_handler.py`
- Modify: `src/api/routers/reminders/agents_handler.py`
- Test: `tests/api/routers/reminders/test_reminders_handler.py`
- Test: `tests/api/routers/reminders/test_agents_handler.py`

- [ ] Add failing endpoint tests for `icon_url`.

In `tests/api/routers/reminders/test_reminders_handler.py`, update the fake service rows:

```python
OutboundChannelInfo(
    "teams",
    True,
    "Microsoft Teams",
    "/integrations/microsoft-teams.png",
    uuid4(),
),
OutboundChannelInfo("slack", False, "Slack", "/integrations/slack.png", None),
OutboundChannelInfo(
    "whatsapp",
    True,
    "WhatsApp",
    "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
    uuid4(),
),
```

Then assert:

```python
assert [channel.icon_url for channel in result.data.channels] == [
    "/integrations/microsoft-teams.png",
    "/integrations/slack.png",
    "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
]
```

Repeat the same fake rows and assertion in `tests/api/routers/reminders/test_agents_handler.py`.

- [ ] Run both endpoint tests and verify they fail before implementation.

```bash
uv run pytest tests/api/routers/reminders/test_reminders_handler.py::test_get_available_channels_returns_service_channels tests/api/routers/reminders/test_agents_handler.py::test_get_available_agent_channels_uses_outbound_channel_service -q
```

Expected: failure because API schema/router mapping does not yet include `icon_url`.

- [ ] Add `icon_url` to the response schema.

```python
class DeliveryChannelInfo(BaseModel):
    """Information about a single delivery channel."""

    channel: str = Field(description="Channel identifier: 'teams', 'slack', or 'whatsapp'")
    available: bool = Field(description="Whether the user has this channel set up and ready to use")
    label: str = Field(description="Human-readable label for the channel")
    icon_url: str = Field(description="Icon URL for rendering this channel in clients")
    mapping_id: UUID | None = Field(
        default=None,
        description="Identity mapping ID to send back when creating a reminder with this channel.",
    )
```

- [ ] Map `icon_url` in both channel handlers.

```python
DeliveryChannelInfo(
    channel=item.channel,
    available=item.available,
    label=item.label,
    icon_url=item.icon_url,
    mapping_id=item.mapping_id,
)
```

- [ ] Run the focused endpoint tests.

```bash
uv run pytest tests/api/routers/reminders/test_reminders_handler.py::test_get_available_channels_returns_service_channels tests/api/routers/reminders/test_agents_handler.py::test_get_available_agent_channels_uses_outbound_channel_service -q
```

Expected: both tests pass.

### Task 3: Update Backend API Documentation

**Files:**

- Modify: `docs/API Documentation/reminders-api.md`
- Modify: `docs/API Documentation/agents-api.md`

- [ ] Update the reminders channel response example to include `icon_url`.

```json
{
  "channel": "teams",
  "available": true,
  "label": "Microsoft Teams",
  "icon_url": "/integrations/microsoft-teams.png",
  "mapping_id": "..."
}
```

- [ ] Add a short note under the channel section:

```markdown
Clients should render channel options from this response. Do not hardcode the supported channel list in the frontend; the backend owns channel identity, labels, icons, availability, and mapping IDs.
```

- [ ] Update `agents-api.md` to say its channel response shape is identical to reminders and includes `icon_url`.

### Task 4: Backend Verification

- [ ] Run the focused backend suite.

```bash
uv run pytest tests/service/channels/test_outbound_channel_service.py tests/api/routers/reminders/test_reminders_handler.py tests/api/routers/reminders/test_agents_handler.py -q
```

Expected: all tests pass.

- [ ] Run Ruff on the changed backend files.

```bash
uv run ruff check src/service/channels src/api/routers/reminders src/api/schemas/reminder_models.py tests/service/channels tests/api/routers/reminders
```

Expected: `All checks passed!`

## Frontend Tasks

Do these only after backend tests pass and the backend endpoint returns `icon_url`.

### Task 5: Remove Frontend Channel Business-Rule Hardcoding

**Files:**

- Modify in `D:\CopilotGTM\copilot-app-frontend`:
  - `src/modules/reminders/types/reminders.types.ts`
  - `src/modules/reminders/constants/reminders.constants.ts`
  - `src/components/reminders/reminder-helpers.ts`
  - `src/components/reminders/CreateEditReminderDrawer.tsx`

- [ ] Update frontend channel API type to include backend display metadata.

```ts
export type DeliveryChannel = string;

export interface DeliveryChannelInfo {
  channel: DeliveryChannel;
  available: boolean;
  label: string;
  icon_url: string;
  mapping_id: string | null;
}
```

- [ ] Stop using static `DELIVERY_CHANNEL_OPTIONS` for create/edit dropdowns.

Use:

```ts
const availableChannels = channelsList.filter((channel) => channel.available);
const selectedChannel = channelsList.find(
  (channel) => channel.channel === form.delivery_channel,
);
```

- [ ] Default selected channel to the first available backend channel.

```ts
useEffect(() => {
  if (mode !== "create" || !channelsList.length) return;
  const current = channelsList.find(
    (channel) => channel.channel === form.delivery_channel,
  );
  if (current?.available) return;
  const firstAvailable = channelsList.find((channel) => channel.available);
  if (firstAvailable) {
    setForm((prev) => ({
      ...prev,
      delivery_channel: firstAvailable.channel,
    }));
  }
}, [mode, channelsList, form.delivery_channel]);
```

- [ ] Render dropdown options from `channelsList`.

```tsx
{channelsList.map((channel) => (
  <SelectItem
    key={channel.channel}
    value={channel.channel}
    disabled={!channel.available}
  >
    {channel.label}
    {!channel.available ? " unavailable" : ""}
  </SelectItem>
))}
```

- [ ] Render icon images from `icon_url` where the picker uses visual channel buttons.

```tsx
<img
  src={channel.icon_url}
  alt=""
  aria-hidden="true"
  className="size-3.5 shrink-0"
/>
```

- [ ] Keep a generic icon fallback if `icon_url` is missing or fails to load; this fallback is presentation-only and must not affect channel availability.

### Task 6: Frontend Tests

- [ ] Add or update a frontend test so a mocked channel response with Teams, Slack, and WhatsApp renders all three options.

Expected mocked response:

```ts
{
  data: {
    channels: [
      {
        channel: "teams",
        available: true,
        label: "Microsoft Teams",
        icon_url: "/integrations/microsoft-teams.png",
        mapping_id: "teams-map",
      },
      {
        channel: "slack",
        available: true,
        label: "Slack",
        icon_url: "/integrations/slack.png",
        mapping_id: "slack-map",
      },
      {
        channel: "whatsapp",
        available: true,
        label: "WhatsApp",
        icon_url: "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
        mapping_id: "wa-map",
      },
    ],
  },
}
```

- [ ] Add or update a frontend test proving unavailable channels are visible but disabled.

### Task 7: End-To-End UI Verification

- [ ] Start the backend and frontend dev servers if they are not already running.

Backend:

```bash
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
yarn dev
```

- [ ] Use Playwright to verify `localhost:3000`.

Steps:

1. Open `http://localhost:3000`.
2. Login with the `.env` local test credentials.
3. Navigate to `/agents`.
4. Open `Create agent`.
5. Open the `Delivery channel` dropdown.
6. Confirm the network response for `/api/proxy/agents/channels` includes Teams, Slack, WhatsApp, and `icon_url`.
7. Confirm the dropdown renders Teams, Slack, and WhatsApp.

Expected UI result: WhatsApp appears in the channel picker without adding a hardcoded WhatsApp option in the frontend.

## Acceptance Criteria

- Backend channel endpoints return Teams, Slack, and WhatsApp with `channel`, `available`, `label`, `icon_url`, and `mapping_id`.
- Backend remains the only source of truth for channel availability and create/update validation.
- Frontend renders channel options from backend response.
- Frontend does not hardcode the supported delivery channel list for reminders or agents.
- Local Playwright verification on `localhost:3000` shows WhatsApp in the create-agent delivery dropdown.

## Notes And Boundaries

- Do not add a database migration.
- Do not change provider send behavior in this plan.
- Do not remove unrelated dirty files in either repo.
- Frontend may keep generic icon fallback logic, but not channel eligibility logic.
- The existing backend `OutboundChannelService` remains the single channel service for reminders, agents, tools, bot callbacks, CRM task reminders, and reminder execution.
