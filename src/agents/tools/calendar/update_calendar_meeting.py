"""Agent tools for organizer-owned calendar meeting updates and cancellation.

The internal event ID comes from model-visible calendar reads, but it is never
trusted by itself. The mutation service joins it to the current user's active
calendar integration and mailbox-specific provider ID before any provider call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.agents.tools.base import execute_with_capabilities
from src.infra.config.configs import security_secrets_config
from src.infra.utils.agent_datetime import parse_agent_datetime
from src.infra.utils.timezone import resolve_timezone_name
from src.service.calendar_invites.attendee_resolver import (
    CalendarAttendeeInput,
    CalendarInviteAttendeeResolver,
)
from src.service.calendar_invites.models import (
    CalendarAttendeeAction,
    CalendarInviteAttendee,
    CalendarInviteError,
    CalendarMeetingUpdateRequest,
    CalendarNotificationScope,
)
from src.service.calendar_invites.mutations import (
    CalendarMeetingMutationService,
    CalendarMutationSnapshot,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

_BOT_CHANNEL_PAYLOAD_KEY = "_bot_channel_payload"


def _meeting_payload(
    target,
    request: CalendarMeetingUpdateRequest,
    *,
    attendee_action: CalendarAttendeeAction | None,
    action_attendees: list[CalendarInviteAttendee],
) -> dict[str, Any]:
    """Return replayable tool inputs for one reviewed meeting update."""

    return {
        "event_id": str(request.event_id),
        "sender_email": target.integration.integration_email,
        "title": request.title,
        "start_time": request.start_at.isoformat(),
        "end_time": request.end_at.isoformat(),
        "timezone": request.timezone,
        "attendees": [
            item.model_dump()
            for item in (action_attendees if attendee_action else request.attendees)
        ],
        "body": request.body,
        "location": request.location,
        "expected_version": request.expected_version,
        "notification_scope": request.notification_scope.value,
        "attendee_action": attendee_action.value if attendee_action else None,
    }


def _confirmation_token(
    payload: dict[str, Any],
    final_attendees: list[CalendarInviteAttendee],
    *,
    secret: str,
    organisation_id: UUID,
    user_id: UUID,
) -> str:
    """Authenticate a tenant-bound review without treating roster order as a change."""

    tool_payload = dict(payload)
    tool_payload["attendees"] = sorted(
        tool_payload.get("attendees") or [],
        key=lambda item: (
            str(item.get("email") or "").lower(),
            bool(item.get("optional")),
            str(item.get("name") or ""),
        ),
    )
    normalized_final_attendees = sorted(
        (item.model_dump() for item in final_attendees),
        key=lambda item: (
            item["email"],
            item["optional"],
            item.get("name") or "",
        ),
    )

    canonical = json.dumps(
        {
            "organisation_id": str(organisation_id),
            "user_id": str(user_id),
            "tool_payload": tool_payload,
            "final_attendees": normalized_final_attendees,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _snapshot_attendees(snapshot: CalendarMutationSnapshot) -> list[CalendarInviteAttendee]:
    """Convert the organizer provider roster into the update request contract."""

    attendees: list[CalendarInviteAttendee] = []
    for attendee in snapshot.normalized.attendees:
        if attendee.is_organiser:
            continue
        payload = attendee.data_payload
        attendees.append(
            CalendarInviteAttendee(
                email=attendee.email,
                name=attendee.display_name,
                optional=bool(
                    payload.get("optional") or str(payload.get("type") or "").lower() == "optional"
                ),
            )
        )
    return attendees


def _attendee_map(
    attendees: list[CalendarInviteAttendee],
) -> dict[str, CalendarInviteAttendee]:
    """Index a provider-normalized roster by its already-normalized email."""

    return {attendee.email: attendee for attendee in attendees}


def _apply_attendee_action(
    snapshot: CalendarMutationSnapshot,
    attendees: list[CalendarInviteAttendee],
    action: CalendarAttendeeAction,
) -> list[CalendarInviteAttendee]:
    """Apply an explicit semantic operation to the live organizer roster."""

    current = _attendee_map(_snapshot_attendees(snapshot))
    requested = _attendee_map(attendees)
    if action == CalendarAttendeeAction.ADD:
        # Existing provider attendees win so "add X" cannot silently rewrite
        # their role or display metadata while adding the new address.
        return list(current.values()) + [
            attendee for email, attendee in requested.items() if email not in current
        ]
    if action == CalendarAttendeeAction.REMOVE:
        return [attendee for email, attendee in current.items() if email not in requested]
    return list(requested.values())


def _changed_fields(changes: dict[str, Any]) -> frozenset[str]:
    """Translate user-visible differences into the sparse provider PATCH mask."""

    field_names = {
        "Title": "title",
        "Time": "time",
        "Location": "location",
        "Description": "body",
    }
    changed = {field_names[item["field"]] for item in changes["scalar"]}
    if changes["attendees_changed"]:
        changed.add("attendees")
    return frozenset(changed)


def _meeting_changes(
    snapshot: CalendarMutationSnapshot,
    request: CalendarMeetingUpdateRequest,
) -> dict[str, Any]:
    """Return only user-visible differences between provider state and desired state."""

    fields = snapshot.normalized.fields
    previous_attendees = _attendee_map(_snapshot_attendees(snapshot))
    updated_attendees = _attendee_map(request.attendees)
    added = [
        updated_attendees[email]
        for email in sorted(updated_attendees.keys() - previous_attendees.keys())
    ]
    removed = [
        previous_attendees[email]
        for email in sorted(previous_attendees.keys() - updated_attendees.keys())
    ]
    role_changes = [
        (previous_attendees[email], updated_attendees[email])
        for email in sorted(previous_attendees.keys() & updated_attendees.keys())
        if previous_attendees[email].optional != updated_attendees[email].optional
    ]
    scalar: list[dict[str, Any]] = []
    comparisons = (
        ("Title", fields.get("title"), request.title),
        (
            "Time",
            (fields.get("start_at"), fields.get("end_at")),
            (request.start_at, request.end_at),
        ),
        ("Location", fields.get("location") or "", request.location or ""),
        ("Description", fields.get("description") or "", request.body or ""),
    )
    for label, previous, updated in comparisons:
        if previous != updated:
            scalar.append({"field": label, "previous": previous, "updated": updated})
    return {
        "scalar": scalar,
        "added": added,
        "removed": removed,
        "role_changes": role_changes,
        "attendees_changed": bool(added or removed or role_changes),
        "material_changed": bool(scalar),
    }


def _notification_scope(
    provider: str,
    requested_scope: str | None,
    changes: dict[str, Any],
) -> CalendarNotificationScope:
    """Select the provider-supported default without adding another user prompt."""

    aliases = {
        "new_attendees_only": CalendarNotificationScope.CHANGED_ATTENDEES_ONLY,
        "changed_attendees_only": CalendarNotificationScope.CHANGED_ATTENDEES_ONLY,
        "all": CalendarNotificationScope.ALL_ATTENDEES,
        "all_attendees": CalendarNotificationScope.ALL_ATTENDEES,
        "none": CalendarNotificationScope.NONE,
        "dont_notify": CalendarNotificationScope.NONE,
        "don't_notify": CalendarNotificationScope.NONE,
    }
    scope = aliases.get((requested_scope or "").strip().lower())
    if requested_scope and scope is None:
        raise CalendarInviteError(
            "calendar_notification_scope_invalid",
            "Use all attendees, changed attendees only, or no notifications.",
            status_code=400,
        )
    if provider == "google":
        if scope == CalendarNotificationScope.CHANGED_ATTENDEES_ONLY:
            raise CalendarInviteError(
                "google_selective_notifications_unsupported",
                (
                    "Google Calendar cannot notify only newly changed attendees through "
                    "the connected API. Should I notify all attendees instead?"
                ),
                status_code=409,
            )
        return scope or CalendarNotificationScope.ALL_ATTENDEES
    if scope == CalendarNotificationScope.NONE:
        raise CalendarInviteError(
            "outlook_notification_suppression_unsupported",
            "Outlook cannot suppress this meeting update. Should I notify the attendees?",
            status_code=409,
        )
    if scope == CalendarNotificationScope.CHANGED_ATTENDEES_ONLY and changes["material_changed"]:
        raise CalendarInviteError(
            "outlook_selective_notifications_unsupported",
            (
                "Outlook must notify all attendees when meeting details change. "
                "Should I notify everyone?"
            ),
            status_code=409,
        )
    if scope:
        return scope
    if changes["attendees_changed"] and not changes["material_changed"]:
        return CalendarNotificationScope.CHANGED_ATTENDEES_ONLY
    return CalendarNotificationScope.ALL_ATTENDEES


def _format_window(start: datetime, end: datetime, timezone_name: str) -> str:
    """Format one meeting window without repeating its date."""

    try:
        timezone = ZoneInfo(timezone_name)
        local_start = start.astimezone(timezone)
        local_end = end.astimezone(timezone)
    except ZoneInfoNotFoundError:
        local_start, local_end = start, end
    return (
        f"{local_start.strftime('%-d %B')}, "
        f"{local_start.strftime('%-I:%M %p')}–{local_end.strftime('%-I:%M %p')} "
        f"{timezone_name}"
    )


def _attendee_line(prefix: str, attendee: CalendarInviteAttendee) -> str:
    """Render one changed attendee with role, name, and email."""

    role = "optional" if attendee.optional else "required"
    identity = (
        f"{attendee.name} — {attendee.email}"
        if attendee.name and attendee.name.lower() != attendee.email
        else attendee.email
    )
    return f"{prefix} {role}: {identity}"


def _notification_label(
    provider: str,
    scope: CalendarNotificationScope,
    changes: dict[str, Any],
) -> str:
    """Return concise canonical copy for the effective notification behavior."""

    if scope == CalendarNotificationScope.NONE:
        return "No update emails"
    if scope == CalendarNotificationScope.ALL_ATTENDEES:
        return "All attendees (Google default)" if provider == "google" else "All attendees"
    if changes["removed"] or changes["role_changes"]:
        return "Changed attendees only (default)"
    return "New attendees only (default)"


def _change_lines(
    changes: dict[str, Any],
    timezone_name: str,
) -> list[str]:
    """Render only the fields that differ from the organizer provider copy."""

    lines: list[str] = []
    for change in changes["scalar"]:
        label = change["field"]
        previous = change["previous"]
        updated = change["updated"]
        if label == "Time":
            previous = _format_window(previous[0], previous[1], timezone_name)
            updated = _format_window(updated[0], updated[1], timezone_name)
        elif label == "Description":
            lines.append("Description updated")
            continue
        lines.extend([label, f"{previous} → {updated}"])
    lines.extend(_attendee_line("+ Add", attendee) for attendee in changes["added"])
    lines.extend(_attendee_line("- Remove", attendee) for attendee in changes["removed"])
    for previous, updated in changes["role_changes"]:
        identity = updated.name or updated.email
        old_role = "optional" if previous.optional else "required"
        new_role = "optional" if updated.optional else "required"
        lines.extend(["Attendee role", f"{identity}: {old_role} → {new_role}"])
    return lines


def _channel_payload(
    context: AgentContext,
    *,
    text: str,
    kind: str,
    title: str,
) -> dict[str, Any]:
    """Use the existing card envelope while keeping one text fallback everywhere."""

    if (context.channel or "").lower() in {"web", "voice"}:
        return {
            "type": "ui_card",
            "content": {
                "card": {
                    "kind": kind,
                    "version": "1.0",
                    "tone": "confirmation" if kind.endswith("review") else "success",
                    "title": title,
                    "message": text,
                    "actions": [],
                }
            },
            "text_fallback": text,
            "attachments": None,
            "source_text": text,
            "force_override": True,
        }
    return {
        "type": "message",
        "content": {"text": text},
        "text_fallback": text,
        "attachments": None,
        "source_text": text,
        "force_override": True,
    }


def _review_payload(
    context: AgentContext,
    snapshot: CalendarMutationSnapshot,
    request: CalendarMeetingUpdateRequest,
    changes: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build the single-CTA canonical update preview."""

    confirm = (
        "*confirm*"
        if (context.channel or "").lower() in {"whatsapp", "slack", "google_chat"}
        else "**confirm**"
    )
    lines = [
        "Meeting update",
        "",
        request.title,
        _format_window(request.start_at, request.end_at, request.timezone),
        "",
        "Changes",
        *_change_lines(changes, request.timezone),
        "",
        "Notifications",
        _notification_label(snapshot.target.provider, request.notification_scope, changes),
        "",
        f"Reply {confirm} to apply.",
    ]
    text = "\n".join(lines)
    return text, _channel_payload(
        context,
        text=text,
        kind="calendar_meeting_update_review",
        title="Meeting update ready for confirmation",
    )


