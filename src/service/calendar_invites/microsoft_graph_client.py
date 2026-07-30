"""Microsoft Graph client helpers for calendar-backed Teams invites."""

from __future__ import annotations

from html import escape
from typing import Any, cast

import httpx
from src.service.calendar_invites.models import (
    CalendarInviteCreateRequest,
    CalendarInviteError,
    CalendarMeetingUpdateRequest,
)
from src.service.calendar_invites.timezones import to_outlook_timezone
from src.service.integrations.api_error_utils import (
    IntegrationPermissionError,
    extract_api_error_details,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.service.integrations.sync_health import AuthTokenRevokedError
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_patch_json,
    graph_api_post_json,
    graph_api_post_no_content,
    graph_api_request,
)


def _format_outlook_datetime(request_dt, outlook_timezone: str) -> str:
    from zoneinfo import ZoneInfo

    if outlook_timezone == "UTC":
        return cast(str, request_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat())

    from tzlocal.windows_tz import win_tz

    iana_name = win_tz[outlook_timezone]
    return cast(str, request_dt.astimezone(ZoneInfo(iana_name)).replace(tzinfo=None).isoformat())


def _microsoft_update_attendees(
    request: CalendarMeetingUpdateRequest,
) -> list[dict[str, Any]]:
    """Preserve Graph attendee roles and response state for unchanged addresses."""

    current: dict[str, dict[str, Any]] = {}
    for item in request.provider_attendees:
        email_address = item.get("emailAddress")
        if not isinstance(email_address, dict):
            continue
        email = str(email_address.get("address") or "").strip().lower()
        if email:
            current[email] = item

    attendees: list[dict[str, Any]] = []
    for attendee in request.attendees:
        raw = current.get(attendee.email, {})
        raw_address = raw.get("emailAddress")
        raw_address = raw_address if isinstance(raw_address, dict) else {}
        merged: dict[str, Any] = {
            "emailAddress": {
                "address": attendee.email,
                "name": raw_address.get("name") or attendee.name or attendee.email,
            },
            **({"status": raw["status"]} if isinstance(raw.get("status"), dict) else {}),
        }
        current_type = str(raw.get("type") or "").lower()
        if attendee.optional:
            merged["type"] = "optional"
        elif current_type == "optional":
            merged["type"] = "required"
        else:
            # Resource attendees must stay resources; the canonical attendee model
            # otherwise collapses both resource and required into optional=False.
            merged["type"] = current_type or "required"
        attendees.append(merged)
    return attendees


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


def _outlook_update_body(request: CalendarMeetingUpdateRequest) -> dict[str, str] | None:
    """Replace organizer notes while retaining Graph's Teams join block."""

    if not request.body_changed:
        return None
    updated_notes = request.body or ""
    current = request.provider_body or ""
    if not current:
        return {"contentType": "Text", "content": updated_notes}

    previous = request.previous_body or ""
    is_html = (request.provider_body_content_type or "").lower() == "html"
    replacement = escape(updated_notes) if is_html else updated_notes
    candidates = [previous, escape(previous)] if previous else []
    for candidate in candidates:
        if candidate and candidate in current:
            return {
                "contentType": request.provider_body_content_type or "HTML",
                "content": current.replace(candidate, replacement, 1),
            }

    # Graph stores the online-meeting join controls inside the body. If the
    # previous preview was truncated or HTML-normalized, prepend the new notes
    # instead of replacing the body and accidentally deleting the join block.
    separator = "<br><br>" if is_html else "\n\n"
    return {
        "contentType": request.provider_body_content_type or "HTML",
        "content": f"{replacement}{separator}{current}",
    }


