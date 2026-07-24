"""Resolve the current user's calendar integration for invite sending.

The resolver is the security gate before any outbound invite write.
It selects a user-owned integration, validates invite scope requirements, and
ensures tokens are available before creating provider-bound service clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationActivityRequest,
    IntegrationActivityRequestStatus,
    IntegrationType,
    KnowledgeIntegration,
)
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
    provider: str
    connected_account_count: int = 1
    selection_reason: str = "only_writable_calendar"
    warning: str | None = None


class CalendarIntegrationResolver:
    """Find a user-owned calendar connection with invite-write scope.

    Resolution is deterministic and constrained by organization and sender context.
    """

    _SUPPORTED_TYPES = (IntegrationType.MICROSOFT, IntegrationType.GOOGLE)

    async def resolve(
        self,
        session,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        *,
        sender_email: str | None = None,
        preferred_sender_email: str | None = None,
        user_email: str | None = None,
        official_domains: set[str] | None = None,
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
                col(KnowledgeIntegration.integration_type).in_(self._SUPPORTED_TYPES),
                or_(
                    col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                    col(KnowledgeIntegration.requires_reauth) == True,  # noqa: E712
                ),
            )
            .order_by(col(KnowledgeIntegration.created_at).desc())
        )
        result = await session.execute(stmt)
        integrations = cast(list[KnowledgeIntegration], result.scalars().all())
        if not integrations:
            raise CalendarInviteError(
                "calendar_integration_required",
                "Connect Outlook or Google Calendar before I can send calendar invites.",
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
                        f"I could not find a Calendar connection for {sender_email}. "
                        f"Available sender accounts: {available_accounts}."
                    ),
                    status_code=409,
                )
            return self._validate_selected_integration(
                integration,
                organisation_id,
                connected_account_count=len(integrations),
                selection_reason="explicit_sender",
            )

        # 2) If caller did not specify sender, prefer writable integrations.
        # This keeps behavior deterministic for the common one-account flow and
        # only triggers selection prompts when multiple writable accounts exist.
        writable_integrations = [
            integration
            for integration in integrations
            if not get_missing_required_scopes(
                self._calendar_capability_type(integration.integration_type),
                list(integration.scopes or []),
            )
            and not integration.requires_reauth
            and self._has_usable_token(integration)
        ]

        if not writable_integrations:
            # No candidate passed scope/token validation yet.
            # Re-checking the newest integration returns actionable scope errors.
            integration = integrations[0]
            return self._validate_selected_integration(
                integration,
                organisation_id,
                connected_account_count=len(integrations),
                selection_reason="only_connected_calendar",
            )

        if len(writable_integrations) == 1:
            return self._validate_selected_integration(
                writable_integrations[0],
                organisation_id,
                connected_account_count=len(integrations),
                selection_reason="only_writable_calendar",
            )

        # Multiple calendars should not turn the common invite flow into a
        # clarification. Choose from explicit user state first, then progressively
        # weaker but deterministic signals, and expose the choice in the preview.
        normalized_preference = self._normalize_email(preferred_sender_email)
        preferred = self._find_by_email(writable_integrations, normalized_preference)
        if preferred:
            return self._validate_selected_integration(
                preferred,
                organisation_id,
                connected_account_count=len(integrations),
                selection_reason="stored_preference",
            )

        warning = None
        if normalized_preference and self._find_by_email(integrations, normalized_preference):
            warning = (
                f"Preferred calendar {normalized_preference} is unavailable; "
                "using the next best connected calendar."
            )

        primary = self._find_by_email(writable_integrations, self._normalize_email(user_email))
        if primary:
            return self._validate_selected_integration(
                primary,
                organisation_id,
                connected_account_count=len(integrations),
                selection_reason="primary_user_email",
                warning=warning,
            )

        official = [
            integration
            for integration in writable_integrations
            if self._email_domain(integration.integration_email) in (official_domains or set())
        ]
        if len(official) == 1:
            return self._validate_selected_integration(
                official[0],
                organisation_id,
                connected_account_count=len(integrations),
                selection_reason="official_domain",
                warning=warning,
            )

        last_used_id = await self._last_successful_integration_id(
            session,
            organisation_id=organisation_id,
            requested_by_user_id=requested_by_user_id,
            candidate_ids={integration.integration_id for integration in writable_integrations},
        )
        last_used = next(
            (
                integration
                for integration in writable_integrations
                if integration.integration_id == last_used_id
            ),
            None,
        )
        selected = last_used or writable_integrations[0]
        return self._validate_selected_integration(
            selected,
            organisation_id,
            connected_account_count=len(integrations),
            selection_reason=(
                "recent_successful_invite" if last_used else "most_recent_connection"
            ),
            warning=warning,
        )

    def _validate_selected_integration(
        self,
        integration: KnowledgeIntegration,
        organisation_id: UUID,
        *,
        connected_account_count: int = 1,
        selection_reason: str = "only_writable_calendar",
        warning: str | None = None,
    ) -> ResolvedCalendarIntegration:
        # Validate integration before handing back a token-bound session object.
        # Missing permissions here means we can return a precise "reconnect" error
        # instead of hiding failures during Graph writes.
        if integration.requires_reauth:
            provider_name = self._provider_display_name(integration.integration_type)
            raise CalendarInviteError(
                "calendar_reauth_required",
                f"Reconnect {provider_name} before I can send calendar invites.",
                status_code=409,
            )

        scopes = list(integration.scopes or [])
        missing = get_missing_required_scopes(
            self._calendar_capability_type(integration.integration_type),
            scopes,
        )
        if missing:
            provider_name = self._provider_display_name(integration.integration_type)
            labels = ", ".join(get_missing_scope_labels(missing))
            raise CalendarInviteError(
                "calendar_write_scope_required",
                f"Reconnect {provider_name} with these permissions: {labels}.",
                status_code=409,
            )

        if not self._has_usable_token(integration):
            # We still verify token material because stale integration rows can
            # remain active but no longer be usable for outbound calls.
            raise CalendarInviteError(
                "calendar_reauth_required",
                f"Reconnect {self._provider_display_name(integration.integration_type)} "
                "before I can send calendar invites.",
                status_code=409,
            )

        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        return ResolvedCalendarIntegration(
            integration=integration,
            token_info=token_info,
            provider=self._provider_key(integration.integration_type),
            connected_account_count=connected_account_count,
            selection_reason=selection_reason,
            warning=warning,
        )

    @staticmethod
    def _normalize_email(value: str | None) -> str | None:
        """Normalize optional sender identity before comparing connected accounts."""
        normalized = (value or "").strip().lower()
        return normalized or None

    @classmethod
    def _find_by_email(
        cls,
        integrations: list[KnowledgeIntegration],
        email: str | None,
    ) -> KnowledgeIntegration | None:
        """Return the connected calendar matching one already-normalized address."""
        if not email:
            return None
        return next(
            (
                integration
                for integration in integrations
                if cls._normalize_email(integration.integration_email) == email
            ),
            None,
        )

    @staticmethod
    def _email_domain(email: str | None) -> str | None:
        """Extract a normalized domain for official-account preference."""
        normalized = (email or "").strip().lower()
        return normalized.rsplit("@", 1)[1] if "@" in normalized else None

    @staticmethod
    async def _last_successful_integration_id(
        session,
        *,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        candidate_ids: set[UUID],
    ) -> UUID | None:
        """Return the calendar used by the requester's latest successful invite.

        The audit table is already the durable source for provider-side writes,
        so it is a more reliable preference signal than sync or connection time.
        """
        if not candidate_ids:
            return None
        stmt = (
            select(col(IntegrationActivityRequest.integration_id))
            .where(
                col(IntegrationActivityRequest.organisation_id) == organisation_id,
                col(IntegrationActivityRequest.requested_by_user_id) == requested_by_user_id,
                col(IntegrationActivityRequest.activity_type) == "send_calendar_invite",
                col(IntegrationActivityRequest.status)
                == IntegrationActivityRequestStatus.SENT.value,
                col(IntegrationActivityRequest.integration_id).in_(candidate_ids),
            )
            .order_by(col(IntegrationActivityRequest.updated_at).desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return cast(UUID | None, result.scalar_one_or_none())

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

    @staticmethod
    def _provider_key(integration_type: IntegrationType) -> str:
        if integration_type == IntegrationType.GOOGLE:
            return "google"
        return "microsoft"

    @staticmethod
    def _provider_display_name(integration_type: IntegrationType) -> str:
        if integration_type == IntegrationType.GOOGLE:
            return "Google"
        return "Microsoft"

    @staticmethod
    def _calendar_capability_type(integration_type: IntegrationType) -> IntegrationType:
        if integration_type == IntegrationType.GOOGLE:
            return IntegrationType.GOOGLE_CALENDAR
        return IntegrationType.OUTLOOK_CALENDAR


OutlookCalendarIntegrationResolver = CalendarIntegrationResolver
