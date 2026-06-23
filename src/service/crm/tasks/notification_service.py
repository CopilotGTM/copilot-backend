"""Task-assignment notification service for user-created CRM tasks."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import User
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.service.crm.access.crm_user_service import build_user_display_name
from src.service.crm.tasks.contracts import TaskWriteServiceError

logger = get_logger("task_notification_service")


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
