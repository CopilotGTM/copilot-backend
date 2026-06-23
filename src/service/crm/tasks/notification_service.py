"""Task-assignment notification service for user-created CRM tasks."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import User
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.chat_session_models import ChatType
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.config.configs import app_config
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.service.crm.access.crm_user_service import build_user_display_name
from src.service.crm.tasks.contracts import TaskWriteServiceError

logger = get_logger("task_notification_service")


def _task_notification_user_id(task: CRMTask) -> UUID | None:
    metadata = getattr(task, "task_metadata", None) or {}
    public_user_id = metadata.get("assigned_to_public_user_id")
    if public_user_id:
        try:
            return UUID(str(public_user_id))
        except ValueError:
            logger.warning("Invalid assigned_to_public_user_id on task %s", task.task_id)
    return getattr(task, "assigned_to_user_id", None)


def _task_deep_link(task: CRMTask) -> str:
    base_url = (app_config.APP_URL or "").rstrip("/") or "https://app.copilotgtm.com"
    return f"{base_url}/tasks?task_id={task.task_id}"


async def send_task_assignment_whatsapp(
    *,
    organisation_id: UUID,
    user_id: UUID,
    task: CRMTask,
    deal: CRMDeal | None,
    assigner_name: str,
    is_update: bool,
    due_date_text: str | None,
) -> bool:
    """Send a task assignment/update WhatsApp template to a linked internal user."""
    from src.service.channels import (
        DEFAULT_TEMPLATE_LANGUAGE,
        TASK_REMINDER_TEMPLATE_NAME,
        OutboundChannelService,
        OutboundChatContextService,
        WhatsAppOutboundProvider,
    )

    async with master_async_session_scope() as master_session:
        target = await OutboundChannelService().resolve_user_channel(
            master_session,
            organisation_id,
            user_id,
            "whatsapp",
        )
    if not target:
        return False

    title = task.title or "(no subject)"
    due_text = due_date_text or "No due date"
    deal_name = deal.name if deal else "No deal"
    url = _task_deep_link(task)
    text = (
        f"Task {'updated' if is_update else 'assigned'}: {title}\n"
        f"Assigned by: {assigner_name}\n"
        f"Due: {due_text}\n"
        f"Deal: {deal_name}\n"
        f"Open task: {url}"
    )

    message_id = await WhatsAppOutboundProvider().send_template_message_with_id(
        phone_number_id=str(target.delivery_context.get("phone_number_id", "")),
        recipient_wa_id=str(target.delivery_context.get("recipient_wa_id", "")),
        template_name=TASK_REMINDER_TEMPLATE_NAME,
        language_code=DEFAULT_TEMPLATE_LANGUAGE,
        body_parameters=[title, due_text, deal_name, url],
    )
    if not message_id:
        return False

    try:
        await OutboundChatContextService().persist_outbound_notification_context(
            organisation_id=organisation_id,
            user_id=user_id,
            chat_type=ChatType.WHATSAPP_CHAT,
            text=text,
            metadata={
                "notification_type": "task_assignment",
                "source_type": "crm_task",
                "source_id": str(task.task_id),
                "template_name": TASK_REMINDER_TEMPLATE_NAME,
                "whatsapp_message_id": message_id,
                "deep_link": url,
            },
        )
    except Exception:
        logger.warning(
            "Failed to persist outbound WhatsApp task chat context",
            extra={
                "event": "task_whatsapp_context_persist_failed",
                "organisation_id": str(organisation_id),
                "task_id": str(task.task_id),
                "user_id": str(user_id),
                "whatsapp_message_id": message_id,
            },
            exc_info=True,
        )
    return True


class TaskNotificationService:
    """Validate assignees and send best-effort assignment emails."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def validate_external_assignee_domain(self, email: str) -> None:
        """Reject external assignee if org has domains set and email does not match any."""
        async with master_async_session_scope() as master_session:
            org = await master_session.get(Organisation, self.organisation_id)

        if not org or not org.domain:
            return

        email_domain = email.rsplit("@", 1)[-1].lower().removeprefix("www.")
        allowed_domains = [d.lower().removeprefix("www.") for d in org.domain]
        if email_domain not in allowed_domains:
            display = ", ".join(d.removeprefix("www.") for d in org.domain)
            raise TaskWriteServiceError(
                f"External assignee email must be from: {display}",
                400,
            )

    async def send_assignment_email(
        self, task: CRMTask, deal: CRMDeal | None, *, is_update: bool = False
    ) -> None:
        """Send task assignment or update email to the task assignee, CC the assigner."""
        recipient_email = task.external_assignee_email or task.assigned_to_email
        recipient_name = task.external_assignee_name or task.assigned_to_name
        if not recipient_email:
            return

        assigner_name = "Someone"
        assigner_email: str | None = None
        try:
            async with master_async_session_scope() as master_session:
                actor = await master_session.get(User, self.actor_user_id)
            if actor:
                assigner_name = build_user_display_name(actor) or str(actor.email)
                assigner_email = str(actor.email)
        except Exception:
            logger.debug("Could not resolve assigner details", exc_info=True)
        if assigner_email and assigner_email.lower() == recipient_email.lower():
            assigner_email = None

        due_date_str = None
        if task.due_at:
            due_date_str = task.due_at.strftime("%B %d, %Y at %I:%M %p")
        elif task.due_date:
            due_date_str = task.due_date.strftime("%B %d, %Y")

        try:
            from src.infra.auth.email_service import email_service

            await email_service.send_task_assignment_email(
                to_email=recipient_email,
                assignee_name=recipient_name,
                assigner_name=assigner_name,
                task_subject=task.title or "(no subject)",
                task_body=task.description,
                task_priority=task.priority,
                task_due_date=due_date_str,
                task_status=task.status,
                deal_name=deal.name if deal else None,
                cc_email=assigner_email,
                is_update=is_update,
            )
        except Exception:
            logger.warning(
                "Failed to send task %s email for task %s to %s",
                "update" if is_update else "assignment",
                task.task_id,
                recipient_email,
                exc_info=True,
            )

        assignee_user_id = (
            None if task.external_assignee_email else _task_notification_user_id(task)
        )
        if not assignee_user_id:
            return

        try:
            await send_task_assignment_whatsapp(
                organisation_id=self.organisation_id,
                user_id=assignee_user_id,
                task=task,
                deal=deal,
                assigner_name=assigner_name,
                is_update=is_update,
                due_date_text=due_date_str,
            )
        except Exception:
            logger.warning(
                "Failed to send task %s WhatsApp notification for task %s",
                "update" if is_update else "assignment",
                task.task_id,
                exc_info=True,
            )