def _receipt_payload(
    context: AgentContext,
    snapshot: CalendarMutationSnapshot,
    request: CalendarMeetingUpdateRequest,
    changes: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build a receipt from the provider-verified mutation result."""

    lines = [
        "Meeting updated",
        "",
        "Changes",
        *_change_lines(changes, request.timezone),
        "",
        "Notifications",
        _notification_label(snapshot.target.provider, request.notification_scope, changes),
    ]
    text = "\n".join(lines)
    return text, _channel_payload(
        context,
        text=text,
        kind="calendar_meeting_update_receipt",
        title="Meeting updated",
    )


async def update_calendar_meeting(
    *,
    context: AgentContext,
    event_id: str,
    title: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    attendees: list[CalendarAttendeeInput] | None = None,
    body: str | None = None,
    location: str | None = None,
    timezone: str | None = None,
    sender_email: str | None = None,
    expected_version: str | None = None,
    notification_scope: str | None = None,
    attendee_action: str | None = None,
    confirmation_token: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Preview or update one organizer meeting inside the immutable agent boundary."""

    if not context.user_id:
        return {"status": "error", "message": "Cannot update meeting: user not identified."}
    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        return {"status": "error", "code": "invalid_event_id", "message": "Invalid event ID."}

    async with context.acquire_session() as session:
        service = CalendarMeetingMutationService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
        )
        try:
            snapshot = await service.resolve_snapshot(parsed_event_id, sender_email=sender_email)
        except CalendarInviteError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}
        target = snapshot.target
        fields = snapshot.normalized.fields

        timezone_name = resolve_timezone_name(
            timezone
            or fields.get("timezone")
            or target.event.timezone
            or (context.metadata or {}).get("user_timezone"),
            default="UTC",
        )
        start_at = fields.get("start_at") or target.event.start_at
        end_at = fields.get("end_at") or target.event.end_at
        if start_time:
            start_at = parse_agent_datetime(
                start_time,
                timezone_name=timezone_name,
                metadata=context.metadata,
                require_time=True,
            )
        if end_time:
            end_at = parse_agent_datetime(
                end_time,
                timezone_name=timezone_name,
                metadata=context.metadata,
                require_time=True,
            )
        if start_at is None or end_at is None:
            return {
                "status": "needs_clarification",
                "code": "calendar_meeting_time_required",
                "message": "I could not understand the new meeting time.",
            }

        action: CalendarAttendeeAction | None = None
        requested_attendees: list[CalendarInviteAttendee] = []
        if attendees is None:
            if attendee_action:
                return {
                    "status": "needs_clarification",
                    "code": "calendar_attendee_action_without_attendees",
                    "message": "Which attendees should I change?",
                }
            desired_attendees = _snapshot_attendees(snapshot)
        else:
            if not attendee_action and not (confirmed and confirmation_token):
                return {
                    "status": "needs_clarification",
                    "code": "calendar_attendee_action_required",
                    "message": (
                        "Should I add these attendees, remove them, or replace the "
                        "current attendee list?"
                    ),
                }
            if attendee_action:
                try:
                    action = CalendarAttendeeAction(attendee_action.strip().lower())
                except ValueError:
                    return {
                        "status": "needs_clarification",
                        "code": "calendar_attendee_action_invalid",
                        "message": (
                            "Should I add these attendees, remove them, or replace the "
                            "current attendee list?"
                        ),
                    }
            if not attendees and confirmed and confirmation_token and not action:
                # An empty final roster is valid in an exact confirmation payload;
                # the normal resolver rejects emptiness only for new attendee intent.
                requested_attendees = []
            else:
                resolution = await CalendarInviteAttendeeResolver(
                    session,
                    organisation_id=context.organisation_id,
                    requested_by_user_id=context.user_id,
                    user_email=context.user_email,
                ).resolve(attendees)
                if resolution.has_blockers:
                    return {
                        "status": "needs_clarification",
                        "code": "calendar_attendees_need_clarification",
                        "message": resolution.clarification_message(),
                        "resolved_attendees": resolution.public_resolved(),
                        "ambiguous_attendees": resolution.ambiguous,
                        "missing_attendees": resolution.missing,
                    }
                requested_attendees = resolution.service_attendees()
            desired_attendees = (
                _apply_attendee_action(snapshot, requested_attendees, action)
                if action
                # A confirmation payload always carries the reviewed final roster.
                # Its fingerprint below makes this replacement safe without an action.
                else requested_attendees
            )

        try:
            provider_body_payload = (
                snapshot.provider_payload.get("body")
                if isinstance(snapshot.provider_payload, dict)
                else None
            )
            provider_body_payload = (
                provider_body_payload if isinstance(provider_body_payload, dict) else {}
            )
            request = CalendarMeetingUpdateRequest(
                organisation_id=context.organisation_id,
                requested_by_user_id=context.user_id,
                event_id=parsed_event_id,
                expected_version=snapshot.provider_etag,
                title=title if title is not None else (fields.get("title") or "Meeting"),
                start_at=start_at,
                end_at=end_at,
                timezone=timezone_name,
                attendees=desired_attendees,
                body=body if body is not None else fields.get("description"),
                location=location if location is not None else fields.get("location"),
                body_changed=body is not None,
                provider_body=provider_body_payload.get("content"),
                provider_body_content_type=provider_body_payload.get("contentType"),
                previous_body=fields.get("description"),
                provider_etag=snapshot.provider_etag,
            )
            changes = _meeting_changes(snapshot, request)
            provider_attendees = snapshot.provider_payload.get("attendees")
            request = request.model_copy(
                update={
                    "notification_scope": _notification_scope(
                        target.provider,
                        notification_scope,
                        changes,
                    ),
                    "changed_fields": _changed_fields(changes),
                    "provider_attendees": (
                        provider_attendees if isinstance(provider_attendees, list) else []
                    ),
                }
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        except CalendarInviteError as exc:
            return {
                "status": "needs_clarification",
                "code": exc.code,
                "message": exc.message,
            }

        if not changes["material_changed"] and not changes["attendees_changed"]:
            if confirmed and expected_version:
                # An uncertain earlier request may have reached the provider even
                # when its response did not reach us. Current provider state is the
                # authoritative reconciliation result, so confirmation can complete.
                return {
                    "status": "updated",
                    "message": "Meeting update verified.",
                    "event_id": event_id,
                    "reconciled": True,
                }
            return {
                "status": "unchanged",
                "message": "The meeting already has these details.",
                "event_id": event_id,
            }

        payload = _meeting_payload(
            target,
            request,
            attendee_action=action,
            action_attendees=requested_attendees,
        )
        confirmation_secret = await security_secrets_config.get_oauth_state_secret()
        preview_token = _confirmation_token(
            payload,
            request.attendees,
            secret=confirmation_secret,
            organisation_id=context.organisation_id,
            user_id=context.user_id,
        )
        payload["confirmation_token"] = preview_token
        review_text, channel_payload = _review_payload(context, snapshot, request, changes)
        confirmation_matches = bool(
            confirmation_token and hmac.compare_digest(confirmation_token, preview_token)
        )
        version_matches = expected_version == snapshot.provider_etag
        if not confirmed or not expected_version or not version_matches or not confirmation_matches:
            if confirmed and expected_version and not version_matches:
                message = (
                    "The meeting changed after the earlier preview. Review this latest update."
                )
            elif confirmed and not confirmation_matches:
                message = "The update details differ from the earlier preview. Review them again."
            else:
                message = "Review this meeting update."
            confirmation_result = {
                "status": "confirmation_required",
                "code": (
                    "calendar_meeting_update_refreshed"
                    if confirmed
                    else "calendar_meeting_update_confirmation_required"
                ),
                "message": message,
                "confirmation_payload": payload,
                "review_text": review_text,
                _BOT_CHANNEL_PAYLOAD_KEY: channel_payload,
            }
            if channel_payload["type"] == "ui_card":
                confirmation_result["ui_card"] = channel_payload["content"]["card"]
            return confirmation_result
        try:
            mutation_result = await service.update_meeting(request, sender_email=sender_email)
        except CalendarInviteError as exc:
            if exc.code == "calendar_meeting_changed":
                try:
                    latest = await service.resolve_snapshot(
                        parsed_event_id,
                        sender_email=sender_email,
                    )
                except CalendarInviteError:
                    latest = None
                if latest is not None:
                    # A provider-side race gets a fresh confirmation instead of
                    # another blind retry against an obsolete organizer version.
                    latest_fields = latest.normalized.fields
                    latest_provider_attendees = latest.provider_payload.get("attendees")
                    rebased_attendees = (
                        _apply_attendee_action(latest, requested_attendees, action)
                        if action
                        else _snapshot_attendees(latest)
                    )
                    refreshed = request.model_copy(
                        update={
                            "title": (
                                request.title
                                if title is not None
                                else latest_fields.get("title") or "Meeting"
                            ),
                            "start_at": (
                                request.start_at
                                if start_time
                                else latest_fields.get("start_at") or request.start_at
                            ),
                            "end_at": (
                                request.end_at
                                if end_time
                                else latest_fields.get("end_at") or request.end_at
                            ),
                            "timezone": (
                                request.timezone
                                if timezone
                                else latest_fields.get("timezone") or request.timezone
                            ),
                            "attendees": rebased_attendees,
                            "body": (
                                request.body
                                if body is not None
                                else latest_fields.get("description")
                            ),
                            "location": (
                                request.location
                                if location is not None
                                else latest_fields.get("location")
                            ),
                            "previous_body": latest_fields.get("description"),
                            "expected_version": latest.provider_etag,
                            "provider_etag": latest.provider_etag,
                            "provider_attendees": (
                                latest_provider_attendees
                                if isinstance(latest_provider_attendees, list)
                                else []
                            ),
                        }
                    )
                    refreshed_changes = _meeting_changes(latest, refreshed)
                    refreshed = refreshed.model_copy(
                        update={
                            "notification_scope": _notification_scope(
                                latest.target.provider,
                                notification_scope,
                                refreshed_changes,
                            ),
                            "changed_fields": _changed_fields(refreshed_changes),
                        }
                    )
                    refreshed_text, refreshed_channel = _review_payload(
                        context,
                        latest,
                        refreshed,
                        refreshed_changes,
                    )
                    refreshed_payload = _meeting_payload(
                        latest.target,
                        refreshed,
                        attendee_action=action,
                        action_attendees=requested_attendees,
                    )
                    refreshed_payload["confirmation_token"] = _confirmation_token(
                        refreshed_payload,
                        refreshed.attendees,
                        secret=confirmation_secret,
                        organisation_id=context.organisation_id,
                        user_id=context.user_id,
                    )
                    refreshed_result = {
                        "status": "confirmation_required",
                        "code": "calendar_meeting_update_refreshed",
                        "message": (
                            "The meeting changed while I was applying the update. "
                            "Review this latest version."
                        ),
                        "confirmation_payload": refreshed_payload,
                        "review_text": refreshed_text,
                        _BOT_CHANNEL_PAYLOAD_KEY: refreshed_channel,
                    }
                    if refreshed_channel["type"] == "ui_card":
                        refreshed_result["ui_card"] = refreshed_channel["content"]["card"]
                    return refreshed_result
            return {"status": "error", "code": exc.code, "message": exc.message}
    receipt_text, receipt_channel = _receipt_payload(context, snapshot, request, changes)
    result = {
        "status": "updated",
        "message": "Meeting updated.",
        "event_id": str(mutation_result.event_id),
        "provider_event_id": mutation_result.provider_event_id,
        "activity_request_id": (
            str(mutation_result.request_id) if mutation_result.request_id else None
        ),
        "notification_scope": request.notification_scope.value,
        "receipt_text": receipt_text,
        _BOT_CHANNEL_PAYLOAD_KEY: receipt_channel,
    }
    if receipt_channel["type"] == "ui_card":
        result["ui_card"] = receipt_channel["content"]["card"]
    return result


async def cancel_calendar_meeting(
    *,
    context: AgentContext,
    event_id: str,
    sender_email: str | None = None,
    cancellation_message: str | None = None,
    expected_version: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Preview or cancel one non-recurring meeting owned by the current user."""

    if not context.user_id:
        return {"status": "error", "message": "Cannot cancel meeting: user not identified."}
    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        return {"status": "error", "code": "invalid_event_id", "message": "Invalid event ID."}
    async with context.acquire_session() as session:
        service = CalendarMeetingMutationService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
        )
        try:
            snapshot = await service.resolve_snapshot(parsed_event_id, sender_email=sender_email)
        except CalendarInviteError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}
        target = snapshot.target
        if target.provider == "google" and cancellation_message:
            return {
                "status": "needs_clarification",
                "code": "google_cancellation_message_unsupported",
                "message": (
                    "Google Calendar cannot attach a custom cancellation message here. "
                    "Confirm cancellation without it, or send the note separately."
                ),
            }
        version = snapshot.provider_etag
        preview = {
            "event_id": event_id,
            "sender_email": target.integration.integration_email,
            "title": target.event.title,
            "start_time": target.event.start_at.isoformat(),
            "cancellation_message": cancellation_message,
            "expected_version": version,
        }
        if not confirmed or not expected_version or expected_version != version:
            return {
                "status": "confirmation_required",
                "code": (
                    "calendar_meeting_cancel_refreshed"
                    if confirmed and expected_version
                    else "calendar_meeting_cancel_confirmation_required"
                ),
                "message": (
                    "The meeting changed after the earlier preview. Review this cancellation."
                    if confirmed and expected_version
                    else "Please confirm cancellation of this exact meeting."
                ),
                "confirmation_payload": preview,
            }
        try:
            result = await service.cancel_meeting(
                parsed_event_id,
                sender_email=sender_email,
                cancellation_message=cancellation_message,
                expected_version=version,
            )
        except CalendarInviteError as exc:
            if exc.code == "calendar_meeting_changed":
                try:
                    latest = await service.resolve_snapshot(
                        parsed_event_id,
                        sender_email=sender_email,
                    )
                except CalendarInviteError:
                    latest = None
                if latest is not None:
                    latest_start = (
                        latest.normalized.fields.get("start_at") or latest.target.event.start_at
                    )
                    return {
                        "status": "confirmation_required",
                        "code": "calendar_meeting_cancel_refreshed",
                        "message": (
                            "The meeting changed while I was cancelling it. "
                            "Review the latest cancellation."
                        ),
                        "confirmation_payload": {
                            **preview,
                            "title": latest.normalized.fields.get("title"),
                            "start_time": latest_start.isoformat(),
                            "expected_version": latest.provider_etag,
                        },
                    }
            return {"status": "error", "code": exc.code, "message": exc.message}
    return {
        "status": "cancelled",
        "message": "Meeting cancelled.",
        "event_id": str(result.event_id),
        "provider_event_id": result.provider_event_id,
        "activity_request_id": str(result.request_id) if result.request_id else None,
    }


def create_update_calendar_meeting(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind organizer-owned meeting updates to immutable agent context."""

    async def update_calendar_meeting_tool(
        event_id: str,
        title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        attendees: list[CalendarAttendeeInput] | None = None,
        body: str | None = None,
        location: str | None = None,
        timezone: str | None = None,
        sender_email: str | None = None,
        expected_version: str | None = None,
        notification_scope: str | None = None,
        attendee_action: str | None = None,
        confirmation_token: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Preview or update an existing meeting organized by the current user.

        For an attendee explicitly described as "from our team", pass a structured
        item with name, scope="team", and its optional flag. Team scope searches
        CRM users only instead of choosing a similarly named external contact. Carry
        that scope only while clarifying the same unresolved attendee; a later edit
        that introduces a new name is unscoped unless the user repeats the team cue.
        Infer attendee_action from meaning, not exact words: use add when the user
        wants more people, remove when they want fewer, and replace only when they
        specify the complete final roster. This applies equally to multilingual and
        code-mixed requests.

        Always show the returned review before writing. On the user's confirmation,
        resend its exact fields, expected_version, notification_scope, attendee_action,
        and confirmation_token with confirmed=True. Do not advertise alternative
        notification actions in the review; if the user separately requests one,
        regenerate the review.
        """

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpdateCalendarMeeting",
            tool_func=update_calendar_meeting,
            parameters={
                "context": context,
                "event_id": event_id,
                "title": title,
                "start_time": start_time,
                "end_time": end_time,
                "attendees": attendees,
                "body": body,
                "location": location,
                "timezone": timezone,
                "sender_email": sender_email,
                "expected_version": expected_version,
                "notification_scope": notification_scope,
                "attendee_action": attendee_action,
                "confirmation_token": confirmation_token,
                "confirmed": confirmed,
            },
            description="Updating calendar meeting",
            fallback_value={"status": "error", "message": "Failed to update meeting."},
        )
        return dict(result)

    return update_calendar_meeting_tool


def create_cancel_calendar_meeting(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind organizer-owned meeting cancellation to immutable agent context."""

    async def cancel_calendar_meeting_tool(
        event_id: str,
        sender_email: str | None = None,
        cancellation_message: str | None = None,
        expected_version: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Preview or cancel an existing meeting organized by the current user."""

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CancelCalendarMeeting",
            tool_func=cancel_calendar_meeting,
            parameters={
                "context": context,
                "event_id": event_id,
                "sender_email": sender_email,
                "cancellation_message": cancellation_message,
                "expected_version": expected_version,
                "confirmed": confirmed,
            },
            description="Cancelling calendar meeting",
            fallback_value={"status": "error", "message": "Failed to cancel meeting."},
        )
        return dict(result)

    return cancel_calendar_meeting_tool
