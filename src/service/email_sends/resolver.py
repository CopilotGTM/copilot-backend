"""Resolve the current user's Outlook Mail integration for outbound sends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.email_sends.models import EmailSendError
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    get_missing_scope_labels,
)


@dataclass(frozen=True)
class ResolvedEmailIntegration:
    """Resolved sender context for a single email send operation."""

    integration: KnowledgeIntegration
    token_info: IntegrationTokenInfo


class OutlookEmailIntegrationResolver:
    """Find a user-owned Outlook Mail connection with Mail.Send permission."""

    async def resolve(
        self,
        session,
        organisation_id: UUID,
        requested_by_user_id: UUID,
        *,
        sender_email: str | None = None,
    ) -> ResolvedEmailIntegration:
        stmt = (
            select(KnowledgeIntegration)
            .where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.created_by_user_id) == requested_by_user_id,
                col(KnowledgeIntegration.integration_type) == IntegrationType.OUTLOOK_EMAIL,
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
            raise EmailSendError(
                "email_integration_required",
                "Connect Outlook Email before I can send emails.",
                status_code=409,
            )

        if sender_email:
            selected_email = sender_email.strip().lower()
            integration = next(
                (
                    candidate
                    for candidate in integrations
                    if (candidate.integration_email or "").strip().lower() == selected_email
                ),
                None,
            )
            if integration is None:
                raise EmailSendError(
                    "email_sender_not_found",
                    (
                        f"I could not find an Outlook Email connection for {sender_email}. "
                        f"Available sender accounts: {self._format_available_accounts(integrations)}."
                    ),
                    status_code=409,
                )
            return self._validate_selected_integration(integration, organisation_id)

        writable_integrations = [
            integration
            for integration in integrations
            if not get_missing_required_scopes(
                integration.integration_type,
                list(integration.scopes or []),
            )
            and not integration.requires_reauth
            and self._has_usable_token(integration)
        ]

        if not writable_integrations:
            return self._validate_selected_integration(integrations[0], organisation_id)

        if len(writable_integrations) > 1:
            raise EmailSendError(
                "email_sender_selection_required",
                (
                    "You have multiple Outlook mailboxes that can send emails. "
                    f"Which sender should I use? Available sender accounts: "
                    f"{self._format_available_accounts(writable_integrations)}."
                ),
                status_code=409,
            )

        return self._validate_selected_integration(writable_integrations[0], organisation_id)

    def _validate_selected_integration(
        self,
        integration: KnowledgeIntegration,
        organisation_id: UUID,
    ) -> ResolvedEmailIntegration:
        if integration.requires_reauth:
            raise EmailSendError(
                "email_reauth_required",
                "Reconnect Outlook Email before I can send emails.",
                status_code=409,
            )

        missing = get_missing_required_scopes(
            integration.integration_type,
            list(integration.scopes or []),
        )
        if missing:
            labels = ", ".join(get_missing_scope_labels(missing))
            raise EmailSendError(
                "email_send_scope_required",
                f"Reconnect Outlook Email with these permissions: {labels}.",
                status_code=409,
            )

        if not self._has_usable_token(integration):
            raise EmailSendError(
                "email_reauth_required",
                "Reconnect Outlook Email before I can send emails.",
                status_code=409,
            )

        return ResolvedEmailIntegration(
            integration=integration,
            token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
        )

    @staticmethod
    def _has_usable_token(integration: KnowledgeIntegration) -> bool:
        credentials = dict(integration.credentials or {})
        return bool(credentials.get("refresh_token") or credentials.get("access_token"))

    @staticmethod
    def _format_available_accounts(integrations: list[KnowledgeIntegration]) -> str:
        values = [
            integration.integration_email or integration.name or str(integration.integration_id)
            for integration in integrations
        ]
        return ", ".join(values)
