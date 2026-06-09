"""Meeting summary ready notifications across email and linked channels.

Called by the deal worker after a deal-linked meeting summary is committed.
This service owns recipient resolution, channel targeting, and the generic
NotificationDelivery idempotency ledger; it deliberately does not generate or
mutate summaries.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, cast, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col
from src.domain.models.master_tables import Organisation, User
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import SupabaseUserStatus
from src.domain.models.organisation_tables.sync import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CRMDeal,
    GenerationStatus,
    IntegrationType,
    KnowledgeIntegration,
    MessagingChannel,
    MessagingDealLink,
    NotificationDelivery,
    NotificationDeliveryChannel,
    NotificationDeliveryStatus,
)
from src.infra.auth.email_service import email_service
from src.infra.config.configs import app_config
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

MEETING_SUMMARY_READY_NOTIFICATION = "meeting_summary_ready"
CALENDAR_EVENT_DEAL_ASSOCIATION_SOURCE = "calendar_event_deal_association"
NOTIFICATION_DELIVERY_STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class MeetingSummaryNotificationContext:
    """Loaded summary context shared by email and channel renderers."""

    organisation_id: UUID
    organisation_name: str
    deal_id: UUID
    deal_name: str | None
    event_id: UUID
    meeting_title: str | None
    meeting_start_at: datetime | None
    summary_data: dict[str, Any]
    association_id: UUID | None = None


@dataclass(frozen=True)
class NotificationRecipient:
    """Internal app user who is eligible for an email notification."""

    user_id: UUID
    email: str
    first_name: str | None


@dataclass(frozen=True)
class ChannelTarget:
    """Deal-linked messaging channel with enough provider context to send."""

    channel_id: UUID
    provider: str
    name: str | None
    delivery_context: dict[str, str]


@dataclass(frozen=True)
class RenderedEmail:
    """Email payload after subject and body rendering."""

    subject: str
    html: str
    text: str


async def _maybe_await(value: Any) -> Any:
    """Support monkeypatched sync helpers in unit tests."""
    if inspect.isawaitable(value):
        return await value
    return value


def _delivery_status_value(status: NotificationDeliveryStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


class MeetingSummaryNotificationService:
    """Sends summary-ready notifications after the generated summary is durable.

    The service is org-scoped. Every org-table query filters by
    `organisation_id`; provider/channel identifiers are delivery addresses, not
    tenant boundaries.
    """

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def build_summary_url(self, *, event_id: UUID) -> str:
        """Build the app deep link; frontend auth guard owns login redirect."""
        base_url = (app_config.APP_URL or "").rstrip("/") or "https://app.copilotgtm.com"
        return f"{base_url}/meetings?event_id={event_id}&section=summary"

    @staticmethod
    def _format_meeting_time(value: datetime | None) -> str:
        if value is None:
            return "Not available"
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def render_email(
        self,
        context: MeetingSummaryNotificationContext,
        recipient: NotificationRecipient,
    ) -> RenderedEmail:
        """Render the email without embedding summary content in the message body.

        We send only a deep link so the app's auth guard can enforce login and
        route the user directly to the meeting summary after authentication.
        """
        title = context.meeting_title or "Untitled meeting"
        url = self.build_summary_url(event_id=context.event_id)
        meeting_time = self._format_meeting_time(context.meeting_start_at)

        safe_name = html_escape(recipient.first_name or "there")
        safe_title = html_escape(title)
        safe_time = html_escape(meeting_time)
        safe_org = html_escape(context.organisation_name)
        safe_url = html_escape(url)

        subject = f"Meeting summary is ready: {title}"
        html = f"""
            <h2>Hi {safe_name},</h2>
            <p>The summary for <strong>{safe_title}</strong> is ready in CopilotGTM.</p>
            <p><strong>Meeting time:</strong> {safe_time}</p>
            <div class="button-container">
                <a href="{safe_url}" class="button">Open meeting summary</a>
            </div>
            <p>You are receiving this because you are an internal attendee in {safe_org}.
            External attendees are not notified.</p>
        """
        text = (
            f"Hi {recipient.first_name or 'there'},\n\n"
            f'The summary for "{title}" is ready in CopilotGTM.\n\n'
            f"Meeting time: {meeting_time}\n"
            f"Open summary: {url}\n\n"
            f"You are receiving this because you are an internal attendee in "
            f"{context.organisation_name}. External attendees are not notified."
        )
        return RenderedEmail(subject=subject, html=html, text=text)

    def render_channel_text(self, context: MeetingSummaryNotificationContext) -> str:
        """Render a short channel message that links back to the app summary."""
        title = context.meeting_title or "Untitled meeting"
        url = self.build_summary_url(event_id=context.event_id)
        meeting_time = self._format_meeting_time(context.meeting_start_at)
        return (
            f"**Meeting summary is ready:** {title}\n"
            f"Meeting time: {meeting_time}\n"
            f"Open summary: {url}"
        )

    async def _fetch_org_users(self) -> list[User]:
        async for session in acquire_master_async_session():
            # Master users are filtered by organisation and active approval state so
            # notifications only go to current internal users, not external attendees.
            stmt = (
                select(User)
                .where(
                    col(User.organisation_id) == self.organisation_id,
                    col(User.status) == SupabaseUserStatus.ACTIVE,
                    col(User.admin_status) == AdminUserStatus.APPROVED,
                )
                .order_by(col(User.email))
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        return []

    async def _fetch_internal_recipient_emails_for_event(self, event_id: UUID) -> set[str]:
        async for session in acquire_org_async_session(self.organisation_id):
            # Calendar attendees are filtered through the org-owned event row. An
            # attendee email alone is not trusted as proof of tenant membership.
            stmt = (
                select(col(CalendarAttendee.email))
                .join(CalendarEvent, col(CalendarEvent.event_id) == col(CalendarAttendee.event_id))
                .where(
                    col(CalendarEvent.organisation_id) == self.organisation_id,
                    col(CalendarEvent.event_id) == event_id,
                )
            )
            result = await session.execute(stmt)
            return {
                str(email).strip().lower()
                for email in result.scalars().all()
                if str(email or "").strip()
            }
        return set()

    async def resolve_email_recipients(
        self,
        *,
        event_id: UUID | None = None,
    ) -> list[NotificationRecipient]:
        """Return active internal users who were invited to the meeting."""
        allowed_emails = (
            await _maybe_await(self._fetch_internal_recipient_emails_for_event(event_id))
            if event_id
            else set()
        )
        if not allowed_emails:
            return []

        users = await _maybe_await(self._fetch_org_users())
        recipients: list[NotificationRecipient] = []
        seen: set[str] = set()
        for user in users:
            # Intersect the attendee roster with approved app users. This keeps
            # client/buyer attendees out of internal summary notifications.
            if user.organisation_id != self.organisation_id:
                continue
            if user.status != SupabaseUserStatus.ACTIVE:
                continue
            if user.admin_status != AdminUserStatus.APPROVED:
                continue
            email = str(user.email or "").strip().lower()
            if not email or email in seen:
                continue
            if email not in allowed_emails:
                continue
            seen.add(email)
            recipients.append(
                NotificationRecipient(
                    user_id=user.user_id,
                    email=email,
                    first_name=user.first_name,
                )
            )
        return recipients

    async def _fetch_deal_linked_channel_rows(self, deal_id: UUID) -> list[Any]:
        async for session in acquire_org_async_session(self.organisation_id):
            # Channel notifications are limited to active channels already linked
            # to this deal in this org; the external channel id is never enough.
            stmt = (
                select(
                    col(MessagingChannel.channel_id),
                    col(MessagingChannel.external_channel_id),
                    col(MessagingChannel.channel_name),
                    col(MessagingChannel.channel_metadata),
                    col(MessagingChannel.is_archived),
                    col(MessagingChannel.is_deleted),
                    col(KnowledgeIntegration.integration_type),
                )
                .join(
                    MessagingDealLink,
                    col(MessagingDealLink.channel_id) == col(MessagingChannel.channel_id),
                )
                .join(
                    KnowledgeIntegration,
                    col(KnowledgeIntegration.integration_id)
                    == col(MessagingDealLink.integration_id),
                )
                .where(
                    col(MessagingDealLink.organisation_id) == self.organisation_id,
                    col(MessagingDealLink.deal_id) == deal_id,
                    col(MessagingDealLink.channel_id).isnot(None),
                    col(MessagingDealLink.is_deleted).is_(False),
                    col(MessagingChannel.is_deleted).is_(False),
                    col(MessagingChannel.is_archived) == "false",
                    cast(col(KnowledgeIntegration.is_active), Boolean).is_(True),
                    col(KnowledgeIntegration.integration_type).in_(
                        [IntegrationType.SLACK, IntegrationType.TEAMS]
                    ),
                )
                .distinct(col(MessagingChannel.channel_id))
            )
            result = await session.execute(stmt)
            return list(result.all())
        return []

    async def resolve_channel_targets(self, deal_id: UUID) -> list[ChannelTarget]:
        """Resolve active Slack/Teams channels that can receive a deal summary link."""
        rows = await _maybe_await(self._fetch_deal_linked_channel_rows(deal_id))
        targets: list[ChannelTarget] = []
        seen: set[UUID] = set()

        for row in rows:
            if row.channel_id in seen:
                continue
            if bool(row.is_deleted) or str(row.is_archived).lower() == "true":
                continue

            integration_type = row.integration_type
            provider = (
                integration_type.value
                if hasattr(integration_type, "value")
                else str(integration_type)
            )
            metadata = dict(row.channel_metadata or {})
            delivery_context: dict[str, str] | None = None

            if provider == IntegrationType.SLACK.value:
                # Slack sends need both workspace and channel ids. Older channel
                # rows may only have one, so skip rather than sending to ambiguity.
                team_id = metadata.get("workspace_id") or metadata.get("team_id")
                channel_id = metadata.get("slack_channel_id") or row.external_channel_id
                if team_id and channel_id:
                    delivery_context = {"team_id": str(team_id), "channel_id": str(channel_id)}
            elif provider == IntegrationType.TEAMS.value:
                # Teams proactive sends require the original conversation context.
                # A channel row without service_url cannot be safely addressed.
                service_url = metadata.get("service_url")
                conversation_id = metadata.get("conversation_id") or row.external_channel_id
                if service_url and conversation_id:
                    delivery_context = {
                        "service_url": str(service_url),
                        "conversation_id": str(conversation_id),
                    }

            if not delivery_context:
                logger.info(
                    "Skipping meeting summary channel notification target without delivery context",
                    extra={
                        "event": "meeting_summary_channel_notification_context_missing",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                        "channel_id": str(row.channel_id),
                        "provider": provider,
                    },
                )
                continue

            seen.add(row.channel_id)
            targets.append(
                ChannelTarget(
                    channel_id=row.channel_id,
                    provider=provider,
                    name=row.channel_name,
                    delivery_context=delivery_context,
                )
            )

        return targets

    async def load_context(
        self,
        *,
        deal_id: UUID,
        event_id: UUID,
    ) -> MeetingSummaryNotificationContext | None:
        """Load the completed deal-linked summary that notification workers announce."""
        organisation_name = "your organisation"
        async for master_session in acquire_master_async_session():
            org_stmt = select(col(Organisation.organisation_name)).where(
                col(Organisation.organisation_id) == self.organisation_id
            )
            organisation_name = (
                await master_session.execute(org_stmt)
            ).scalar_one_or_none() or organisation_name

        async for session in acquire_org_async_session(self.organisation_id):
            # The association row is the source of truth for deal-linked summaries.
            # Non-deal meeting summaries live on Meeting and are intentionally not
            # announced through this deal-channel notification service.
            stmt = (
                select(CalendarEvent, CalendarEventDealAssociation, CRMDeal)
                .join(
                    CalendarEventDealAssociation,
                    col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
                )
                .join(CRMDeal, col(CRMDeal.deal_id) == col(CalendarEventDealAssociation.deal_id))
                .where(
                    col(CalendarEvent.organisation_id) == self.organisation_id,
                    col(CalendarEvent.event_id) == event_id,
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.deal_id) == deal_id,
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                    col(CalendarEventDealAssociation.meeting_summary_status)
                    == GenerationStatus.COMPLETED,
                    col(CalendarEventDealAssociation.meeting_summary_data).isnot(None),
                )
            )
            row = (await session.execute(stmt)).first()
            if not row:
                return None

            event, association, deal = row
            summary_data = association.meeting_summary_data
            if not isinstance(summary_data, dict):
                return None

            return MeetingSummaryNotificationContext(
                organisation_id=self.organisation_id,
                organisation_name=organisation_name,
                deal_id=deal_id,
                deal_name=deal.name,
                event_id=event_id,
                meeting_title=event.title,
                meeting_start_at=event.start_at,
                summary_data=summary_data,
                association_id=association.association_id,
            )
        return None

    async def send_email(
        self,
        context: MeetingSummaryNotificationContext,
        recipient: NotificationRecipient,
    ) -> bool:
        """Send one already-rendered email through the shared email service."""
        rendered = self.render_email(context, recipient)
        return await email_service.send_email(
            recipient.email,
            rendered.subject,
            email_service._create_styled_html(  # noqa: SLF001
                rendered.subject,
                f"Summary ready for {context.meeting_title or 'your meeting'}",
                rendered.html,
            ),
            rendered.text,
        )

    async def send_channel(
        self,
        context: MeetingSummaryNotificationContext,
        target: ChannelTarget,
    ) -> bool:
        """Send one short summary-ready message to Slack or Teams."""
        text = self.render_channel_text(context)
        if target.provider == IntegrationType.SLACK.value:
            from src.service.bots.slack_bot_service import SlackBotService

            return await SlackBotService()._send_message(  # noqa: SLF001
                target.delivery_context["team_id"],
                target.delivery_context["channel_id"],
                text,
            )

        if target.provider == IntegrationType.TEAMS.value:
            from src.service.bots.teams_bot_service import TeamsBotService

            return await TeamsBotService().send_proactive_message(
                service_url=target.delivery_context["service_url"],
                conversation_id=target.delivery_context["conversation_id"],
                message=text,
            )

        return False

    async def _claim_delivery(
        self,
        *,
        context: MeetingSummaryNotificationContext,
        channel: NotificationDeliveryChannel,
        target_key: str,
        target_label: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID | None:
        """Claim one notification target before performing external I/O.

        The insert-first path handles first delivery. The locked existing-row path
        lets failed deliveries retry while already-sent rows short-circuit.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            insert_stmt = (
                pg_insert(NotificationDelivery)
                .values(
                    organisation_id=self.organisation_id,
                    notification_type=MEETING_SUMMARY_READY_NOTIFICATION,
                    source_type=CALENDAR_EVENT_DEAL_ASSOCIATION_SOURCE,
                    source_id=context.association_id or context.event_id,
                    channel=channel.value,
                    target_key=target_key,
                    target_label=target_label,
                    status=NotificationDeliveryStatus.SENDING.value,
                    attempt_count=1,
                    delivery_metadata={
                        "deal_id": str(context.deal_id),
                        **(metadata or {}),
                    },
                    updated_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(constraint="uq_notification_delivery_target")
                .returning(NotificationDelivery.delivery_id)
            )
            inserted = (await session.execute(insert_stmt)).scalar_one_or_none()
            if inserted:
                await session.commit()
                return inserted

            existing_stmt = (
                select(NotificationDelivery)
                .where(
                    col(NotificationDelivery.organisation_id) == self.organisation_id,
                    col(NotificationDelivery.notification_type)
                    == MEETING_SUMMARY_READY_NOTIFICATION,
                    col(NotificationDelivery.source_type) == CALENDAR_EVENT_DEAL_ASSOCIATION_SOURCE,
                    col(NotificationDelivery.source_id)
                    == (context.association_id or context.event_id),
                    col(NotificationDelivery.channel) == channel.value,
                    col(NotificationDelivery.target_key) == target_key,
                )
                .with_for_update(skip_locked=True)
            )
            existing = (await session.execute(existing_stmt)).scalars().first()
            if not existing:
                await session.rollback()
                return None

            existing_status = _delivery_status_value(existing.status)
            if existing_status == NotificationDeliveryStatus.SENT.value:
                await session.rollback()
                return None

            if existing_status == NotificationDeliveryStatus.SENDING.value:
                updated_at = existing.updated_at
                if updated_at and updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                if (
                    updated_at
                    and datetime.now(UTC) - updated_at < NOTIFICATION_DELIVERY_STALE_AFTER
                ):
                    await session.rollback()
                    return None

            existing.status = NotificationDeliveryStatus.SENDING
            existing.attempt_count += 1
            existing.last_error = None
            existing.updated_at = datetime.now(UTC)
            existing.delivery_metadata = {
                "deal_id": str(context.deal_id),
                **(metadata or {}),
            }
            await session.commit()
            return existing.delivery_id
        return None

    async def _mark_delivery_sent(self, delivery_id: UUID) -> None:
        """Mark a claimed delivery as sent after the provider call succeeds."""
        async for session in acquire_org_async_session(self.organisation_id):
            delivery = await session.get(NotificationDelivery, delivery_id)
            if delivery:
                delivery.status = NotificationDeliveryStatus.SENT
                delivery.sent_at = datetime.now(UTC)
                delivery.last_error = None
                delivery.updated_at = datetime.now(UTC)
                await session.commit()
            return

    async def _mark_delivery_failed(self, delivery_id: UUID, error: str) -> None:
        """Persist provider failure details so a later retry can reclaim the row."""
        async for session in acquire_org_async_session(self.organisation_id):
            delivery = await session.get(NotificationDelivery, delivery_id)
            if delivery:
                delivery.status = NotificationDeliveryStatus.FAILED
                delivery.last_error = error[:1000]
                delivery.updated_at = datetime.now(UTC)
                await session.commit()
            return

    async def notify(self, *, deal_id: UUID, event_id: UUID) -> dict[str, int | str]:
        """Notify internal users and active deal channels for one completed summary."""
        context = await self.load_context(deal_id=deal_id, event_id=event_id)
        if not context:
            return {"status": "skipped", "reason": "summary_not_ready"}

        recipients = await self.resolve_email_recipients(event_id=event_id)
        channels = await self.resolve_channel_targets(deal_id)

        sent = 0
        skipped = 0
        failed = 0

        for recipient in recipients:
            # Claim before sending so a worker crash after the provider call cannot
            # make another worker send the same target concurrently.
            delivery_id = await self._claim_delivery(
                context=context,
                channel=NotificationDeliveryChannel.EMAIL,
                target_key=str(recipient.user_id),
                target_label=recipient.email,
                metadata={"email": recipient.email},
            )
            if not delivery_id:
                skipped += 1
                continue

            try:
                delivered = await self.send_email(context, recipient)
                if delivered:
                    await self._mark_delivery_sent(delivery_id)
                    sent += 1
                else:
                    await self._mark_delivery_failed(delivery_id, "email_send_failed")
                    failed += 1
            except Exception as exc:
                logger.exception(
                    "Meeting summary email notification failed",
                    extra={
                        "event": "meeting_summary_email_notification_failed",
                        "organisation_id": str(self.organisation_id),
                        "event_id": str(event_id),
                        "deal_id": str(deal_id),
                        "recipient_user_id": str(recipient.user_id),
                    },
                )
                await self._mark_delivery_failed(delivery_id, str(exc))
                failed += 1

        channel_map = {
            IntegrationType.SLACK.value: NotificationDeliveryChannel.SLACK,
            IntegrationType.TEAMS.value: NotificationDeliveryChannel.TEAMS,
        }
        for target in channels:
            delivery_channel = channel_map.get(target.provider)
            if not delivery_channel:
                skipped += 1
                continue

            delivery_id = await self._claim_delivery(
                context=context,
                channel=delivery_channel,
                target_key=str(target.channel_id),
                target_label=target.name,
                metadata={
                    "provider": target.provider,
                    "delivery_context": target.delivery_context,
                },
            )
            if not delivery_id:
                skipped += 1
                continue

            try:
                delivered = await self.send_channel(context, target)
                if delivered:
                    await self._mark_delivery_sent(delivery_id)
                    sent += 1
                else:
                    await self._mark_delivery_failed(delivery_id, "channel_send_failed")
                    failed += 1
            except Exception as exc:
                logger.exception(
                    "Meeting summary channel notification failed",
                    extra={
                        "event": "meeting_summary_channel_notification_failed",
                        "organisation_id": str(self.organisation_id),
                        "event_id": str(event_id),
                        "deal_id": str(deal_id),
                        "channel_id": str(target.channel_id),
                        "provider": target.provider,
                    },
                )
                await self._mark_delivery_failed(delivery_id, str(exc))
                failed += 1

        return {
            "status": "failed" if failed else "completed",
            "targets_found": len(recipients) + len(channels),
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
        }