class MicrosoftCalendarInviteClient:
    """Create Outlook calendar events that send Teams meeting invites."""

    async def get_event(
        self,
        token_info: IntegrationTokenInfo,
        provider_event_id: str,
    ) -> dict[str, Any]:
        """Fetch the organizer mailbox's current event copy and change key."""

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Prefer": 'outlook.timezone="UTC"',
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await graph_api_request(
                    client,
                    f"/v1.0/me/events/{provider_event_id}",
                )

        return cast(dict[str, Any], await self._mutation_call(token_info, _request, "read"))

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
                # Persisted event responses should be UTC even though the request
                # payload uses the user's wall clock. The normalizer still handles
                # local responses defensively for delta and legacy callers.
                "Prefer": 'outlook.timezone="UTC"',
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
                raise CalendarInviteError(
                    "unsupported_online_meeting_provider",
                    "This mailbox cannot create Teams calendar meetings.",
                    status_code=409,
                ) from exc
            detail = extract_api_error_details(exc).summary
            suffix = f" Provider said: {detail}" if detail else ""
            raise CalendarInviteError(
                "calendar_provider_error",
                f"Outlook Calendar rejected the invite request.{suffix}",
                status_code=502,
            ) from exc

    async def update_event(
        self,
        token_info: IntegrationTokenInfo,
        provider_event_id: str,
        request: CalendarMeetingUpdateRequest,
    ) -> dict[str, Any]:
        """Patch one Outlook event while retaining its online-meeting identity."""

        timezone = to_outlook_timezone(request.timezone)
        payload: dict[str, Any] = {}
        if request.notification_scope == "none":
            raise CalendarInviteError(
                "outlook_notification_suppression_unsupported",
                "Outlook cannot suppress meeting-update notifications.",
                status_code=409,
            )
        if "attendees" in request.changed_fields:
            payload["attendees"] = _microsoft_update_attendees(request)
        if "title" in request.changed_fields:
            payload["subject"] = request.title
        if "location" in request.changed_fields:
            payload["location"] = {"displayName": request.location or ""}
        if "time" in request.changed_fields:
            payload.update(
                {
                    "start": {
                        "dateTime": _format_outlook_datetime(request.start_at, timezone),
                        "timeZone": timezone,
                    },
                    "end": {
                        "dateTime": _format_outlook_datetime(request.end_at, timezone),
                        "timeZone": timezone,
                    },
                }
            )
        if request.notification_scope == "all_attendees" and request.changed_fields == {
            "attendees"
        }:
            # Graph sends selectively only when attendees is the sole PATCH field.
            # Repeating the unchanged subject triggers the requested all-attendee update.
            payload["subject"] = request.title
        updated_body = _outlook_update_body(request)
        if updated_body is not None and "body" in request.changed_fields:
            payload["body"] = updated_body

        async def _request(access_token: str) -> dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Prefer": f'outlook.timezone="{timezone}"',
            }
            if request.provider_etag:
                headers["If-Match"] = request.provider_etag
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                return await graph_api_patch_json(
                    client,
                    f"/v1.0/me/events/{provider_event_id}",
                    json_body=payload,
                )

        return cast(dict[str, Any], await self._mutation_call(token_info, _request, "update"))

    async def cancel_event(
        self,
        token_info: IntegrationTokenInfo,
        provider_event_id: str,
        comment: str | None,
        provider_etag: str | None = None,
    ) -> None:
        """Cancel one Outlook meeting with an optional attendee-facing comment."""

        async def _request(access_token: str) -> None:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            if provider_etag:
                headers["If-Match"] = provider_etag
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                await graph_api_post_no_content(
                    client,
                    f"/v1.0/me/events/{provider_event_id}/cancel",
                    json_body={"comment": comment or ""},
                )

        await self._mutation_call(token_info, _request, "cancel")

    @staticmethod
    async def _mutation_call(token_info: IntegrationTokenInfo, operation, label: str):
        """Run a Graph mutation with stable organizer-facing failure messages."""

        try:
            return await call_with_token_retry(token_info, operation)
        except IntegrationPermissionError as exc:
            raise CalendarInviteError(
                "calendar_write_scope_required",
                "Reconnect Outlook Calendar with calendar write permission.",
                status_code=409,
            ) from exc
        except AuthTokenRevokedError as exc:
            raise CalendarInviteError(
                "calendar_reauth_required",
                "Reconnect Outlook Calendar before changing meetings.",
                status_code=409,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise CalendarInviteError(
                    "calendar_meeting_not_found",
                    "The Outlook meeting no longer exists.",
                    status_code=404,
                ) from exc
            if exc.response.status_code == 412:
                raise CalendarInviteError(
                    "calendar_meeting_changed",
                    "The Outlook meeting changed after preview.",
                    status_code=409,
                ) from exc
            raise CalendarInviteError(
                "calendar_provider_error",
                f"Outlook Calendar could not {label} the meeting.",
                status_code=502,
            ) from exc
