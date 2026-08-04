"""Retryable worker orchestration for durable external organisation signup."""

from uuid import UUID

from src.domain.models.master_tables import SignupRequest, SignupRequestStatus
from src.infra.auth.email_service import EmailService
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import (
    KaiTrainingService,
    enforce_organisation_research_quota,
)
from src.service.orgnisation.organisation_provisioning_service import (
    OrganisationProvisioningService,
)
from src.service.users.signup_service import SignupService
from src.workers.organization_worker.celery_app import app
from src.workers.organization_worker.client import send_organization_research_task

logger = get_logger("organization_signup_task")


async def _run_post_activation(signup: SignupRequest) -> None:
    """Run non-critical founder follow-up after durable activation."""
    organisation_id = signup.organisation_id
    if organisation_id is None:
        logger.error("Active signup %s has no organisation", signup.signup_request_id)
        return
    try:
        sent = await EmailService().send_signup_welcome_email(
            signup.normalized_email,
            signup.first_name,
        )
        if not sent:
            logger.error("Signup welcome email was not sent for %s", organisation_id)
    except Exception:
        logger.exception("Unable to send signup welcome email for %s", organisation_id)

    run_id: UUID | None = None
    research_queued = False
    try:
        async with master_async_session_scope() as session:
            _, should_enqueue, run_id = await KaiTrainingService(
                session, organisation_id
            ).mark_pending()
        if should_enqueue:
            assert run_id is not None
            enforce_organisation_research_quota(organisation_id)
            await send_organization_research_task(str(organisation_id), str(run_id))
            research_queued = True
    except Exception as exc:
        logger.exception("Unable to queue signup research for %s", organisation_id)
        if run_id is None:
            return
        try:
            async with master_async_session_scope() as session:
                await KaiTrainingService(session, organisation_id).mark_failed(
                    run_id,
                    str(exc),
                    clear_cooldown=not research_queued,
                )
        except Exception:
            logger.exception(
                "Unable to record signup research failure for %s",
                organisation_id,
            )


async def _provision_signup(signup_request_id: UUID) -> dict[str, str]:
    """Resume one signup from its last committed control-plane milestone."""
    # Each phase owns a fresh master session. Provider and tenant I/O therefore
    # cannot hold a control-plane connection, and a worker crash replays safely.
    async with master_async_session_scope() as session:
        signup, organisation, _user, founder = await SignupService(session).ensure_control_plane(
            signup_request_id
        )

    provisioning = OrganisationProvisioningService()
    if signup.status == SignupRequestStatus.CONTROL_PLANE_CREATED:
        await provisioning.provision_founder_tenant(
            organisation.organisation_id,
            founder,
        )
        async with master_async_session_scope() as session:
            signup = await SignupService(session).mark_tenant_defaults_created(signup_request_id)

    if signup.status == SignupRequestStatus.TENANT_DEFAULTS_CREATED:
        await provisioning.provision_storage(organisation.organisation_id)
        async with master_async_session_scope() as session:
            signup = await SignupService(session).mark_storage_created(signup_request_id)

    if signup.status == SignupRequestStatus.STORAGE_CREATED:
        async with master_async_session_scope() as session:
            signup = await SignupService(session).activate(signup_request_id)
        await _run_post_activation(signup)

    return {
        "signup_request_id": str(signup.signup_request_id),
        "status": SignupRequestStatus(signup.status).value,
    }


async def _mark_reconciliation(signup_request_id: UUID) -> None:
    """Record a retry-safe local failure without persisting exception content."""
    async with master_async_session_scope() as session:
        await SignupService(session).mark_reconciliation_required(
            signup_request_id,
            "SIGNUP_PROVISIONING_FAILED",
        )


@app.task(
    name="organization.signup.provision",
    bind=True,
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def provision_signup_task(self, signup_request_id: str) -> dict[str, str]:
    """Provision one durable signup, retrying idempotent phases with backoff."""
    request_id = UUID(signup_request_id)
    try:
        return run_async(_provision_signup(request_id))
    except Exception as exc:
        # Persist reconciliation before Celery records failure. Exhausted retries
        # remain visible to operations instead of deleting or claiming success.
        run_async(_mark_reconciliation(request_id))
        if self.request.retries >= self.max_retries:
            logger.exception("Signup provisioning retries exhausted")
            raise
        countdown = 30 * (2**self.request.retries)
        raise self.retry(exc=exc, countdown=countdown) from exc
