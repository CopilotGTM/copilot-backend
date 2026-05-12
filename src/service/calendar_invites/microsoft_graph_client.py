"""Microsoft Graph client helpers for calendar-backed Teams invites."""

from __future__ import annotations

from typing import Any, cast

import httpx
from src.service.calendar_invites.models import CalendarInviteCreateRequest, CalendarInviteError
from src.service.calendar_invites.timezones import to_outlook_timezone
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.service.integrations.sync_health import AuthTokenRevokedError
from src.workers.integrations_worker.providers._microsoft_graph import graph_api_post_json


def _format_outlook_datetime(request_dt, outlook_timezone: str) -> str:
    from zoneinfo import ZoneInfo

    if outlook_timezone == "UTC":
        return cast(str, request_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat())

    from tzlocal.windows_tz import win_tz

    iana_name = win_tz[outlook_timezone]
    return cast(str, request_dt.astimezone(ZoneInfo(iana_name)).replace(tzinfo=None).isoformat())


def build_microsoft_event_payload(request: CalendarInviteCreateRequest) -> dict[str, Any]:
    """Build a POST /me/events payload that creates a calendar-backed Teams meeting."""
    if not request.attendees:
        raise ValueError("at least one attendee is required")

    # Build timezone and datetime strings before mapping attendees so Graph sees
    # one canonical format across all integrations and avoids daylight-saving drift.
    outlook_timezone = to_outlook_timezone(request.timezone)
    payload: dict[str, Any] = {
        "subject": request.title,
        "start": {
            "dateTime": _format_outlook_datetime(request.start_at, outlook_timezone),
            "timeZone": outlook_timezone,
        },
        "end": {
            "dateTime": _format_outlook_datetime(request.end_at, outlook_timezone),
            "timeZone": outlook_timezone,
        },
        "attendees": [
            {
                "emailAddress": {
                    "address": attendee.email,
                    "name": attendee.name or attendee.email,
                },
                "type": "optional" if attendee.optional else "required",
            }
            for attendee in request.attendees
        ],
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
    }
    if request.body:
        payload["body"] = {"contentType": "Text", "content": request.body}
    if request.location:
        payload["location"] = {"displayName": request.location}
    if request.idempotency_key:
        payload["transactionId"] = request.idempotency_key
    return payload


class MicrosoftCalendarInviteClient:
    """Create Outlook calendar events that send Teams meeting invites."""

    async def create_event(
        self,
        token_info: IntegrationTokenInfo,
        request: CalendarInviteCreateRequest,
    ) -> dict[str, Any]:
        """Create an Outlook event with Teams metadata using a token-bound session."""
        # Keep payload creation and headers local to the request path so every
        # retry sends the same transaction intent (idempotency key included).
        payload = build_microsoft_event_payload(request)

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Prefer": f'outlook.timezone="{to_outlook_timezone(request.timezone)}"',
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await graph_api_post_json(
                    client,
                    "/v1.0/me/events",
                    json_body=payload,
                )

        try:
            return cast(dict[str, Any], await call_with_token_retry(token_info, _request))
        except IntegrationPermissionError as exc:
            raise CalendarInviteError(
                "calendar_write_scope_required",
                "Reconnect Outlook Calendar with calendar write permission.",
                status_code=409,
            ) from exc
        except AuthTokenRevokedError as exc:
            raise CalendarInviteError(
                "calendar_reauth_required",
                "Reconnect Outlook Calendar before I can send calendar invites.",
                status_code=409,
            ) from exc
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.lower()
            if exc.response.status_code in {400, 422} and "online" in body and "meeting" in body:
                # Provider can return "meeting provider not supported" for
                # tenants that cannot create Teams meetings; translate to a
                # user-facing guidance instead of a generic 400 replay.
                raise CalendarInviteError(
                    "unsupported_online_meeting_provider",
                    "This mailbox cannot create Teams calendar meetings.",
                    status_code=409,
                ) from exc
            raise CalendarInviteError(
                "calendar_provider_error",
                "Outlook Calendar rejected the invite request.",
                status_code=502,
            ) from exc
