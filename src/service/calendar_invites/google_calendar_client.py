"""Google Calendar client helpers for calendar-backed Meet invites."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from src.service.calendar_invites.models import (
    CalendarInviteCreateRequest,
    CalendarInviteError,
    CalendarMeetingUpdateRequest,
)
from src.service.integrations.api_error_utils import (
    IntegrationPermissionError,
    extract_api_error_details,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.service.integrations.sync_health import AuthTokenRevokedError
from src.workers.integrations_worker.providers._google_api import GOOGLE_CALENDAR_BASE_URL


def _format_google_datetime(request_dt: datetime, timezone_name: str) -> str:
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise CalendarInviteError(
            "invalid_calendar_timezone",
            "Use a valid IANA timezone before sending a Google Calendar invite.",
            status_code=400,
        ) from exc
    return request_dt.astimezone(timezone).isoformat()


def _derive_google_conference_request_id(request: CalendarInviteCreateRequest) -> str:
    attendee_part = ",".join(sorted(attendee.email for attendee in request.attendees))
    raw = "|".join(
        [
            str(request.organisation_id),
            str(request.requested_by_user_id),
            request.title,
            request.start_at.isoformat(),
            request.end_at.isoformat(),
            attendee_part,
        ]
    )
    return f"invite-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def build_google_event_payload(request: CalendarInviteCreateRequest) -> dict[str, Any]:
    """Build a Google Events.insert payload that creates a Meet-backed invite."""
    if not request.attendees:
        raise ValueError("at least one attendee is required")

    conference_request_id = request.idempotency_key or _derive_google_conference_request_id(request)
    payload: dict[str, Any] = {
        "summary": request.title,
        "start": {
            "dateTime": _format_google_datetime(request.start_at, request.timezone),
            "timeZone": request.timezone,
        },
        "end": {
            "dateTime": _format_google_datetime(request.end_at, request.timezone),
            "timeZone": request.timezone,
        },
        "attendees": [
            {
                "email": attendee.email,
                **({"displayName": attendee.name} if attendee.name else {}),
                **({"optional": True} if attendee.optional else {}),
            }
            for attendee in request.attendees
        ],
        "conferenceData": {
            "createRequest": {
                "requestId": conference_request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if request.body:
        payload["description"] = request.body
    if request.location:
        payload["location"] = request.location
    return payload


async def google_calendar_api_post_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST JSON to Google Calendar and translate insufficient-scope responses."""
    response = await client.post(
        f"{GOOGLE_CALENDAR_BASE_URL}{endpoint}",
        json=json_body,
        params=params,
    )
    if response.status_code == 403:
        response_text = response.text.lower()
        if (
            "insufficientpermissions" in response_text
            or "insufficient permissions" in response_text
            or "insufficient authentication scopes" in response_text
            or "access_token_scope_insufficient" in response_text
        ):
            raise IntegrationPermissionError(
                "Google Calendar write permission was not granted.",
                status_code=403,
                provider="google",
                endpoint=endpoint,
                response_summary=response.text,
            )
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


