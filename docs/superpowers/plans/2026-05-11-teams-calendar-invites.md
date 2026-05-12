# Teams Calendar Invites Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-grade Outlook calendar write path so users and AI agents can send calendar-backed Microsoft Teams invites.

**Architecture:** Upgrade the user-owned Outlook Calendar integration to `Calendars.ReadWrite`, add a durable invite request model, implement a service that validates org/user/deal boundaries and calls Microsoft Graph outside DB sessions, then expose the service through a `send_calendar_invite` AI tool. The created Graph event is normalized through existing calendar sync writer code so downstream meeting/timeline/transcript systems reuse the same tables.

**Current API decision (2026-05-12):** The HTTP API endpoint was intentionally deferred. Calendar invite creation is currently driven from the LLM/agent tool path only. The API design remains documented as deprecated/future-use so it can be restored later without rediscovering the contract.

**Sender selection decision (2026-05-12):** When a user owns multiple writable Outlook Calendar
connections, the service must not guess. It returns the available sender email addresses, the agent
asks the user which one to use, and the follow-up call passes `sender_email`. The selected account is
then checked for current-user ownership, required scopes, reauth state, and token availability before
any Microsoft Graph write happens.

**Tech Stack:** FastAPI, SQLModel, PostgreSQL org schemas, Alembic org migrations, httpx, Microsoft Graph, Agno tool registry, existing `IntegrationTokenInfo` / `call_with_token_retry`.

---

## File Map

- Modify: `src/infra/config/microsoft_integration_scopes.py`
  - Replace Outlook Calendar delegated scope `Calendars.Read` with `Calendars.ReadWrite`.
- Modify: `D:/CopilotGTM/copilot-infra/terraform/azure/modules/microsoft_oauth_app/main.tf`
  - Replace the Microsoft OAuth app delegated `Calendars.Read` permission ID with delegated `Calendars.ReadWrite`.
- Modify: `src/service/integrations/scope_validation.py`
  - Add labels and satisfying-scope aliases for `Calendars.ReadWrite`.
- Modify: `tests/service/integrations/test_scope_validation.py`
  - Lock new Outlook Calendar required scope behavior.
- Modify: `tests/api/routers/integrations/test_integration_routes.py`
  - Verify existing read-only Outlook Calendar integrations show additional permission required.
- Create: `src/domain/models/organisation_tables/sync/calendar_invite.py`
  - Persist outbound invite requests for audit, idempotency, and repair.
- Modify: `src/domain/models/organisation_tables/sync/__init__.py`
  - Export `CalendarInviteRequest` and `CalendarInviteRequestStatus`.
- Modify: `src/domain/models/organisation_tables/__init__.py`
  - Export the new model.
- Create: `migrations/org_migrations/versions/3c7f79637798_030_add_calendar_invite_requests.py`
  - Add org-scoped table and constraints.
- Create: `src/service/calendar_invites/models.py`
  - Service request/response dataclasses, typed errors, and timezone conversion contract.
- Create: `src/service/calendar_invites/integration_resolver.py`
  - Resolve current user's writable Outlook Calendar integration and require explicit sender email
    selection when multiple writable accounts exist.
- Create: `src/service/calendar_invites/microsoft_graph_client.py`
  - Create Microsoft Graph event payloads with `transactionId` and call `POST /v1.0/me/events`.
- Create: `src/service/calendar_invites/timezones.py`
  - Convert product/user IANA time zones into Microsoft-supported Outlook time zone names.
- Create: `src/service/calendar_invites/repository.py`
  - Encapsulate invite request persistence and status transitions.
- Create: `src/service/calendar_invites/service.py`
  - Orchestrate validation, persistence, provider call, event sync, and deal linking.
- Create: `src/service/calendar_invites/__init__.py`
  - Export public service types.
- Deferred/deprecated: `src/api/schemas/calendar_invites.py`
  - API request/response schemas if the HTTP endpoint is restored later.
- Deferred/deprecated: `src/api/routers/calendar_invites_handler.py`
  - `POST /users/calendar/invites` if the HTTP endpoint is restored later.
- Deferred/deprecated: router registration file
  - Include the router only if the HTTP endpoint is restored later.
- Create: `src/agents/tools/calendar/__init__.py`
  - Export tool factory.
- Create: `src/agents/tools/calendar/send_calendar_invite.py`
  - Agent tool wrapper around `CalendarInviteService`.
- Modify: `src/agents/tools/registry.py`
  - Add `SEND_CALENDAR_INVITE`, register factory, allow on GeneralAgent and VoiceRealtimeAgent.
- Modify: `src/service/voice_realtime/tool_manifest.py`
  - Add voice manifest schema for `send_calendar_invite`.
- Modify: `src/workers/chat_worker/services/voice_realtime_sideband_service.py`
  - Add the explicit sideband factory mapping for the voice runtime.
- Create: `tests/service/calendar_invites/test_microsoft_graph_client.py`
  - Payload and Graph error tests.
- Create: `tests/service/calendar_invites/test_calendar_invite_service.py`
  - Service orchestration and org/user/deal boundary tests.
- Deferred/deprecated: `tests/api/routers/test_calendar_invites_handler.py`
  - API request/response and error mapping tests if the HTTP endpoint is restored later.
