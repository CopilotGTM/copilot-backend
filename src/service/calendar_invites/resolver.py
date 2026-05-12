"""Resolve the current user's Outlook Calendar integration for invite sending.

The resolver is the security gate before any outbound invite write.
It selects a user-owned integration, validates invite scope requirements, and
ensures tokens are available before creating Graph-bound service clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.calendar_invites.models import CalendarInviteError
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    get_missing_scope_labels,
)


@dataclass(frozen=True)
class ResolvedCalendarIntegration:
    """Resolved outbound sender context for a single invite operation."""

    integration: KnowledgeIntegration
    token_info: IntegrationTokenInfo


class OutlookCalendarIntegrationResolver:
    """Find a user-owned Outlook Calendar connection with invite-write scope.

    Resolution is deterministic and constrained by organization and sender context.
    """

    async def resolve(
        self,
        session,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        *,
        sender_email: str | None = None,
    ) -> ResolvedCalendarIntegration:
        """Select a sender-bound integration for one invite send request.

        Selection enforces org/user ownership, scope sufficiency, and available
        token material before returning the token-backed client payload.
        """
        # 1) Restrict to org/user-owned active integrations first.
        # Reauth rows stay in the candidate set so selected unhealthy accounts
        # can return a precise reconnect error instead of looking disconnected.
        stmt = (
            select(KnowledgeIntegration)
            .where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.created_by_user_id) == requested_by_user_id,
                col(KnowledgeIntegration.integration_type) == IntegrationType.OUTLOOK_CALENDAR,
                col(KnowledgeIntegration.is_active) == True,  # noqa: E712
            )
            .order_by(col(KnowledgeIntegration.created_at).desc())
        )
        result = await session.execute(stmt)
        integrations = cast(list[KnowledgeIntegration], result.scalars().all())
        if not integrations:
            raise CalendarInviteError(
                "calendar_integration_required",
                "Connect Outlook Calendar before I can send calendar invites.",
                status_code=409,
            )

        if sender_email:
            # Caller selected a sender account explicitly, so we validate it now
            # instead of picking arbitrarily when multiple accounts exist.
            selected_email = sender_email.strip().lower()
            integration = next(
                (
                    candidate
                    for candidate in integrations
                    if (candidate.integration_email or "").strip().lower() == selected_email
                ),
                None,
            )
            if not integration:
                available_accounts = self._format_available_accounts(integrations)
                raise CalendarInviteError(
                    "calendar_sender_not_found",
                    (
                        f"I could not find an Outlook Calendar connection for {sender_email}. "
                        f"Available sender accounts: {available_accounts}."
                    ),
                    status_code=409,
                )
            return self._validate_selected_integration(integration, organisation_id)

        # 2) If caller did not specify sender, prefer writable integrations.
        # This keeps behavior deterministic for the common one-account flow and
        # only triggers selection prompts when multiple writable accounts exist.
        writable_integrations = [
            integration
            for integration in integrations
            if not get_missing_required_scopes(
                IntegrationType.OUTLOOK_CALENDAR,
                list(integration.scopes or []),
            )
            and not integration.requires_reauth
            and self._has_usable_token(integration)
        ]

        if not writable_integrations:
            # No candidate passed scope/token validation yet.
            # Re-checking the newest integration returns actionable scope errors.
            integration = integrations[0]
            return self._validate_selected_integration(integration, organisation_id)

        if len(writable_integrations) > 1:
            # Ambiguous sender selection must be resolved before write.
            available_accounts = self._format_available_accounts(writable_integrations)
            raise CalendarInviteError(
                "calendar_sender_selection_required",
                (
                    "You have multiple Outlook Calendar accounts that can send invites. "
                    f"Which sender should I use? Available sender accounts: {available_accounts}."
                ),
                status_code=409,
            )

        return self._validate_selected_integration(writable_integrations[0], organisation_id)

    def _validate_selected_integration(
        self,
        integration: KnowledgeIntegration,
        organisation_id: UUID,
    ) -> ResolvedCalendarIntegration:
        # Validate integration before handing back a token-bound session object.
        # Missing permissions here means we can return a precise "reconnect" error
        # instead of hiding failures during Graph writes.
        if integration.requires_reauth:
            raise CalendarInviteError(
                "calendar_reauth_required",
                "Reconnect Outlook Calendar before I can send calendar invites.",
                status_code=409,
            )

        scopes = list(integration.scopes or [])
        missing = get_missing_required_scopes(IntegrationType.OUTLOOK_CALENDAR, scopes)
        if missing:
            labels = ", ".join(get_missing_scope_labels(missing))
            raise CalendarInviteError(
                "calendar_write_scope_required",
                f"Reconnect Outlook Calendar with these permissions: {labels}.",
                status_code=409,
            )

        if not self._has_usable_token(integration):
            # We still verify token material because stale integration rows can
            # remain active but no longer be usable for outbound calls.
            raise CalendarInviteError(
                "calendar_reauth_required",
                "Reconnect Outlook Calendar before I can send calendar invites.",
                status_code=409,
            )

        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        return ResolvedCalendarIntegration(integration=integration, token_info=token_info)

    @staticmethod
    def _has_usable_token(integration: KnowledgeIntegration) -> bool:
        # Support both refresh and access token shapes in credentials; either one
        # allows token resolution to proceed and keeps reconnect loops narrower.
        credentials = dict(integration.credentials or {})
        return bool(credentials.get("refresh_token") or credentials.get("access_token"))

    @staticmethod
    def _format_available_accounts(integrations: list[KnowledgeIntegration]) -> str:
        # Prefer integration_email in prompt and logs because users identify
        # accounts by mailbox, not UUID alone.
        values = [
            integration.integration_email or integration.name or str(integration.integration_id)
            for integration in integrations
        ]
        return ", ".join(values)
