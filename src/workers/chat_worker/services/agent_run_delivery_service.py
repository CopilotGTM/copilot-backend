"""Deliver one persisted agent result without rerunning the agent or its tools.

The worker reads the immutable run and trigger snapshots, claims one tenant
delivery row, renders a sanitized email, and records the provider outcome. The
claim transaction ends before the slow provider call; a token-fenced update and
the recovery sweep make duplicate or lost broker messages safe.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from html import escape
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import markdown
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.master_tables import User
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import SupabaseUserStatus
from src.domain.models.organisation_tables import AgentRun
from src.domain.models.organisation_tables.sync import NotificationDelivery
from src.infra.auth.email_service import PlatformEmailSendError, email_service
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.observability.metrics import record_agent_run_delivery
from src.infra.utils.logger import get_logger
from src.service.automation.runs.delivery_access import AgentRunDeliveryAccess

logger = get_logger("agent_run_delivery")

_LEASE = timedelta(minutes=10)
# Bound email size so a large model response cannot exhaust provider or worker
# memory; the full result remains available through the run URL.
_MAX_EMAIL_OUTPUT_CHARACTERS = 100_000
_TRUNCATED_OUTPUT_CHARACTERS = 10_000
# Retry backoff is capped at five minutes because the scheduled recovery sweep
# is the long-lived safety net, not an unbounded Celery countdown.
_MAX_RETRY_DELAY_SECONDS = 300
_SAFE_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_VOID_TAGS = {"br", "hr"}

OrgSessionScope = Callable[[UUID], AsyncIterator[AsyncSession]]
MasterSessionScope = Callable[[], AsyncIterator[AsyncSession]]


class DeliveryPreparationError(ValueError):
    """Safe terminal error detected before the provider boundary."""

    def __init__(self, code: str, message: str, *, status: str = "FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AgentRunDeliveryExecutionService:
    """Send the persisted Markdown result through the platform email account."""

    def __init__(
        self,
        organisation_id: UUID,
        *,
        session_scope: OrgSessionScope = acquire_org_async_session,
        master_session_scope: MasterSessionScope = acquire_master_async_session,
        access_factory: Callable[[AsyncSession, UUID], Any] = AgentRunDeliveryAccess,
        platform_email_service: Any = email_service,
    ) -> None:
        self.organisation_id = organisation_id
        self.session_scope = session_scope
        self.master_session_scope = master_session_scope
        self.access_factory = access_factory
        self.email_service = platform_email_service

    async def execute(
        self,
        delivery_id: UUID,
        *,
        worker_id: str,
        retry_allowed: bool,
    ) -> dict[str, Any]:
        """Claim, send, and finalize one delivery without rerunning its source run.

        Safe provider failures return a retry result when Celery may retry; an
        uncertain provider outcome becomes ``NEEDS_ATTENTION`` so the same email
        is not sent again blindly.
        """

        claimed = await self._claim(delivery_id, worker_id)
        if claimed is None:
            record_agent_run_delivery("skipped")
            return {"status": "skipped", "delivery_id": str(delivery_id)}
        delivery, run = claimed
        if delivery.claim_token is None:
            raise RuntimeError("claimed delivery has no fence token")

        try:
            message = await self._message(delivery, run)
        except DeliveryPreparationError as exc:
            await self._mark_terminal(delivery, exc.status, exc.code, exc.message)
            outcome = exc.status.lower()
            _record_outcome(delivery, run, outcome, exc.code)
            return {"status": outcome, "run_id": str(run.run_id)}

        # _claim committed and left its tenant session scope before this provider
        # call; the persisted token now fences any stale worker that races it.
        try:
            provider_id = await self.email_service.send_email_with_receipt(**message)
        except PlatformEmailSendError as exc:
            logger.error(
                "Agent output email provider request failed org=%s run=%s delivery=%s",
                self.organisation_id,
                run.run_id,
                delivery.delivery_id,
            )
            if exc.safe_to_retry and retry_allowed:
                async for session in self.session_scope(self.organisation_id):
                    updated = await self.access_factory(
                        session, self.organisation_id
                    ).release_for_retry(
                        delivery.delivery_id,
                        claim_token=delivery.claim_token,
                        next_attempt_at=datetime.now(UTC)
                        + timedelta(seconds=_delivery_retry_delay_seconds(delivery.attempt_count)),
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                    await session.commit()
                    if not updated:
                        return _superseded_result(delivery, run)
                    _record_outcome(delivery, run, "retry", exc.code)
                    return {"status": "retry", "run_id": str(run.run_id)}
            # Resend may have accepted the message even when the response was
            # lost, so unknown outcomes must stop rather than create a duplicate.
            if not exc.safe_to_retry:
                await self._mark_terminal(
                    delivery,
                    "NEEDS_ATTENTION",
                    exc.code,
                    exc.message,
                )
                _record_outcome(delivery, run, "needs_attention", exc.code)
                return {"status": "needs_attention", "run_id": str(run.run_id)}
            await self._mark_terminal(
                delivery,
                "NEEDS_ATTENTION",
                exc.code,
                exc.message,
            )
            _record_outcome(delivery, run, "needs_attention", exc.code)
            return {"status": "needs_attention", "run_id": str(run.run_id)}
        except Exception:
            await self._mark_terminal(
                delivery,
                "NEEDS_ATTENTION",
                "delivery_outcome_unknown",
                "Provider outcome is unknown",
            )
            _record_outcome(
                delivery,
                run,
                "needs_attention",
                "delivery_outcome_unknown",
            )
            return {"status": "needs_attention", "run_id": str(run.run_id)}

        # Record the provider result only after the external call has returned;
        # the claim token ensures a reclaimed worker cannot overwrite this state.
        async for session in self.session_scope(self.organisation_id):
            updated = await self.access_factory(session, self.organisation_id).mark_sent(
                delivery.delivery_id,
                claim_token=delivery.claim_token,
                provider_resource_id=provider_id,
                sent_at=datetime.now(UTC),
            )
            await session.commit()
            if not updated:
                return _superseded_result(delivery, run)
            _record_outcome(delivery, run, "sent")
            return {"status": "sent", "run_id": str(run.run_id)}
        raise RuntimeError("organization session scope yielded no session")

    async def _claim(
        self,
        delivery_id: UUID,
        worker_id: str,
    ) -> tuple[NotificationDelivery, AgentRun] | None:
        """Commit ownership before any rendering or provider work begins."""

        worker = worker_id.strip()
        if not worker:
            raise ValueError("worker_id must be a non-empty string")
        now = datetime.now(UTC)
        async for session in self.session_scope(self.organisation_id):
            claimed = await self.access_factory(session, self.organisation_id).claim(
                delivery_id,
                worker_id=worker,
                claimed_at=now,
                stale_before=now - _LEASE,
            )
            if claimed is None:
                return None
            # Release the claim transaction before rendering and calling Resend;
            # only the durable claim token must survive the slow external work.
            await session.commit()
            return claimed
        raise RuntimeError("organization session scope yielded no session")

    async def _message(
        self,
        delivery: NotificationDelivery,
        run: AgentRun,
    ) -> dict[str, str]:
        """Render a platform email solely from the persisted run snapshots."""

        delivery_channels = run.output_delivery_config.get("channels")
        if not isinstance(delivery_channels, list) or "email_notification" not in delivery_channels:
            raise DeliveryPreparationError(
                "invalid_delivery_config",
                "Pinned run does not enable platform email notification",
            )
        recipient = await self._recipient(delivery.target_key)
        if recipient is None:
            raise DeliveryPreparationError(
                "recipient_unavailable",
                "The notification recipient is no longer an active workspace user",
                status="SKIPPED",
            )

        base_url = str(getattr(self.email_service, "app_url", "")).rstrip("/")
        base_url = base_url or "https://app.copilotgtm.com"
        run_url = f"{base_url}/agents-v1/{run.agent_id}?run_id={run.run_id}"
        output_markdown = _canonical_output_markdown(run, run_url)
        agent_name = _safe_label(run.agent_snapshot.get("name"), "Agent")
        trigger_name = _safe_label(run.trigger_snapshot.get("name"), run.event_key)
        recipient_name = _recipient_name(recipient)
        trigger_reason = _event_reason(run.event_key)
        subject_context = _subject_context(run.input_snapshot)
        completed = run.completed_at.isoformat() if run.completed_at else "Completed"
        subject = (
            f"{getattr(self.email_service, 'app_name', 'CopilotGTM')} — {agent_name} completed"
        )
        subject = " ".join(subject.split())[:998]
        context_html = (
            f"<p>Hi {escape(recipient_name)},</p>"
            f"<h2>{escape(agent_name)} completed</h2>"
            f"<p><strong>Trigger:</strong> {escape(trigger_name)}</p>"
            f"<p><strong>Reason:</strong> {escape(trigger_reason)}</p>"
            + (
                f"<p><strong>Subject:</strong> {escape(subject_context)}</p>"
                if subject_context
                else ""
            )
            + (
                f"<p><strong>Completed:</strong> {escape(completed)}</p>"
                f'<p><a href="{escape(run_url, quote=True)}">View full result</a></p>'
                "<hr>"
                f"{_safe_markdown_html(output_markdown)}"
            )
        )
        html_content = self.email_service._create_styled_html(
            subject,
            f"{agent_name} completed after {trigger_name}.",
            context_html,
        )
        text_content = (
            f"Hi {recipient_name},\n\n"
            f"{agent_name} completed\n"
            f"Trigger: {trigger_name}\n"
            f"Reason: {trigger_reason}\n"
            + (f"Subject: {subject_context}\n" if subject_context else "")
            + (f"Completed: {completed}\nView full result: {run_url}\n\n{output_markdown}")
        )
        return {
            "to_email": str(recipient.email),
            "subject": subject,
            "html_content": html_content,
            "text_content": text_content,
            "idempotency_key": f"agent-output:{delivery.delivery_id}",
        }

    async def _recipient(self, target_key: str) -> User | None:
        try:
            user_id = UUID(target_key)
        except ValueError as exc:
            raise DeliveryPreparationError(
                "invalid_recipient",
                "The notification recipient identity is invalid",
            ) from exc
        user_model = cast(Any, User)
        async for session in self.master_session_scope():
            # Organization membership plus active, approved, and verified state
            # is the authorization boundary; a queued target is not proof of access.
            return (
                await session.execute(
                    select(User).where(
                        user_model.user_id == user_id,
                        user_model.organisation_id == self.organisation_id,
                        user_model.status == SupabaseUserStatus.ACTIVE,
                        user_model.admin_status == AdminUserStatus.APPROVED,
                        user_model.email_verified.is_(True),
                    )
                )
            ).scalar_one_or_none()
        raise RuntimeError("master session scope yielded no session")

    async def _mark_terminal(
        self,
        delivery: NotificationDelivery,
        status: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Persist a deliberate terminal delivery result under the claim fence."""

        async for session in self.session_scope(self.organisation_id):
            updated = await self.access_factory(session, self.organisation_id).mark_terminal(
                delivery.delivery_id,
                claim_token=delivery.claim_token,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
            await session.commit()
            if not updated:
                raise RuntimeError("delivery claim was superseded")
            return
        raise RuntimeError("organization session scope yielded no session")


def _canonical_output_markdown(run: AgentRun, run_url: str) -> str:
    """Choose text or deterministic JSON output and shorten only for email."""

    if run.output_text and run.output_text.strip():
        result = run.output_text.strip()
    elif run.output_json is not None:
        result = (
            "```json\n"
            + json.dumps(
                run.output_json,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n```"
        )
    else:
        raise DeliveryPreparationError("empty_output", "Agent output is empty")
    if len(result) <= _MAX_EMAIL_OUTPUT_CHARACTERS:
        return result
    return (
        result[:_TRUNCATED_OUTPUT_CHARACTERS]
        + "\n\n> Output shortened for email. "
        + f"[Open the full run in CopilotGTM]({run_url})."
    )


class _SafeHTML(HTMLParser):
    """Keep markdown output to a small allowlist after HTML escaping."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SAFE_TAGS:
            return
        rendered_attrs: list[str] = []
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value and _safe_link(value):
                    rendered_attrs.append(f' href="{escape(value, quote=True)}"')
                elif name == "title" and value:
                    rendered_attrs.append(f' title="{escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(rendered_attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SAFE_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.parts.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


def _safe_markdown_html(value: str) -> str:
    # Escape model output before Markdown conversion, then allow only the tags
    # and link schemes needed by the email template.
    generated = markdown.markdown(
        escape(value, quote=False),
        extensions=["fenced_code", "sane_lists", "tables"],
    )
    sanitizer = _SafeHTML()
    sanitizer.feed(generated)
    sanitizer.close()
    return "".join(sanitizer.parts)


def _safe_link(value: str) -> bool:
    return urlsplit(value.strip()).scheme.lower() in {"http", "https", "mailto"}


def _safe_label(value: object, fallback: str) -> str:
    label = str(value).strip() if value is not None else ""
    return " ".join(label.split())[:255] or fallback


def _recipient_name(recipient: User) -> str:
    return _safe_label(
        " ".join(
            part.strip()
            for part in (
                str(getattr(recipient, "first_name", "") or ""),
                str(getattr(recipient, "last_name", "") or ""),
            )
            if part.strip()
        ),
        "there",
    )


def _event_reason(event_key: str) -> str:
    words = event_key.replace(".", " ").replace("_", " ").split()
    return " ".join(
        word.upper() if word in {"ai", "crm"} else word.title() if index == 0 else word.lower()
        for index, word in enumerate(words)
    )


def _subject_context(input_snapshot: dict[str, Any]) -> str | None:
    subject = input_snapshot.get("subject")
    record = input_snapshot.get("record")
    if not isinstance(subject, dict):
        return None
    subject_type = subject.get("type")
    if not isinstance(subject_type, str) or not subject_type.strip():
        return None
    type_label = _event_reason(subject_type)
    if not isinstance(record, dict):
        return type_label
    label = next(
        (
            _safe_label(value, "")
            for key in ("name", "full_name", "title")
            if isinstance(value := record.get(key), str) and value.strip()
        ),
        "",
    )
    if not label:
        label = _safe_label(
            " ".join(
                str(record.get(key) or "").strip()
                for key in ("first_name", "last_name")
                if str(record.get(key) or "").strip()
            ),
            "",
        )
    return f"{type_label} · {label}" if label else type_label


def _record_outcome(
    delivery: NotificationDelivery,
    run: AgentRun,
    status: str,
    error_code: str | None = None,
) -> None:
    logger.info(
        "Agent output delivery outcome org=%s run=%s delivery=%s channel=%s status=%s error=%s attempts=%d",
        delivery.organisation_id,
        run.run_id,
        delivery.delivery_id,
        getattr(delivery.channel, "value", delivery.channel),
        status,
        error_code or "none",
        delivery.attempt_count,
    )
    record_agent_run_delivery(
        status,
        provider="resend",
        reason=error_code,
        attempts=delivery.attempt_count,
        latency_ms=_delivery_latency_ms(run),
    )


def _superseded_result(
    delivery: NotificationDelivery,
    run: AgentRun,
) -> dict[str, str]:
    _record_outcome(delivery, run, "skipped", "superseded")
    return {"status": "superseded", "run_id": str(run.run_id)}


def _delivery_retry_delay_seconds(attempt_number: int) -> int:
    """Return bounded exponential backoff with small jitter for one attempt."""

    if attempt_number < 1:
        raise ValueError("attempt_number must be positive")
    exponent = min(attempt_number - 1, 4)
    base = min(30 * (2**exponent), _MAX_RETRY_DELAY_SECONDS)
    jitter = secrets.randbelow(max(1, base // 4) + 1)
    return min(base + jitter, _MAX_RETRY_DELAY_SECONDS)


def _delivery_latency_ms(run: AgentRun) -> int | None:
    """Measure from committed run completion to this delivery outcome."""

    if run.completed_at is None:
        return None
    completed_at = run.completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - completed_at).total_seconds() * 1000))


__all__ = ["AgentRunDeliveryExecutionService"]