class GoogleCalendarInviteClient:
    """Create Google Calendar events that send attendee invites."""

    async def create_event(
        self,
        token_info: IntegrationTokenInfo,
        request: CalendarInviteCreateRequest,
    ) -> dict[str, Any]:
        """Create a Google Calendar event using a token-bound session."""
        payload = build_google_event_payload(request)

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await google_calendar_api_post_json(
                    client,
                    "/calendar/v3/calendars/primary/events",
                    json_body=payload,
                    params={"sendUpdates": "all", "conferenceDataVersion": 1},
                )

        try:
            return cast(dict[str, Any], await call_with_token_retry(token_info, _request))
        except IntegrationPermissionError as exc:
            raise CalendarInviteError(
                "calendar_write_scope_required",
                "Reconnect Google Calendar with calendar invite permission.",
                status_code=409,
            ) from exc
        except AuthTokenRevokedError as exc:
            raise CalendarInviteError(
                "calendar_reauth_required",
                "Reconnect Google Calendar before I can send calendar invites.",
                status_code=409,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                raise CalendarInviteError(
                    "calendar_write_scope_required",
                    "Reconnect Google Calendar with calendar invite permission.",
                    status_code=409,
                ) from exc
            detail = extract_api_error_details(exc).summary
            suffix = f" Provider said: {detail}" if detail else ""
            raise CalendarInviteError(
                "calendar_provider_error",
                f"Google Calendar rejected the invite request.{suffix}",
                status_code=502,
            ) from exc

    async def update_event(
        self,
        token_info: IntegrationTokenInfo,
        provider_event_id: str,
        request: CalendarMeetingUpdateRequest,
    ) -> dict[str, Any]:
        """Patch one Google event and notify its attendee roster."""

        payload = {
            "summary": request.title,
            "description": request.body or "",
            "location": request.location or "",
            "start": {
                "dateTime": _format_google_datetime(request.start_at, request.timezone),
                "timeZone": request.timezone,
            },
            "end": {
                "dateTime": _format_google_datetime(request.end_at, request.timezone),
                "timeZone": request.timezone,
            },
            "attendees": [
                {
                    "email": attendee.email,
                    **({"displayName": attendee.name} if attendee.name else {}),
                    **({"optional": True} if attendee.optional else {}),
                }
                for attendee in request.attendees
            ],
        }

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            if request.provider_etag:
                headers["If-Match"] = request.provider_etag
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                response = await client.patch(
                    f"{GOOGLE_CALENDAR_BASE_URL}/calendar/v3/calendars/primary/events/{provider_event_id}",
                    json=payload,
                    params={"sendUpdates": "all", "conferenceDataVersion": 1},
                )
                response.raise_for_status()
                return cast(dict[str, Any], response.json())

        return await self._mutation_call(token_info, _request, "update")

    async def cancel_event(
        self,
        token_info: IntegrationTokenInfo,
        provider_event_id: str,
        provider_etag: str | None = None,
    ) -> None:
        """Delete one organizer-owned Google event and notify all attendees."""

        async def _request(access_token: str) -> None:
            headers = {"Authorization": f"Bearer {access_token}"}
            if provider_etag:
                headers["If-Match"] = provider_etag
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                response = await client.delete(
                    f"{GOOGLE_CALENDAR_BASE_URL}/calendar/v3/calendars/primary/events/{provider_event_id}",
                    params={"sendUpdates": "all"},
                )
                response.raise_for_status()

        await self._mutation_call(token_info, _request, "cancel")

    @staticmethod
    async def _mutation_call(token_info: IntegrationTokenInfo, operation, label: str):
        """Run a Google mutation with the calendar package's stable error contract."""

        try:
            return await call_with_token_retry(token_info, operation)
        except IntegrationPermissionError as exc:
            raise CalendarInviteError(
                "calendar_write_scope_required",
                "Reconnect Google Calendar with calendar invite permission.",
                status_code=409,
            ) from exc
        except AuthTokenRevokedError as exc:
            raise CalendarInviteError(
                "calendar_reauth_required",
                "Reconnect Google Calendar before changing meetings.",
                status_code=409,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise CalendarInviteError(
                    "calendar_meeting_not_found",
                    "The Google Calendar meeting no longer exists.",
                    status_code=404,
                ) from exc
            if exc.response.status_code == 412:
                raise CalendarInviteError(
                    "calendar_meeting_changed",
                    "The Google Calendar meeting changed after preview.",
                    status_code=409,
                ) from exc
            raise CalendarInviteError(
                "calendar_provider_error",
                f"Google Calendar could not {label} the meeting.",
                status_code=502,
            ) from exc


__all__ = [
    "GoogleCalendarInviteClient",
    "build_google_event_payload",
    "google_calendar_api_post_json",
]
