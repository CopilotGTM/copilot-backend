from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.domain.models.user_models import (
    RealtimeControlMode,
    RealtimeConversationMode,
    RealtimeToolPolicy,
    RealtimeTurnMode,
    UserRealtimeCallAttachRequest,
    UserRealtimeCallAttachResponse,
    UserRealtimeRuntimeConfig,
    UserRealtimeSessionRequest,
    UserRealtimeSessionResponse,
)
from src.infra.auth.user_context import UserIDs
from src.infra.config.agent_config import agent_config
from src.infra.config.configs import openai_config, security_secrets_config
from src.infra.config.constant import RealtimeErrors
from src.infra.database.sessions import acquire_org_async_session, master_async_session_scope
from src.infra.messaging.redis_client import get_redis_client
from src.infra.messaging.task_dispatcher import send_task_async
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import (
    load_organisation_runtime_context,
    with_organisation_context_policy,
)
from src.service.users.voice_realtime_contract import (
    VOICE_REALTIME_QUEUE,
    VOICE_REALTIME_TASK_NAME,
    build_voice_realtime_allowed_tools,
    build_voice_realtime_tool_schemas,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class RealtimeSessionServiceError(Exception):
    """Expected service-layer error for realtime bootstrap failures."""

    message: str
    status_code: int

    def __str__(self) -> str:
        return self.message


class RealtimeSessionService:
    """Creates Azure realtime client secrets plus a backend-authored runtime contract."""

    def __init__(self, user_ids: UserIDs):
        self.user_ids = user_ids

    async def close(self) -> None:
        """Keep a consistent service dependency shape."""
        return None

    async def create_session(
        self,
        session_request: UserRealtimeSessionRequest,
        db: AsyncSession | None = None,
    ) -> UserRealtimeSessionResponse:
        endpoint = (openai_config.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
        api_key = openai_config.AZURE_OPENAI_API_KEY
        deployment = agent_config.VOICE_REALTIME_MODEL
        voice = agent_config.VOICE_REALTIME_VOICE
        request_id = str(uuid4())

        if not endpoint or not api_key or not deployment:
            logger.error(
                "Realtime bootstrap blocked: missing Azure OpenAI config",
                extra={
                    "event": "realtime_bootstrap_config_missing",
                    "user_id": str(self.user_ids.user_id),
                    "organisation_id": str(self.user_ids.organisation_id),
                    "request_id": request_id,
                    "has_endpoint": bool(endpoint),
                    "has_api_key": bool(api_key),
                    "has_deployment": bool(deployment),
                },
            )
            raise RealtimeSessionServiceError(RealtimeErrors.REALTIME_NOT_CONFIGURED, 500)

        url = f"{endpoint}/openai/v1/realtime/client_secrets"
        payload = {
            "session": {
                "type": "realtime",
                "model": deployment,
                "audio": {
                    "output": {
                        "voice": voice,
                    },
                },
            }
        }
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
            "x-ms-client-request-id": request_id,
        }
        start = perf_counter()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                azure_response = await client.post(url, headers=headers, json=payload)
            azure_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            duration_ms = round((perf_counter() - start) * 1000)
            status_code = exc.response.status_code if exc.response is not None else None
            logger.warning(
                "Azure realtime bootstrap request failed",
                extra={
                    "event": "realtime_bootstrap_upstream_error",
                    "user_id": str(self.user_ids.user_id),
                    "organisation_id": str(self.user_ids.organisation_id),
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "upstream_status_code": status_code,
                    "model": deployment,
                    "voice": voice,
                    "conversation_mode": session_request.conversation_mode.value,
                },
            )
            raise RealtimeSessionServiceError(RealtimeErrors.SESSION_BOOTSTRAP_FAILED, 502) from exc
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            duration_ms = round((perf_counter() - start) * 1000)
            logger.warning(
                "Azure realtime bootstrap request unavailable",
                extra={
                    "event": "realtime_bootstrap_unavailable",
                    "user_id": str(self.user_ids.user_id),
                    "organisation_id": str(self.user_ids.organisation_id),
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "model": deployment,
                    "voice": voice,
                    "conversation_mode": session_request.conversation_mode.value,
                },
            )
            raise RealtimeSessionServiceError(
                RealtimeErrors.SESSION_BOOTSTRAP_UNAVAILABLE, 503
            ) from exc

        duration_ms = round((perf_counter() - start) * 1000)

        try:
            body = azure_response.json()
        except ValueError as exc:
            logger.warning(
                "Azure realtime bootstrap returned non-JSON response",
                extra={
                    "event": "realtime_bootstrap_invalid_json",
                    "user_id": str(self.user_ids.user_id),
                    "organisation_id": str(self.user_ids.organisation_id),
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                },
            )
            raise RealtimeSessionServiceError(RealtimeErrors.INVALID_SESSION_RESPONSE, 502) from exc

        ephemeral_key = body.get("value")
        expires_at_raw = body.get("expires_at")
        azure_session = body.get("session") or {}

        if not ephemeral_key or not expires_at_raw:
            logger.warning(
                "Azure realtime bootstrap returned incomplete payload",
                extra={
                    "event": "realtime_bootstrap_invalid_payload",
                    "user_id": str(self.user_ids.user_id),
                    "organisation_id": str(self.user_ids.organisation_id),
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "has_ephemeral_key": bool(ephemeral_key),
                    "has_expires_at": bool(expires_at_raw),
                },
            )
            raise RealtimeSessionServiceError(RealtimeErrors.INVALID_SESSION_RESPONSE, 502)

        expires_at = _coerce_expires_at(expires_at_raw)
        session_id = str(azure_session.get("id") or request_id)
        runtime_config_secret = await _get_runtime_config_secret()
        chat_session_id = await self._resolve_chat_session_id(
            session_request=session_request,
            realtime_session_id=session_id,
            deployment=deployment,
            voice=voice,
            db=db,
        )
        async with master_async_session_scope() as master_session:
            org_name, organisation_profile = await load_organisation_runtime_context(
                master_session, self.user_ids.organisation_id
            )
        runtime_config = _build_runtime_config(
            session_id=session_id,
            chat_session_id=chat_session_id,
            voice=voice,
            expires_at=expires_at,
            user_ids=self.user_ids,
            secret=runtime_config_secret,
            org_name=org_name or "Our Organization",
            organisation_profile=organisation_profile,
            conversation_mode=session_request.conversation_mode,
        )
        turn_mode = _resolve_turn_mode(session_request.conversation_mode)

        logger.info(
            "Realtime session bootstrap succeeded",
            extra={
                "event": "realtime_bootstrap_created",
                "user_id": str(self.user_ids.user_id),
                "organisation_id": str(self.user_ids.organisation_id),
                "request_id": request_id,
                "session_id": session_id,
                "chat_session_id": chat_session_id,
                "duration_ms": duration_ms,
                "model": deployment,
                "voice": voice,
                "device_platform": session_request.device.platform.value,
                "app_version": session_request.device.app_version,
            },
        )

        return UserRealtimeSessionResponse(
            session_id=session_id,
            ephemeral_key=str(ephemeral_key),
            endpoint=f"{endpoint}/openai/v1/realtime/calls",
            model=deployment,
            voice=voice,
            expires_at=expires_at,
            turn_mode=turn_mode,
            chat_session_id=chat_session_id,
            runtime_config=runtime_config,
        )

    async def attach_call(
        self,
        attach_request: UserRealtimeCallAttachRequest,
        db: AsyncSession | None = None,
    ) -> UserRealtimeCallAttachResponse:
        if attach_request.runtime_config.chat_session_id != attach_request.chat_session_id:
            raise RealtimeSessionServiceError(RealtimeErrors.INVALID_RUNTIME_CONFIG, 400)

        if attach_request.runtime_config.control_mode != RealtimeControlMode.BACKEND_SIDEBAND:
            raise RealtimeSessionServiceError(RealtimeErrors.INVALID_RUNTIME_CONFIG, 400)

        if attach_request.runtime_config.expires_at <= datetime.now(UTC):
            raise RealtimeSessionServiceError(RealtimeErrors.EXPIRED_RUNTIME_CONFIG, 400)

        if not verify_runtime_config_signature(
            attach_request.runtime_config,
            user_ids=self.user_ids,
            session_id=attach_request.session_id,
            chat_session_id=attach_request.chat_session_id,
            secret=await _get_runtime_config_secret(),
        ):
            raise RealtimeSessionServiceError(RealtimeErrors.INVALID_RUNTIME_CONFIG, 400)

        chat_session = await self._load_owned_voice_chat_session(
            chat_session_id=attach_request.chat_session_id,
            db=db,
        )
        if chat_session is None:
            raise RealtimeSessionServiceError(RealtimeErrors.VOICE_SESSION_NOT_FOUND, 404)

        lock_key = f"voice_realtime:call:{attach_request.call_id}"
        lock_owner = attach_request.chat_session_id
        redis_client = get_redis_client()
        acquired = redis_client.set(lock_key, lock_owner, nx=True, ex=120)
        if not acquired:
            existing_owner = redis_client.get(lock_key)
            if existing_owner != lock_owner:
                raise RealtimeSessionServiceError(RealtimeErrors.CALL_ALREADY_ATTACHED, 409)

        await self._store_call_attachment(
            chat_session=chat_session,
            session_id=attach_request.session_id,
            call_id=attach_request.call_id,
            status="queued",
            db=db,
        )

        voice_job = await self._create_voice_job(
            chat_session_id=attach_request.chat_session_id,
            session_id=attach_request.session_id,
            call_id=attach_request.call_id,
            db=db,
        )

        if not openai_config.AZURE_OPENAI_ENDPOINT or not openai_config.AZURE_OPENAI_API_KEY:
            raise RealtimeSessionServiceError(RealtimeErrors.REALTIME_NOT_CONFIGURED, 500)

        try:
            await send_task_async(
                VOICE_REALTIME_TASK_NAME,
                args=[
                    str(self.user_ids.organisation_id),
                    str(self.user_ids.user_id),
                    self.user_ids.user_email,
                    attach_request.chat_session_id,
                    attach_request.session_id,
                    attach_request.call_id,
                    attach_request.runtime_config.model_dump(mode="json"),
                    str(voice_job.job_id),
                    self.user_ids.user_timezone,
                ],
                queue=VOICE_REALTIME_QUEUE,
            )
        except Exception as exc:
            if redis_client.get(lock_key) == lock_owner:
                redis_client.delete(lock_key)
            await self._update_voice_job(
                job_id=voice_job.job_id,
                status=JobStatus.FAILED,
                error_message=str(exc),
                metadata_updates={"voice_sideband_status": "failed"},
                db=db,
            )
            await self._store_call_attachment(
                chat_session=chat_session,
                session_id=attach_request.session_id,
                call_id=attach_request.call_id,
                status="failed",
                error=str(exc),
                db=db,
            )
            raise RealtimeSessionServiceError(RealtimeErrors.CALL_ATTACH_FAILED, 502) from exc

        return UserRealtimeCallAttachResponse(
            call_id=attach_request.call_id,
            chat_session_id=attach_request.chat_session_id,
            control_mode=RealtimeControlMode.BACKEND_SIDEBAND,
            status="queued",
        )

    async def _resolve_chat_session_id(
        self,
        *,
        session_request: UserRealtimeSessionRequest,
        realtime_session_id: str,
        deployment: str,
        voice: str,
        db: AsyncSession | None,
    ) -> str:
        if db is not None:
            return await self._upsert_chat_session(
                db,
                session_request=session_request,
                realtime_session_id=realtime_session_id,
                deployment=deployment,
                voice=voice,
            )

        async for internal_db in acquire_org_async_session(self.user_ids.organisation_id):
            return await self._upsert_chat_session(
                internal_db,
                session_request=session_request,
                realtime_session_id=realtime_session_id,
                deployment=deployment,
                voice=voice,
            )

        return str(uuid4())

    async def _upsert_chat_session(
        self,
        db: AsyncSession,
        *,
        session_request: UserRealtimeSessionRequest,
        realtime_session_id: str,
        deployment: str,
        voice: str,
    ) -> str:
        if session_request.chat_session_id:
            try:
                requested_id = UUID(session_request.chat_session_id)
            except ValueError as exc:
                raise RealtimeSessionServiceError(
                    RealtimeErrors.VOICE_SESSION_NOT_FOUND, 404
                ) from exc

            result = await db.execute(
                select(ChatSession).where(
                    col(ChatSession.session_id) == requested_id,
                    col(ChatSession.user_id) == self.user_ids.user_id,
                    col(ChatSession.organisation_id) == self.user_ids.organisation_id,
                    col(ChatSession.chat_type) == ChatType.VOICE_CHAT,
                    col(ChatSession.is_deleted).is_(False),
                )
            )
            chat_session = result.scalars().one_or_none()
            if chat_session is None:
                raise RealtimeSessionServiceError(RealtimeErrors.VOICE_SESSION_NOT_FOUND, 404)

            properties = dict(chat_session.properties or {})
            properties.update(
                {
                    "realtime_session_id": realtime_session_id,
                    "device_platform": session_request.device.platform.value,
                    "model": deployment,
                    "voice": voice,
                }
            )
            chat_session.properties = properties
            db.add(chat_session)
            await db.commit()
            return str(chat_session.session_id)

        chat_session = ChatSession(
            user_id=self.user_ids.user_id,
            organisation_id=self.user_ids.organisation_id,
            title="Voice Session",
            chat_type=ChatType.VOICE_CHAT,
            properties={
                "realtime_session_id": realtime_session_id,
                "device_platform": session_request.device.platform.value,
                "model": deployment,
                "voice": voice,
            },
        )
        db.add(chat_session)
        await db.flush()
        chat_session_id = str(chat_session.session_id)
        await db.commit()
        return chat_session_id

    async def _load_owned_voice_chat_session(
        self,
        *,
        chat_session_id: str,
        db: AsyncSession | None,
    ) -> ChatSession | None:
        try:
            requested_id = UUID(chat_session_id)
        except ValueError as exc:
            raise RealtimeSessionServiceError(RealtimeErrors.VOICE_SESSION_NOT_FOUND, 404) from exc

        if db is not None:
            return await self._select_owned_voice_chat_session(db, requested_id)

        async for internal_db in acquire_org_async_session(self.user_ids.organisation_id):
            return await self._select_owned_voice_chat_session(internal_db, requested_id)

        return None

    async def _select_owned_voice_chat_session(
        self,
        db: AsyncSession,
        requested_id: UUID,
    ) -> ChatSession | None:
        result = await db.execute(
            select(ChatSession).where(
                col(ChatSession.session_id) == requested_id,
                col(ChatSession.user_id) == self.user_ids.user_id,
                col(ChatSession.organisation_id) == self.user_ids.organisation_id,
                col(ChatSession.chat_type) == ChatType.VOICE_CHAT,
                col(ChatSession.is_deleted).is_(False),
            )
        )
        return result.scalars().one_or_none()

    async def _store_call_attachment(
        self,
        *,
        chat_session: ChatSession,
        session_id: str,
        call_id: str,
        status: str,
        error: str | None = None,
        db: AsyncSession | None,
    ) -> None:
        if db is not None:
            await self._persist_call_attachment(
                db,
                chat_session,
                session_id=session_id,
                call_id=call_id,
                status=status,
                error=error,
            )
            return

        async for internal_db in acquire_org_async_session(self.user_ids.organisation_id):
            refreshed_chat_session = await self._select_owned_voice_chat_session(
                internal_db,
                UUID(str(chat_session.session_id)),
            )
            if refreshed_chat_session is None:
                raise RealtimeSessionServiceError(RealtimeErrors.VOICE_SESSION_NOT_FOUND, 404)
            await self._persist_call_attachment(
                internal_db,
                refreshed_chat_session,
                session_id=session_id,
                call_id=call_id,
                status=status,
                error=error,
            )
            return

    async def _persist_call_attachment(
        self,
        db: AsyncSession,
        chat_session: ChatSession,
        *,
        session_id: str,
        call_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        properties = dict(chat_session.properties or {})
        properties.update(
            {
                "realtime_session_id": session_id,
                "realtime_call_id": call_id,
                "realtime_control_mode": RealtimeControlMode.BACKEND_SIDEBAND.value,
                "voice_sideband_status": status,
            }
        )
        if status == "queued":
            properties.setdefault("voice_sideband_queued_at", datetime.now(UTC).isoformat())
        if error:
            properties["voice_sideband_error"] = error
        elif status != "failed":
            properties.pop("voice_sideband_error", None)
        chat_session.properties = properties
        db.add(chat_session)
        await db.commit()

    async def _create_voice_job(
        self,
        *,
        chat_session_id: str,
        session_id: str,
        call_id: str,
        db: AsyncSession | None,
    ) -> ChatMessageJob:
        if db is not None:
            return await self._persist_voice_job(
                db,
                chat_session_id=chat_session_id,
                session_id=session_id,
                call_id=call_id,
            )

        async for internal_db in acquire_org_async_session(self.user_ids.organisation_id):
            return await self._persist_voice_job(
                internal_db,
                chat_session_id=chat_session_id,
                session_id=session_id,
                call_id=call_id,
            )

        raise RealtimeSessionServiceError(RealtimeErrors.CALL_ATTACH_FAILED, 502)

    async def _persist_voice_job(
        self,
        db: AsyncSession,
        *,
        chat_session_id: str,
        session_id: str,
        call_id: str,
    ) -> ChatMessageJob:
        job = ChatMessageJob(
            message_id=None,
            job_type="voice_realtime_sideband",
            chat_type=ChatType.VOICE_CHAT.value,
            status=JobStatus.PENDING,
            user_id=self.user_ids.user_id,
            job_metadata={
                "chat_session_id": chat_session_id,
                "realtime_session_id": session_id,
                "realtime_call_id": call_id,
                "runtime_control_mode": RealtimeControlMode.BACKEND_SIDEBAND.value,
                "voice_sideband_status": "queued",
            },
        )
        db.add(job)
        await db.flush()
        await db.commit()
        return job

    async def _update_voice_job(
        self,
        *,
        job_id: UUID,
        status: JobStatus,
        error_message: str | None = None,
        metadata_updates: dict[str, object] | None = None,
        db: AsyncSession | None,
    ) -> None:
        if db is not None:
            await self._persist_voice_job_update(
                db,
                job_id=job_id,
                status=status,
                error_message=error_message,
                metadata_updates=metadata_updates,
            )
            return

        async for internal_db in acquire_org_async_session(self.user_ids.organisation_id):
            await self._persist_voice_job_update(
                internal_db,
                job_id=job_id,
                status=status,
                error_message=error_message,
                metadata_updates=metadata_updates,
            )
            return

    async def _persist_voice_job_update(
        self,
        db: AsyncSession,
        *,
        job_id: UUID,
        status: JobStatus,
        error_message: str | None = None,
        metadata_updates: dict[str, object] | None = None,
    ) -> None:
        job = await db.get(ChatMessageJob, job_id)
        if job is None:
            return

        job.status = status
        metadata = dict(job.job_metadata or {})
        if metadata_updates:
            metadata.update(metadata_updates)
        job.job_metadata = metadata
        job.error_message = error_message
        db.add(job)
        await db.commit()


def _coerce_expires_at(value: int | float | str) -> datetime:
    """Normalize Azure expiry values into timezone-aware UTC datetimes."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)

    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise RealtimeSessionServiceError(RealtimeErrors.INVALID_SESSION_RESPONSE, 502) from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    raise RealtimeSessionServiceError(RealtimeErrors.INVALID_SESSION_RESPONSE, 502)


async def _get_runtime_config_secret() -> str:
    try:
        return await security_secrets_config.get_voice_realtime_config_secret()
    except RuntimeError as exc:
        logger.error(
            "Realtime runtime signing secret unavailable",
            extra={
                "event": "realtime_runtime_config_secret_missing",
                "reason": str(exc),
            },
        )
        raise RealtimeSessionServiceError(RealtimeErrors.REALTIME_NOT_CONFIGURED, 500) from exc


def _get_voice_allowed_tools() -> list[str]:
    return build_voice_realtime_allowed_tools()


def _get_voice_tool_schemas() -> list[dict[str, object]]:
    return build_voice_realtime_tool_schemas()


def _runtime_config_payload(
    *,
    session_id: str,
    chat_session_id: str,
    user_ids: UserIDs,
    control_mode: RealtimeControlMode,
    tool_policy: RealtimeToolPolicy,
    allowed_tools: list[str],
    session_update: dict[str, object],
    organisation_context_item: dict[str, object] | None,
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "chat_session_id": chat_session_id,
        "user_id": str(user_ids.user_id),
        "organisation_id": str(user_ids.organisation_id),
        "control_mode": control_mode.value,
        "tool_policy": tool_policy.value,
        "allowed_tools": allowed_tools,
        "session_update": session_update,
        "organisation_context_item": organisation_context_item,
        "issued_at": issued_at.astimezone(UTC).isoformat(),
        "expires_at": expires_at.astimezone(UTC).isoformat(),
    }


def _sign_runtime_config_payload(payload: dict[str, object], *, secret: str) -> str:
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _build_runtime_config(
    *,
    session_id: str,
    chat_session_id: str,
    voice: str,
    expires_at: datetime,
    user_ids: UserIDs,
    secret: str,
    org_name: str,
    conversation_mode: RealtimeConversationMode,
    organisation_profile: str | None = None,
) -> UserRealtimeRuntimeConfig:
    issued_at = datetime.now(UTC)
    control_mode = RealtimeControlMode.BACKEND_SIDEBAND
    tool_policy = RealtimeToolPolicy.GUARDED_AUTO
    allowed_tools = _get_voice_allowed_tools()
    tool_schemas = _get_voice_tool_schemas()
    turn_detection = _build_turn_detection(conversation_mode)
    session_update: dict[str, object] = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": with_organisation_context_policy(
                get_voice_realtime_system_prompt(org_name)
            ),
            "tools": tool_schemas,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": turn_detection,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                },
            },
        },
    }
    organisation_context_item: dict[str, object] | None = None
    if organisation_profile:
        organisation_context_item = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": organisation_profile}],
            },
        }
    payload = _runtime_config_payload(
        session_id=session_id,
        chat_session_id=chat_session_id,
        user_ids=user_ids,
        control_mode=control_mode,
        tool_policy=tool_policy,
        allowed_tools=allowed_tools,
        session_update=session_update,
        organisation_context_item=organisation_context_item,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    signature = _sign_runtime_config_payload(payload, secret=secret)
    return UserRealtimeRuntimeConfig(
        chat_session_id=chat_session_id,
        control_mode=control_mode,
        tool_policy=tool_policy,
        allowed_tools=allowed_tools,
        session_update=session_update,
        organisation_context_item=organisation_context_item,
        issued_at=issued_at,
        expires_at=expires_at,
        signature=signature,
    )


def _resolve_turn_mode(conversation_mode: RealtimeConversationMode) -> RealtimeTurnMode:
    if conversation_mode == RealtimeConversationMode.REALTIME:
        return RealtimeTurnMode.SERVER_VAD
    return RealtimeTurnMode.PUSH_TO_TALK


def _build_turn_detection(
    conversation_mode: RealtimeConversationMode,
) -> dict[str, object] | None:
    if conversation_mode == RealtimeConversationMode.REALTIME:
        return {
            "type": "server_vad",
            "silence_duration_ms": 700,
            "prefix_padding_ms": 300,
        }
    return None


def verify_runtime_config_signature(
    runtime_config: UserRealtimeRuntimeConfig,
    *,
    user_ids: UserIDs,
    session_id: str,
    chat_session_id: str,
    secret: str,
) -> bool:
    payload = _runtime_config_payload(
        session_id=session_id,
        chat_session_id=chat_session_id,
        user_ids=user_ids,
        control_mode=runtime_config.control_mode,
        tool_policy=runtime_config.tool_policy,
        allowed_tools=list(runtime_config.allowed_tools),
        session_update=runtime_config.session_update,
        organisation_context_item=runtime_config.organisation_context_item,
        issued_at=runtime_config.issued_at,
        expires_at=runtime_config.expires_at,
    )
    expected_signature = _sign_runtime_config_payload(payload, secret=secret)
    return hmac.compare_digest(expected_signature, runtime_config.signature)


__all__ = [
    "RealtimeSessionService",
    "RealtimeSessionServiceError",
    "verify_runtime_config_signature",
    "_build_runtime_config",
]
