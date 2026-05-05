from __future__ import annotations

from uuid import UUID

from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded

from src.domain.models.user_models import UserRealtimeRuntimeConfig
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app

logger = get_logger("chat_voice_realtime_task")


@app.task(
    name="chat.voice_realtime_sideband",
    bind=True,
    acks_late=True,
    max_retries=0,
    soft_time_limit=4200,
    time_limit=4500,
)
def voice_realtime_sideband(
    self,
    organisation_id: str,
    user_id: str,
    user_email: str | None,
    chat_session_id: str,
    session_id: str,
    call_id: str,
    runtime_config: dict[str, object],
    job_id: str | None = None,
    user_timezone: str | None = None,
):
    """Own one live voice sideband session inside the chat worker."""

    logger.info(
        "Starting voice realtime sideband org=%s user=%s call=%s session=%s",
        organisation_id,
        user_id,
        call_id,
        chat_session_id,
    )

    try:
        from src.service.voice_realtime.runtime import VoiceRealtimeSidebandService

        service = VoiceRealtimeSidebandService(
            organisation_id=UUID(organisation_id),
            user_id=UUID(user_id),
            user_email=user_email,
            chat_session_id=chat_session_id,
            session_id=session_id,
            call_id=call_id,
            runtime_config=UserRealtimeRuntimeConfig.model_validate(runtime_config),
            job_id=UUID(job_id) if job_id else None,
            user_timezone=user_timezone,
        )
        run_async(service.run())
        logger.info("Voice realtime sideband completed call=%s", call_id)
    except (SoftTimeLimitExceeded, TimeLimitExceeded):
        logger.exception("Voice realtime sideband timed out call=%s", call_id)
        raise
    except Exception:
        logger.exception("Voice realtime sideband failed call=%s", call_id)
        raise