- Create: `tests/agents/tools/calendar/test_send_calendar_invite.py`
  - Agent tool behavior tests.
- Modify: `tests/service/voice_realtime/test_tool_manifest.py`
  - Assert voice manifest exposes the new tool.
- Modify: `docs/API Documentation/integrations-api.md`
  - Keep a deprecated/future-use API contract note; the active path is the AI tool.

## Task 1: Upgrade Microsoft Outlook Calendar Scope Contract

**Files:**
- Modify: `src/infra/config/microsoft_integration_scopes.py`
- Modify: `src/service/integrations/scope_validation.py`
- Modify: `D:/CopilotGTM/copilot-infra/terraform/azure/modules/microsoft_oauth_app/main.tf`
- Modify: `tests/service/integrations/test_scope_validation.py`
- Modify: `tests/api/routers/integrations/test_integration_routes.py`

- [ ] **Step 1: Write failing scope validation tests**

Add tests:

```python
def test_microsoft_outlook_calendar_requires_write_scope_for_invites() -> None:
    assert get_missing_required_scopes(
        IntegrationType.OUTLOOK_CALENDAR,
        [
            "offline_access",
            "User.Read",
            "Calendars.Read",
            "OnlineMeetings.Read",
        ],
    ) == {"Calendars.ReadWrite"}


def test_microsoft_outlook_calendar_accepts_readwrite_scope() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_CALENDAR,
            [
                "offline_access",
                "User.Read",
                "Calendars.ReadWrite",
                "OnlineMeetings.Read",
            ],
        )
        == set()
    )
```

Update the integration route test with an `outlook_calendar` row that has only `Calendars.Read` and assert:

```python
assert connected["missing_scopes"] == ["Calendars.ReadWrite"]
assert connected["missing_scope_labels"] == ["Outlook calendar write access"]
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
uv run pytest tests/service/integrations/test_scope_validation.py tests/api/routers/integrations/test_integration_routes.py -k "outlook_calendar or microsoft" -v
```

Expected: tests fail because `Calendars.ReadWrite` is not required or labelled yet.

- [ ] **Step 3: Implement scope contract**

In `D:/CopilotGTM/copilot-infra/terraform/azure/modules/microsoft_oauth_app/main.tf`, replace the delegated Outlook calendar read scope with the delegated user calendar write scope:

```hcl
# Calendars.ReadWrite - Read and write user calendars (delegated user scope)
resource_access {
  id   = "1ec239c2-d7c9-4623-a91a-a9775856bb36"
  type = "Scope"
}
```

Do not use the application permission ID `ef54d2bf-783f-4e0f-bca1-3210c0444d99`. That is the all-mailbox app permission and requires admin consent; this feature sends invites as the connected user. Some tenants may still require admin approval for user-consent requests as a tenant policy, but that is not the same as needing the application permission.

In `src/infra/config/microsoft_integration_scopes.py`, add:

```python
MICROSOFT_SCOPE_CALENDARS_READ_WRITE = "Calendars.ReadWrite"
```

Update `IntegrationType.OUTLOOK_CALENDAR`:

```python
IntegrationType.OUTLOOK_CALENDAR: (
    MICROSOFT_SCOPE_OFFLINE_ACCESS,
    MICROSOFT_SCOPE_USER_READ,
    MICROSOFT_SCOPE_CALENDARS_READ_WRITE,
    MICROSOFT_SCOPE_ONLINE_MEETINGS_READ,
),
```

In `scope_validation.py`, import the constant and add:

```python
MICROSOFT_SCOPE_CALENDARS_READ_WRITE: frozenset(
    {
        MICROSOFT_SCOPE_CALENDARS_READ_WRITE,
    }
),
```

Add display label:

```python
"Calendars.ReadWrite": "Outlook calendar write access",
```

- [ ] **Step 4: Run tests**

Run the same focused command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C D:/CopilotGTM/copilot-infra add terraform/azure/modules/microsoft_oauth_app/main.tf
git add src/infra/config/microsoft_integration_scopes.py src/service/integrations/scope_validation.py tests/service/integrations/test_scope_validation.py tests/api/routers/integrations/test_integration_routes.py
git commit -m "feat: require outlook calendar write scope"
```

## Task 2: Add Invite Request Persistence

**Files:**
- Create: `src/domain/models/organisation_tables/sync/calendar_invite.py`
- Modify: `src/domain/models/organisation_tables/sync/__init__.py`
- Modify: `src/domain/models/organisation_tables/__init__.py`
- Create: `migrations/org_migrations/versions/3c7f79637798_030_add_calendar_invite_requests.py`

- [ ] **Step 1: Add model tests through migration import smoke**

Add a small model import test if the repo has model import tests. If not, create `tests/domain/models/test_calendar_invite_model.py`:

```python
from src.domain.models.organisation_tables.sync.calendar_invite import (
    CalendarInviteRequest,
    CalendarInviteRequestStatus,
)


def test_calendar_invite_model_table_name() -> None:
    assert CalendarInviteRequest.__tablename__ == "calendar_invite_requests"
    assert CalendarInviteRequestStatus.PENDING.value == "PENDING"
```

- [ ] **Step 2: Create model**

Create `src/domain/models/organisation_tables/sync/calendar_invite.py`:

```python
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.domain.models.organisation_tables.base_mixins import utcnow


class CalendarInviteRequestStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class CalendarInviteRequest(SQLModel, table=True):
    __tablename__ = "calendar_invite_requests"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "requested_by_user_id",
            "idempotency_key",
            name="uq_calendar_invite_request_idempotency",
        ),
        CheckConstraint("end_at > start_at", name="ck_calendar_invite_end_after_start"),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED')",
            name="ck_calendar_invite_status",
        ),
        Index("ix_calendar_invite_org_status", "organisation_id", "status"),
        Index("ix_calendar_invite_requester", "requested_by_user_id"),
        Index("ix_calendar_invite_integration", "integration_id"),
    )

    request_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    requested_by_user_id: UUID = Field(index=True)
    integration_id: UUID = Field(foreign_key="knowledge_integrations.integration_id", index=True)
    deal_id: UUID | None = Field(default=None, foreign_key="crm_deals.deal_id", index=True)
    event_id: UUID | None = Field(default=None, foreign_key="calendar_events.event_id", index=True)
    provider_event_id: str | None = Field(default=None, sa_column=Column(String(255), index=True))
    idempotency_key: str = Field(sa_column=Column(String(128), nullable=False))
    status: CalendarInviteRequestStatus = Field(
        default=CalendarInviteRequestStatus.PENDING,
        sa_column=Column(String(32), nullable=False, server_default=text("'PENDING'")),
    )
    title: str = Field(sa_column=Column(String(500), nullable=False))
    body: str | None = Field(default=None, sa_column=Column(Text))
    location: str | None = Field(default=None, sa_column=Column(String(500)))
    start_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    end_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    timezone: str = Field(sa_column=Column(String(128), nullable=False))
    provider_timezone: str = Field(sa_column=Column(String(128), nullable=False))
    attendees: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    conference_link: str | None = Field(default=None, sa_column=Column(Text))
    provider_response: dict | None = Field(default=None, sa_column=Column(JSONB))
    error_code: str | None = Field(default=None, sa_column=Column(String(128)))
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
```

- [ ] **Step 3: Export model**

Update both `__init__.py` files to import and include:

```python
CalendarInviteRequest,
CalendarInviteRequestStatus,
```

- [ ] **Step 4: Add org migration**

Create an Alembic org migration that creates `calendar_invite_requests` with the same columns and constraints as the model. Use the current org migration style in `migrations/org_migrations/versions`.

- [ ] **Step 5: Run model/migration checks**

Run:

```bash
uv run pytest tests/domain/models/test_calendar_invite_model.py -v
uv run python -m scripts.run_org_migrations --help
```

Expected: model test passes and migration runner imports.

- [ ] **Step 6: Commit**

```bash
git add src/domain/models/organisation_tables/sync/calendar_invite.py src/domain/models/organisation_tables/sync/__init__.py src/domain/models/organisation_tables/__init__.py migrations/org_migrations/versions/*calendar_invite_requests*.py tests/domain/models/test_calendar_invite_model.py
git commit -m "feat: add calendar invite request audit table"
```

## Task 3: Add Calendar Invite Service Models and Resolver

**Files:**
- Create: `src/service/calendar_invites/models.py`
- Create: `src/service/calendar_invites/integration_resolver.py`
- Create: `src/service/calendar_invites/__init__.py`
- Create: `tests/service/calendar_invites/test_integration_resolver.py`

- [ ] **Step 1: Write resolver tests**

Test cases:

```python
async def test_resolver_returns_current_users_writable_outlook_calendar(
    session: AsyncSession,
) -> None:
    integration = make_integration(
        integration_type=IntegrationType.OUTLOOK_CALENDAR,
        created_by_user_id=CURRENT_USER_ID,
        scopes=["offline_access", "User.Read", "Calendars.ReadWrite", "OnlineMeetings.Read"],
    )
    session.add(integration)
    await session.commit()

    resolved = await CalendarIntegrationResolver(session).resolve(
        organisation_id=ORG_ID,
        user_id=CURRENT_USER_ID,
    )

    assert resolved.integration.integration_id == integration.integration_id
    assert resolved.token_info.integration_id == integration.integration_id


async def test_resolver_rejects_other_users_calendar_integration(session: AsyncSession) -> None:
    session.add(
        make_integration(
            integration_type=IntegrationType.OUTLOOK_CALENDAR,
            created_by_user_id=OTHER_USER_ID,
            scopes=["offline_access", "User.Read", "Calendars.ReadWrite", "OnlineMeetings.Read"],
        )
    )
    await session.commit()

    with pytest.raises(CalendarIntegrationRequiredError):
        await CalendarIntegrationResolver(session).resolve(
            organisation_id=ORG_ID,
            user_id=CURRENT_USER_ID,
        )


async def test_resolver_reports_missing_calendars_readwrite(session: AsyncSession) -> None:
    session.add(
        make_integration(
            integration_type=IntegrationType.OUTLOOK_CALENDAR,
            created_by_user_id=CURRENT_USER_ID,
            scopes=["offline_access", "User.Read", "Calendars.Read", "OnlineMeetings.Read"],
        )
    )
    await session.commit()

    with pytest.raises(CalendarWriteScopeRequiredError):
        await CalendarIntegrationResolver(session).resolve(
            organisation_id=ORG_ID,
            user_id=CURRENT_USER_ID,
        )


async def test_resolver_reports_reauth_required(session: AsyncSession) -> None:
    session.add(
        make_integration(
            integration_type=IntegrationType.OUTLOOK_CALENDAR,
            created_by_user_id=CURRENT_USER_ID,
            scopes=["offline_access", "User.Read", "Calendars.ReadWrite", "OnlineMeetings.Read"],
            requires_reauth=True,
        )
    )
    await session.commit()

    with pytest.raises(CalendarReauthRequiredError):
        await CalendarIntegrationResolver(session).resolve(
            organisation_id=ORG_ID,
            user_id=CURRENT_USER_ID,
        )
```

Use the repo's existing fake async session patterns from integration service tests.

- [ ] **Step 2: Add service models**

Create dataclasses:

```python
@dataclass(frozen=True)
class CalendarInviteAttendee:
    email: str
    name: str | None = None
    required: bool = True


@dataclass(frozen=True)
class CalendarInviteCreateRequest:
    organisation_id: UUID
    requested_by_user_id: UUID
    title: str
    start_at: datetime
    end_at: datetime
    timezone: str
    provider_timezone: str
    attendees: list[CalendarInviteAttendee]
    body: str | None = None
    location: str | None = None
    deal_id: UUID | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class CalendarInviteCreateResult:
    request_id: UUID
    event_id: UUID
    provider_event_id: str
    conference_link: str | None
    title: str
    start_at: datetime
    end_at: datetime
    attendees: list[CalendarInviteAttendee]
```

Add typed exceptions:

```python
class CalendarInviteError(Exception):
    """Base error for calendar invite creation failures."""


class CalendarWriteScopeRequiredError(CalendarInviteError):
    """The connected calendar lacks Calendars.ReadWrite."""


class CalendarIntegrationRequiredError(CalendarInviteError):
    """The current user has no usable Outlook Calendar integration."""


class CalendarReauthRequiredError(CalendarInviteError):
    """The connected calendar must be reauthenticated."""


class CalendarInviteValidationError(CalendarInviteError):
    """The invite request is missing required safe-to-send fields."""


class CalendarProviderError(CalendarInviteError):
    """Microsoft Graph rejected the invite request."""


class CalendarProviderDuplicateError(CalendarInviteError):
    """Microsoft Graph reports that this transactionId was already used."""


class CalendarTenantConsentBlockedError(CalendarInviteError):
    """The tenant requires admin approval before the user can grant this delegated scope."""


class UnsupportedOnlineMeetingProviderError(CalendarInviteError):
    """The sender mailbox cannot create Teams online meetings from calendar events."""
```

- [ ] **Step 3: Implement resolver**

Resolver query must include:

```python
select(KnowledgeIntegration).where(
    KnowledgeIntegration.organisation_id == organisation_id,
    KnowledgeIntegration.created_by_user_id == user_id,
    KnowledgeIntegration.integration_type == IntegrationType.OUTLOOK_CALENDAR,
    KnowledgeIntegration.is_active == True,
)
```

Then reject `requires_reauth`, missing refresh/access token, and missing `Calendars.ReadWrite` using `get_missing_required_scopes`.

If more than one writable current-user Outlook Calendar integration exists and no `sender_email` was
provided, return `calendar_sender_selection_required` with the available integration email addresses.
If `sender_email` was provided, match only that `integration_email`; if it is missing or unhealthy,
return the precise failure and do not fall back to another account.

- [ ] **Step 4: Run resolver tests**

```bash
uv run pytest tests/service/calendar_invites/test_integration_resolver.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/calendar_invites tests/service/calendar_invites/test_integration_resolver.py
git commit -m "feat: resolve writable calendar integration"
```

## Task 4: Add Timezone Conversion and Microsoft Graph Invite Client

**Files:**
- Create: `src/service/calendar_invites/timezones.py`
- Create: `src/service/calendar_invites/microsoft_graph_client.py`
- Create: `tests/service/calendar_invites/test_microsoft_graph_client.py`

- [ ] **Step 1: Write timezone and client tests**

Add timezone tests:

```python
def test_to_outlook_timezone_maps_india() -> None:
    assert to_outlook_timezone("Asia/Calcutta") == "India Standard Time"
    assert to_outlook_timezone("Asia/Kolkata") == "India Standard Time"


def test_to_outlook_timezone_rejects_unknown_timezone() -> None:
    with pytest.raises(CalendarInviteValidationError):
        to_outlook_timezone("Mars/Colony")
```

Test payload creation:

```python
def test_build_event_payload_creates_calendar_backed_teams_meeting() -> None:
    payload = MicrosoftGraphCalendarInviteClient.build_event_payload(request)
    assert payload["subject"] == "Acme pricing review"
    assert payload["isOnlineMeeting"] is True
    assert payload["onlineMeetingProvider"] == "teamsForBusiness"
    assert payload["transactionId"] == request.idempotency_key
    assert payload["start"]["timeZone"] == "India Standard Time"
    assert payload["attendees"][0]["emailAddress"]["address"] == "priya@acme.com"
```

Test Graph call uses `POST /v1.0/me/events`, sends `transactionId`, and returns response. Add error mapping tests for Graph tenant consent blocks, duplicate transaction IDs, unsupported online meeting provider, and generic provider errors.

- [ ] **Step 2: Implement timezone conversion**

Create `src/service/calendar_invites/timezones.py` using the `tzlocal` Windows/IANA mapping already present in the repo lockfile:

```python
from zoneinfo import ZoneInfo

from tzlocal.windows_tz import win_tz

from src.service.calendar_invites.models import CalendarInviteValidationError

IANA_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Etc/UTC": "UTC",
}

IANA_TO_WINDOWS = {iana_name: windows_name for windows_name, iana_name in win_tz.items()}


def to_outlook_timezone(timezone: str) -> str:
    normalized = IANA_ALIASES.get(timezone, timezone)
    try:
        ZoneInfo(normalized)
    except Exception as exc:
        raise CalendarInviteValidationError(f"Invalid timezone: {timezone}") from exc
    if normalized == "UTC":
        return "UTC"
    try:
        return IANA_TO_WINDOWS[normalized]
    except KeyError as exc:
        raise CalendarInviteValidationError(
            f"Unsupported calendar timezone for Microsoft Graph: {timezone}"
        ) from exc
```

Add a unit test that asserts every persisted user timezone used in fixtures either maps successfully or is rejected with `CalendarInviteValidationError`.

- [ ] **Step 3: Implement client**

Client shape:

```python
class MicrosoftGraphCalendarInviteClient:
    async def create_event(
        self,
        token_info: IntegrationTokenInfo,
        request: CalendarInviteCreateRequest,
    ) -> dict[str, Any]:
        async def _do_create(token: str) -> dict[str, Any]:
            async with httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {token}",
                    "Prefer": f'outlook.timezone="{request.provider_timezone}"',
                },
                timeout=30.0,
            ) as client:
                return await graph_api_request(
                    client,
                    "/v1.0/me/events",
                    method="POST",
                    json=self.build_event_payload(request),
                )

        return await call_with_token_retry(token_info, _do_create)
```

If `graph_api_request` does not support POST/json, add that support in `src/workers/integrations_worker/providers/_microsoft_graph.py` with regression tests.

In `build_event_payload`, set `transactionId`:

```python
payload["transactionId"] = request.idempotency_key
```

Map Microsoft Graph errors:

- `Authorization_RequestDenied`, `consent_required`, `interaction_required`, or AADSTS admin approval text -> `CalendarTenantConsentBlockedError`
- duplicate `transactionId` response -> `CalendarProviderDuplicateError`
- unsupported `teamsForBusiness` / online meeting provider response -> `UnsupportedOnlineMeetingProviderError`
- all other non-auth Graph failures -> `CalendarProviderError`

- [ ] **Step 4: Run client tests**

```bash
uv run pytest tests/service/calendar_invites/test_microsoft_graph_client.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/calendar_invites/timezones.py src/service/calendar_invites/microsoft_graph_client.py src/workers/integrations_worker/providers/_microsoft_graph.py tests/service/calendar_invites/test_microsoft_graph_client.py
git commit -m "feat: add microsoft graph calendar invite client"
```

## Task 5: Add Repository and Orchestration Service

**Files:**
- Create: `src/service/calendar_invites/repository.py`
- Create: `src/service/calendar_invites/service.py`
- Create: `tests/service/calendar_invites/test_calendar_invite_service.py`

- [ ] **Step 1: Write service tests**

Required tests:

```python
async def test_create_invite_releases_session_before_graph_call(monkeypatch: pytest.MonkeyPatch) -> None:
    graph_called_after_first_commit = False

    async def fake_create_event(token_info, request):
        nonlocal graph_called_after_first_commit
        graph_called_after_first_commit = service.first_session_committed
        return make_graph_event_response()

    service = make_service(graph_create_event=fake_create_event)
    result = await service.create_invite(make_invite_request())

    assert graph_called_after_first_commit is True
    assert result.provider_event_id == "graph-event-1"


async def test_create_invite_rejects_deal_from_other_org() -> None:
    service = make_service()

    with pytest.raises(CalendarInviteValidationError):
        await service.create_invite(make_invite_request(deal_id=OTHER_ORG_DEAL_ID))


async def test_create_invite_upserts_created_event_and_attendees() -> None:
    service = make_service(graph_response=make_graph_event_response(attendee_count=2))

    result = await service.create_invite(make_invite_request())

    event = await load_calendar_event(result.event_id)
    attendees = await load_calendar_attendees(result.event_id)
    assert event.conference_link == "https://teams.microsoft.com/l/meetup-join/test"
    assert {attendee.email for attendee in attendees} == {"priya@acme.com", "sam@acme.com"}


async def test_create_invite_is_idempotent_for_existing_sent_request() -> None:
    service = make_service()
    first = await service.create_invite(make_invite_request(idempotency_key="same-key"))
    second = await service.create_invite(make_invite_request(idempotency_key="same-key"))

    assert second.request_id == first.request_id
    assert service.graph_create_call_count == 1


async def test_create_invite_marks_failed_when_graph_rejects_request() -> None:
    service = make_service(graph_error=CalendarProviderError("Graph rejected request"))

    with pytest.raises(CalendarProviderError):
        await service.create_invite(make_invite_request(idempotency_key="failed-key"))

    request = await load_invite_request_by_key("failed-key")
    assert request.status == CalendarInviteRequestStatus.FAILED
    assert request.error_code == "calendar_provider_error"


async def test_create_invite_marks_unsupported_teams_provider_without_plain_event_fallback() -> None:
    service = make_service(graph_error=UnsupportedOnlineMeetingProviderError("Teams unavailable"))

    with pytest.raises(UnsupportedOnlineMeetingProviderError):
        await service.create_invite(make_invite_request(idempotency_key="unsupported-provider"))

    request = await load_invite_request_by_key("unsupported-provider")
    assert request.status == CalendarInviteRequestStatus.FAILED
    assert request.error_code == "unsupported_online_meeting_provider"


async def test_create_invite_uses_graph_transaction_id_for_duplicate_protection() -> None:
    service = make_service()

    await service.create_invite(make_invite_request(idempotency_key="stable-key"))

    assert service.last_graph_payload["transactionId"] == "stable-key"
```

- [ ] **Step 2: Implement repository**

Methods:

```python
async def create_pending(
    request: CalendarInviteCreateRequest,
    *,
    integration_id: UUID,
    idempotency_key: str,
) -> CalendarInviteRequest
async def mark_sending(request_id: UUID) -> None
async def mark_sent(request_id: UUID, *, provider_event_id: str, event_id: UUID, conference_link: str | None, provider_response: dict[str, Any]) -> None
async def mark_failed(request_id: UUID, *, error_code: str, error_message: str) -> None
async def get_sent_by_idempotency_key(
    organisation_id: UUID,
    requested_by_user_id: UUID,
    idempotency_key: str,
) -> CalendarInviteRequest | None
```

- [ ] **Step 3: Implement service orchestration**

Core flow:

```python
async def create_invite(self, request: CalendarInviteCreateRequest) -> CalendarInviteCreateResult:
    normalized = self._validate_request(request)
    idempotency_key = request.idempotency_key or self._compute_idempotency_key(normalized)
    normalized = replace(
        normalized,
        idempotency_key=idempotency_key,
        provider_timezone=to_outlook_timezone(normalized.timezone),
    )

    async for session in acquire_org_async_session(request.organisation_id):
        resolver = CalendarIntegrationResolver(session)
        resolved = await resolver.resolve(
            organisation_id=request.organisation_id,
            user_id=request.requested_by_user_id,
        )
        integration = resolved.integration
        token_info = resolved.token_info
        repo = CalendarInviteRequestRepository(session)
        existing = await repo.get_sent_by_idempotency_key(
            request.organisation_id,
            request.requested_by_user_id,
            idempotency_key,
        )
        if existing:
            return self._result_from_existing(existing)
        invite_request = await repo.create_pending(
            normalized,
            integration_id=integration.integration_id,
            idempotency_key=idempotency_key,
        )
        await repo.mark_sending(invite_request.request_id)
        await session.commit()

    provider_event = await self.graph_client.create_event(token_info, normalized)

    async for session in acquire_org_async_session(request.organisation_id):
        repo = CalendarInviteRequestRepository(session)
        event = await self._sync_provider_event(session, integration, provider_event)
        await self._link_deal_if_requested(session, event.event_id, request.deal_id)
        await repo.mark_sent(
            invite_request.request_id,
            provider_event_id=str(provider_event["id"]),
            event_id=event.event_id,
            conference_link=event.conference_link,
            provider_response=provider_event,
        )
        await session.commit()
        return CalendarInviteCreateResult(
            request_id=invite_request.request_id,
            event_id=event.event_id,
            provider_event_id=str(provider_event["id"]),
            conference_link=event.conference_link,
            title=event.title or normalized.title,
            start_at=event.start_at,
            end_at=event.end_at,
            attendees=normalized.attendees,
        )
```

If Graph fails, reacquire a fresh session and mark request `FAILED`.

- [ ] **Step 4: Run service tests**

```bash
uv run pytest tests/service/calendar_invites/test_calendar_invite_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/calendar_invites/repository.py src/service/calendar_invites/service.py tests/service/calendar_invites/test_calendar_invite_service.py
git commit -m "feat: orchestrate calendar invite creation"
```

## Task 6: Deferred / Deprecated API Endpoint

**Current status:** Do not implement or register this endpoint right now. Calendar invite creation
is currently HLM/LLM-driven through the agent tool path. Keep the design below as a restoration
recipe for future product work.

**Files:**
- Create: `src/api/schemas/calendar_invites.py`
- Create: `src/api/routers/calendar_invites_handler.py`
- Modify: router registration file
- Create: `tests/api/routers/test_calendar_invites_handler.py`

- [ ] **Step 1: Write API tests**

Test:

```python
def test_create_calendar_invite_returns_sent_response(client: TestClient) -> None:
    response = client.post("/users/calendar/invites", json=valid_invite_payload())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "SENT"
    assert data["conference_link"].startswith("https://teams.microsoft.com/")


def test_create_calendar_invite_maps_missing_scope_to_400(client: TestClient) -> None:
    override_calendar_invite_service_error(CalendarWriteScopeRequiredError("missing scope"))

    response = client.post("/users/calendar/invites", json=valid_invite_payload())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "calendar_write_scope_required"


def test_create_calendar_invite_requires_attendee_email(client: TestClient) -> None:
    payload = valid_invite_payload()
    payload["attendees"] = [{"name": "Priya"}]

    response = client.post("/users/calendar/invites", json=payload)

    assert response.status_code == 422
```

- [ ] **Step 2: Add schemas**

Create Pydantic models with:

- `CalendarInviteAttendeeRequest`
- `CreateCalendarInviteRequest`
- `CreateCalendarInviteResponse`

Use Pydantic email validation if already available in dependencies; otherwise validate with the repo's existing email helper if one exists.

- [ ] **Step 3: Add router**

Router:

```python
router = APIRouter(prefix="/users/calendar", tags=["calendar"])


@router.post("/invites", response_model=ApiResponse[CreateCalendarInviteResponse])
async def create_calendar_invite(
    payload: CreateCalendarInviteRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse[CreateCalendarInviteResponse]:
    service = CalendarInviteService()
    result = await service.create_invite(payload.to_service_request(current_user))
    return success_response(CreateCalendarInviteResponse.from_result(result))
```

Follow the exact response wrapper/dependency names used by adjacent routers.

- [ ] **Step 4: Run API tests**

```bash
uv run pytest tests/api/routers/test_calendar_invites_handler.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/schemas/calendar_invites.py src/api/routers/calendar_invites_handler.py tests/api/routers/test_calendar_invites_handler.py
git commit -m "feat: expose calendar invite API"
```

## Task 7: Add Agent Tool and Registry Wiring

**Files:**
- Create: `src/agents/tools/calendar/__init__.py`
- Create: `src/agents/tools/calendar/send_calendar_invite.py`
- Modify: `src/agents/tools/registry.py`
- Create: `tests/agents/tools/calendar/test_send_calendar_invite.py`
- Modify: `tests/service/agents/test_channel_tool_registry.py` or adjacent registry tests

- [ ] **Step 1: Write tool tests**

Test:

```python
async def test_send_calendar_invite_tool_calls_service_with_context_user_and_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: CalendarInviteCreateRequest | None = None

    async def fake_create_invite(self, request: CalendarInviteCreateRequest):
        nonlocal captured
        captured = request
        return make_invite_result()

    monkeypatch.setattr(CalendarInviteService, "create_invite", fake_create_invite)
    tool = create_send_calendar_invite(context, lambda: None)

    result = await tool(
        title="Acme pricing review",
        start_at="2026-05-12T15:00:00+05:30",
        end_at="2026-05-12T15:30:00+05:30",
        timezone="Asia/Calcutta",
        attendee_emails=["priya@acme.com"],
    )

    assert result["status"] == "sent"
    assert captured is not None
    assert captured.organisation_id == context.organisation_id
    assert captured.requested_by_user_id == context.user_id


async def test_send_calendar_invite_tool_requires_attendee_emails() -> None:
    tool = create_send_calendar_invite(context, lambda: None)

    result = await tool(
        title="Acme pricing review",
        start_at="2026-05-12T15:00:00+05:30",
        end_at="2026-05-12T15:30:00+05:30",
        attendee_emails=[],
    )

    assert result["status"] == "needs_clarification"
    assert "email" in result["message"].lower()


def test_general_agent_includes_send_calendar_invite() -> None:
    assert ToolName.SEND_CALENDAR_INVITE in AGENT_TOOLS["GeneralAgent"]
```

- [ ] **Step 2: Implement tool**

Tool signature:

```python
async def send_calendar_invite_tool(
    title: str,
    start_at: str,
    end_at: str,
    timezone: str | None = None,
    sender_email: str | None = None,
    attendee_emails: list[str] | None = None,
    attendee_names: list[str] | None = None,
    body: str | None = None,
    location: str | None = None,
    deal_name: str | None = None,
    deal_id: str | None = None,
) -> dict[str, Any]:
```

Tool docstring must state:

- call immediately when the user asks to send/schedule a calendar invite and title, time, and attendee emails are known;
- ask a follow-up when attendee emails or a concrete time are missing;
- ask a follow-up when multiple writable Outlook Calendar integrations exist, listing the sender
  email addresses returned by the service;
- this sends a real invite from the connected Outlook calendar;
- do not use it for reminders or tasks.

Use `execute_with_capabilities`:

```python
result = await execute_with_capabilities(
    capabilities_getter(),
    tool_name="SendCalendarInvite",
    tool_func=send_calendar_invite,
            parameters={
                "context": context,
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "timezone": timezone,
                "sender_email": sender_email,
                "attendee_emails": attendee_emails,
                "attendee_names": attendee_names,
                "body": body,
                "location": location,
                "deal_name": deal_name,
                "deal_id": deal_id,
            },
    description="Sending calendar invite",
    fallback_value={"status": "error", "message": "Failed to send calendar invite."},
)
```

- [ ] **Step 3: Wire registry**

Add `ToolName.SEND_CALENDAR_INVITE = "send_calendar_invite"`.

Add to:

- `GENERAL_AGENT_TOOLS`
- `AGENT_TOOLS["VoiceRealtimeAgent"]` via general set
- `CHANNEL_ONLY_TOOLS` for `{"teams", "slack", "whatsapp", "web", "voice"}`
- `TOOL_CATEGORIES` as `ToolCategory.CALENDAR`
- `_register_tool_factories`

- [ ] **Step 4: Run tool tests**

```bash
uv run pytest tests/agents/tools/calendar/test_send_calendar_invite.py tests/service/agents/test_channel_tool_registry.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agents/tools/calendar src/agents/tools/registry.py tests/agents/tools/calendar/test_send_calendar_invite.py tests/service/agents/test_channel_tool_registry.py
git commit -m "feat: add send calendar invite agent tool"
```

## Task 8: Add Voice Runtime Manifest Wiring

**Files:**
- Modify: `src/service/voice_realtime/tool_manifest.py`
- Modify: `src/workers/chat_worker/services/voice_realtime_sideband_service.py`
- Modify: `tests/service/voice_realtime/test_tool_manifest.py`

- [ ] **Step 1: Write voice manifest test**

Add:

```python
def test_voice_manifest_includes_send_calendar_invite() -> None:
    schemas = {schema["name"]: schema for schema in build_voice_tool_schemas()}
    assert "send_calendar_invite" in schemas
    schema = schemas["send_calendar_invite"]
    assert "title" in schema["parameters"]["properties"]
    assert "attendee_emails" in schema["parameters"]["properties"]
```

- [ ] **Step 2: Add manifest definition**

Add factory builder:

```python
def _build_send_calendar_invite_factory(context: AgentContext) -> Callable[[], Callable[..., Any]]:
    from src.agents.tools.calendar.send_calendar_invite import create_send_calendar_invite

    return partial(create_send_calendar_invite, context, _build_empty_capabilities)
```

Add `VoiceRealtimeToolDefinition` with required fields:

```python
"required": ["title", "start_at", "end_at", "attendee_emails"]
```

- [ ] **Step 3: Wire sideband explicit map**

In the explicit tool map in `voice_realtime_sideband_service.py`, add:

```python
ToolName.SEND_CALENDAR_INVITE: partial(create_send_calendar_invite, context, empty_capabilities),
```

- [ ] **Step 4: Run voice tests**

```bash
uv run pytest tests/service/voice_realtime/test_tool_manifest.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/voice_realtime/tool_manifest.py src/workers/chat_worker/services/voice_realtime_sideband_service.py tests/service/voice_realtime/test_tool_manifest.py
git commit -m "feat: expose calendar invites to voice runtime"
```

## Task 9: Add Deprecated / Future-Use API Documentation

**Files:**
- Modify: `docs/API Documentation/integrations-api.md`

- [ ] **Step 1: Document deferred endpoint contract**

Include:

- endpoint path
- status as deprecated/not registered
- note that active creation happens through the LLM/agent tool path
- auth requirement
- required Outlook Calendar reconnect for `Calendars.ReadWrite`
- request/response examples
- error codes
- note that the Teams bot app permissions are not used for this feature

- [ ] **Step 2: Commit**

```bash
git add "docs/API Documentation/integrations-api.md"
git commit -m "docs: record deferred calendar invite API contract"
```

## Task 10: Final Verification

**Files:** all changed files

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest tests/service/integrations/test_scope_validation.py tests/api/routers/integrations/test_integration_routes.py tests/service/calendar_invites tests/api/routers/test_calendar_invites_handler.py tests/agents/tools/calendar tests/service/voice_realtime/test_tool_manifest.py -v
```

Expected: PASS.

- [ ] **Step 2: Run lint and type checks before PR**

Per repo guidance, run only at the end:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

Expected: PASS or only known unrelated baseline failures. Investigate any failures in changed files.

- [ ] **Step 3: Manual OAuth check**

Reconnect Outlook Calendar locally/dev and verify the authorization URL includes:

```text
offline_access User.Read Calendars.ReadWrite OnlineMeetings.Read
```

- [ ] **Step 4: Manual Graph behavior check**

With a writable test Microsoft account:

1. Send invite through API.
2. Confirm event appears in sender Outlook calendar.
3. Confirm invitee receives calendar invite.
4. Confirm response contains Teams join URL.
5. Confirm existing calendar sync can see or reconcile the event.

- [ ] **Step 5: Final commit**

```bash
git status --short
git commit --allow-empty -m "chore: verify teams calendar invites"
```

Use the empty verification commit only if the project convention accepts it; otherwise skip and include verification in the PR description.

## Self-Review Checklist

- [ ] Microsoft scope change touches only the user-owned Outlook Calendar integration.
- [ ] Teams bot app permissions remain unchanged.
- [ ] No DB session is held during Graph calls.
- [ ] Every DB query includes `organisation_id`, and integration lookup includes `created_by_user_id`.
- [ ] Agent tool cannot accept or override `organisation_id`, `integration_id`, or `requested_by_user_id`.
- [ ] Duplicate agent retries are idempotent.
- [ ] Created events are written to existing calendar tables, not a parallel meeting table.
- [ ] Existing read-only Outlook Calendar users get `additional_permissions` until reconnect.
- [ ] API docs explicitly call out `Calendars.ReadWrite`.

Plan complete and saved to `docs/superpowers/plans/2026-05-11-teams-calendar-invites.md`. Two execution options:

1. Subagent-Driven (recommended) - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. Inline Execution - execute tasks in this session using executing-plans, batch execution with checkpoints.
