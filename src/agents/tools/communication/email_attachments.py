"""Authorized listing and reading of Gmail and Outlook email attachments.

`get_email_details` uses the listing function to expose message-owned attachment
handles. The General Agent calls `read_email_attachment` for one handle at a
time. Provider calls happen only after the org-scoped database session is
released, so a slow mailbox cannot pin a pooled connection.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx
from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult as AgnoToolResult
from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.communication.mailbox_emails import _resolve_readable_mailbox
from src.agents.tools.crm.access import AgentCRMAccessError, require_agent_deal_access
from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetSourceType,
    DealAssetStatus,
)
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation, Email
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.configs import chat_file_upload_config
from src.infra.media.audio_transcription import transcribe_audio
from src.infra.media.chat_attachment_text import (
    extract_text_from_bytes,
    is_audio_mimetype,
    is_image_mimetype,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.service.integrations.email.attachment_provider import (
    AttachmentMeta,
    DownloadedAttachment,
    attachment_filter_reason,
    build_attachment_source_record_id,
    download_gmail_attachment,
    download_outlook_attachment,
    list_gmail_attachments,
    list_outlook_attachments,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)
from src.workers.integrations_worker.providers._google_api import GMAIL_BASE_URL

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_GMAIL_TYPES = {IntegrationType.GMAIL, IntegrationType.GOOGLE}
_OUTLOOK_TYPES = {IntegrationType.OUTLOOK_EMAIL, IntegrationType.MICROSOFT}


@dataclass(slots=True, frozen=True)
class ReadableEmailSource:
    """Email and provider coordinates captured after one authorized lookup."""

    email_id: UUID
    external_message_id: str
    internet_message_id: str
    integration_type: IntegrationType | None
    token_info: IntegrationTokenInfo | None
    email: Email | None = None
    access_scope: str = "mailbox"
    scope_id: UUID | None = None
    deal_id: UUID | None = None


@dataclass(slots=True, frozen=True)
class CachedEmailAttachment:
    """Metadata and bytes recovered from an existing DealAsset blob."""

    attachment: AttachmentMeta
    downloaded: DownloadedAttachment


def _error(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message}


def _validate_scope(
    *,
    mailbox_email: str | None,
    deal_id: str | None,
) -> dict[str, Any] | None:
    if mailbox_email and deal_id:
        return _error(
            "email_scope_conflict",
            "Use either mailbox_email or deal_id when reading an email attachment.",
        )
    if not mailbox_email and not deal_id:
        return _error(
            "email_scope_required",
            "A connected mailbox or authorized deal is required.",
        )
    return None


async def resolve_readable_email_source(
    *,
    context: AgentContext,
    internet_message_id: str,
    mailbox_email: str | None,
    deal_id: str | None,
    require_provider: bool = True,
) -> ReadableEmailSource | None:
    """Resolve one email inside the existing mailbox-or-deal access boundary.

    The RFC Message-ID and provider IDs are identifiers only. Organisation
    scope plus mailbox ownership or an active visible-deal association grants
    access; callers must not weaken this query to trust a provider handle.
    """
    organisation_id = context.organisation_id
    if not organisation_id or not internet_message_id:
        return None

    async with context.acquire_session() as session:
        email_c = cast(Any, Email).__table__.c
        assoc_c = cast(Any, DealEmailAssociation).__table__.c
        integration_c = cast(Any, KnowledgeIntegration).__table__.c
        integration: KnowledgeIntegration | None = None
        parsed_deal_id: UUID | None = None

        if mailbox_email:
            integration = await _resolve_readable_mailbox(
                context=context,
                mailbox_email=mailbox_email,
                session=session,
            )
            if integration is None:
                return None
        else:
            try:
                parsed_deal_id = UUID(cast(str, deal_id))
                await require_agent_deal_access(context, session, parsed_deal_id)
            except (ValueError, AgentCRMAccessError):
                return None

        email_stmt = (
            select(Email)
            .where(
                email_c.organisation_id == organisation_id,
                email_c.internet_message_id == internet_message_id,
                email_c.is_deleted.is_(False),
            )
            .order_by(email_c.received_at.desc(), email_c.email_id.desc())
            .limit(1)
        )
        if integration is not None:
            email_stmt = email_stmt.where(email_c.integration_id == integration.integration_id)
        else:
            email_stmt = email_stmt.join(
                DealEmailAssociation,
                assoc_c.internet_message_id == email_c.internet_message_id,
            ).where(
                assoc_c.organisation_id == organisation_id,
                assoc_c.deal_id == parsed_deal_id,
                assoc_c.status == AssociationStatus.ACTIVE.value,
            )

        email = (await session.execute(email_stmt)).scalars().first()
        if email is None:
            return None

        if integration is None and require_provider:
            # The org filter is the tenant boundary; an integration UUID from an
            # email row is not sufficient authority on its own.
            integration_stmt = select(KnowledgeIntegration).where(
                integration_c.organisation_id == organisation_id,
                integration_c.integration_id == email.integration_id,
                integration_c.is_active.is_(True),
            )
            integration = (await session.execute(integration_stmt)).scalar_one_or_none()
            if integration is None:
                return None

        integration_type: IntegrationType | None = None
        token_info: IntegrationTokenInfo | None = None
        if integration is not None:
            integration_type = integration.integration_type
            if isinstance(integration_type, str):
                integration_type = IntegrationType(integration_type)
            if integration_type not in _GMAIL_TYPES | _OUTLOOK_TYPES:
                return None
            token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)

        source = ReadableEmailSource(
            email_id=email.email_id,
            external_message_id=email.external_message_id,
            internet_message_id=cast(str, email.internet_message_id),
            integration_type=integration_type,
            token_info=token_info,
            email=email,
            access_scope="mailbox" if mailbox_email else "deal",
            scope_id=integration.integration_id
            if mailbox_email and integration
            else parsed_deal_id,
            deal_id=parsed_deal_id,
        )

    # The session is deliberately closed before token refresh or provider I/O.
    return source


async def _list_provider_attachments(source: ReadableEmailSource) -> list[AttachmentMeta]:
    """List attachment handles from the provider message without downloading files."""
    if source.token_info is None or source.integration_type is None:
        raise RuntimeError("Email provider is unavailable")
    token = await get_access_token(source.token_info)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    ) as client:
        if source.integration_type in _GMAIL_TYPES:
            url = (
                f"{GMAIL_BASE_URL}/gmail/v1/users/me/messages/"
                f"{quote(source.external_message_id, safe='')}"
            )
            response = await client.get(url, params={"format": "full"})
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json().get("payload", {}))
            return list_gmail_attachments(payload)
        return await list_outlook_attachments(client, source.external_message_id)


async def _download_provider_attachment(
    source: ReadableEmailSource,
    attachment: AttachmentMeta,
) -> DownloadedAttachment:
    """Download one provider attachment after its message ownership was verified."""
    if source.token_info is None or source.integration_type is None:
        raise RuntimeError("Email provider is unavailable")
    token = await get_access_token(source.token_info)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    ) as client:
        if source.integration_type in _GMAIL_TYPES:
            return await download_gmail_attachment(
                client,
                source.external_message_id,
                attachment.attachment_id,
            )
        return await download_outlook_attachment(
            client,
            source.external_message_id,
            attachment.attachment_id,
        )


async def _read_cached_deal_attachment(
    *,
    context: AgentContext,
    deal_id: UUID | None,
    source: ReadableEmailSource,
    attachment_id: str,
) -> CachedEmailAttachment | None:
    """Read an existing DealAsset blob before making another provider request."""
    if deal_id is None:
        return None

    source_record_id = build_attachment_source_record_id(
        source.internet_message_id,
        attachment_id,
    )
    async with context.acquire_session() as session:
        asset_c = cast(Any, DealAsset).__table__.c
        # All provenance inputs are externally derived, so org and visible deal
        # filters remain part of the cache lookup's authorization boundary.
        stmt = select(DealAsset).where(
            asset_c.organisation_id == context.organisation_id,
            asset_c.deal_id == deal_id,
            asset_c.source_type == DealAssetSourceType.EMAIL_ATTACHMENT.value,
            asset_c.source_record_id == source_record_id,
            asset_c.status.in_([DealAssetStatus.PROCESSING.value, DealAssetStatus.ACTIVE.value]),
            asset_c.is_deleted.is_(False),
            asset_c.storage_path.is_not(None),
        )
        asset = (await session.execute(stmt)).scalars().first()
        storage_path = asset.storage_path if asset else None
        size_bytes = asset.size_bytes if asset else None
        filename = (asset.filename or asset.name) if asset else None
        mimetype = asset.mimetype if asset else None

    if not storage_path:
        return None

    storage = AzureOrganisationBlobStorage(context.organisation_id)
    try:
        content = await storage.get_blob_content(storage_path)
        actual_size = size_bytes if size_bytes is not None else len(content)
        return CachedEmailAttachment(
            attachment=AttachmentMeta(
                filename=filename or "attachment",
                mime_type=mimetype or "application/octet-stream",
                size_bytes=actual_size,
                attachment_id=attachment_id,
            ),
            downloaded=DownloadedAttachment(
                content=content,
                size_bytes=actual_size,
            ),
        )
    finally:
        await storage.close()


def _is_supported_mimetype(mimetype: str) -> bool:
    normalized = (mimetype or "").split(";", maxsplit=1)[0].strip().lower()
    return normalized in {item.lower() for item in chat_file_upload_config.ALLOWED_MIMETYPES}


def _manifest_item(
    attachment: AttachmentMeta,
    *,
    internet_message_id: str,
) -> dict[str, Any]:
    supported = _is_supported_mimetype(attachment.mime_type)
    filter_reason = attachment_filter_reason(attachment)
    readable = supported and filter_reason is None
    reason = None
    if not supported:
        reason = "unsupported_attachment_type"
    elif filter_reason and filter_reason.startswith("oversized"):
        reason = "attachment_too_large"
    elif filter_reason:
        reason = "inline_or_non_document_attachment"
    return {
        "attachment_id": attachment.attachment_id,
        "internet_message_id": internet_message_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "readable": readable,
        "unreadable_reason": reason,
    }


async def _persist_attachment_metadata(
    *,
    context: AgentContext,
    source: ReadableEmailSource,
    attachments: list[dict[str, Any]],
) -> None:
    """Cache immutable provider metadata so later detail reads avoid another listing call."""
    async with context.acquire_session() as session:
        email_c = cast(Any, Email).__table__.c
        stmt = select(Email).where(
            email_c.organisation_id == context.organisation_id,
            email_c.email_id == source.email_id,
            email_c.is_deleted.is_(False),
        )
        email = (await session.execute(stmt)).scalar_one_or_none()
        if email is None:
            return
        email.attachments = attachments
        session.add(email)
        await session.commit()


async def list_email_attachments(
    internet_message_id: str,
    *,
    context: AgentContext,
    mailbox_email: str | None = None,
    deal_id: str | None = None,
) -> dict[str, Any]:
    """List provider file handles for one authorized email message."""
    if scope_error := _validate_scope(mailbox_email=mailbox_email, deal_id=deal_id):
        return scope_error

    source = await resolve_readable_email_source(
        context=context,
        internet_message_id=internet_message_id,
        mailbox_email=mailbox_email,
        deal_id=deal_id,
    )
    if source is None:
        return _error("email_not_found", "The email is unavailable in the requested scope.")

    try:
        provider_attachments = await _list_provider_attachments(source)
    except Exception:
        logger.exception(
            "Failed to list attachments for email %s",
            internet_message_id,
        )
        return _error(
            "attachment_list_failed",
            "The email was found, but its attachment list could not be loaded.",
        )

    manifests = [
        _manifest_item(item, internet_message_id=source.internet_message_id)
        for item in provider_attachments
    ]
    try:
        await _persist_attachment_metadata(
            context=context,
            source=source,
            attachments=manifests,
        )
    except Exception:
        # Provider metadata remains usable for this response even when the
        # optional cache write fails; the next read can list it again.
        logger.warning(
            "Failed to cache attachment metadata for email %s",
            internet_message_id,
            exc_info=True,
        )

    return {
        "status": "success",
        "internet_message_id": source.internet_message_id,
        "attachment_instance_count": len(manifests),
        "readable_attachment_count": sum(bool(item["readable"]) for item in manifests),
        "attachments": manifests,
    }


def _extract_text_chunk(
    content: bytes,
    *,
    mimetype: str,
    filename: str,
    offset: int,
    max_chars: int,
) -> dict[str, Any]:
    """Extract one bounded, non-overlapping text chunk from document bytes."""
    extracted = extract_text_from_bytes(
        content,
        mimetype,
        filename,
        max_chars=offset + max_chars + 1,
    )
    visible = extracted[offset : offset + max_chars]
    truncated = len(extracted) > offset + max_chars
    return {
        "content": visible,
        "offset": offset,
        "returned_characters": len(visible),
        "truncated": truncated,
        "next_offset": offset + len(visible) if truncated else None,
    }


async def read_email_attachment(
    internet_message_id: str,
    attachment_id: str,
    *,
    context: AgentContext,
    mailbox_email: str | None = None,
    deal_id: str | None = None,
    offset: int = 0,
    max_chars: int = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS,
) -> dict[str, Any] | AgnoToolResult:
    """Read one attachment owned by one authorized email message.

    Callers discover `(internet_message_id, attachment_id)` pairs through
    `get_email_details`. This function validates that pair at the provider
    before downloading bytes, so a guessed handle from another message fails.
    """
    if scope_error := _validate_scope(mailbox_email=mailbox_email, deal_id=deal_id):
        return scope_error
    if offset < 0 or max_chars <= 0:
        return _error(
            "invalid_attachment_range",
            "offset must be non-negative and max_chars must be positive.",
        )
    max_chars = min(max_chars, chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS)

    source = await resolve_readable_email_source(
        context=context,
        internet_message_id=internet_message_id,
        mailbox_email=mailbox_email,
        deal_id=deal_id,
        require_provider=False,
    )
    if source is None:
        return _error("email_not_found", "The email is unavailable in the requested scope.")

    try:
        cached = await _read_cached_deal_attachment(
            context=context,
            deal_id=source.deal_id,
            source=source,
            attachment_id=attachment_id,
        )
    except Exception:
        # A stale or unavailable cache is not authoritative; the provider path
        # can still recover the requested attachment.
        logger.warning(
            "Failed to read cached deal attachment %s",
            attachment_id,
            exc_info=True,
        )
        cached = None

    attachment: AttachmentMeta
    downloaded: DownloadedAttachment | None = None
    if cached is not None:
        attachment = cached.attachment
        downloaded = cached.downloaded
    else:
        provider_source = source
        if provider_source.token_info is None:
            resolved_provider = await resolve_readable_email_source(
                context=context,
                internet_message_id=internet_message_id,
                mailbox_email=mailbox_email,
                deal_id=deal_id,
            )
            if resolved_provider is None:
                return _error(
                    "provider_auth_unavailable",
                    "The email is available locally, but its provider cannot download attachments.",
                )
            provider_source = resolved_provider
        try:
            provider_attachments = await _list_provider_attachments(provider_source)
        except Exception:
            logger.exception("Failed to list email attachments before read")
            return _error(
                "attachment_list_failed",
                "The email was found, but its attachment list could not be loaded.",
            )

        selected_attachment = next(
            (item for item in provider_attachments if item.attachment_id == attachment_id),
            None,
        )
        if selected_attachment is None:
            return _error(
                "attachment_not_found",
                "The attachment does not belong to the requested email.",
            )
        attachment = selected_attachment
        source = provider_source

    if not _is_supported_mimetype(attachment.mime_type):
        return _error(
            "unsupported_attachment_type",
            f"{attachment.filename} has an unsupported attachment type.",
        )
    filter_reason = attachment_filter_reason(attachment)
    if filter_reason and filter_reason.startswith("oversized"):
        return _error(
            "attachment_too_large",
            f"{attachment.filename} exceeds the 25 MB attachment limit.",
        )
    if filter_reason:
        return _error(
            "attachment_not_readable",
            f"{attachment.filename} is an inline or non-document attachment.",
        )

    try:
        if downloaded is None:
            downloaded = await _download_provider_attachment(source, attachment)
    except ValueError as exc:
        if "exceeds" in str(exc).lower():
            return _error("attachment_too_large", str(exc))
        logger.warning(
            "Provider returned invalid bytes for email attachment %s: %s",
            attachment_id,
            exc,
        )
        return _error(
            "attachment_download_failed",
            f"{attachment.filename} could not be decoded.",
        )
    except Exception:
        logger.exception("Failed to download email attachment %s", attachment_id)
        return _error(
            "attachment_download_failed",
            f"{attachment.filename} could not be downloaded.",
        )

    normalized_mimetype = attachment.mime_type.split(";", maxsplit=1)[0].strip().lower()
    if is_image_mimetype(normalized_mimetype):
        return AgnoToolResult(
            content=(
                f"Opened email attachment {attachment.filename}. "
                "Analyze the image directly before answering."
            ),
            images=[
                AgnoImage(
                    id=attachment.attachment_id,
                    content=downloaded.content,
                    format=normalized_mimetype.split("/")[-1],
                    mime_type=normalized_mimetype,
                )
            ],
        )

    try:
        if is_audio_mimetype(normalized_mimetype):
            extracted = await transcribe_audio(downloaded.content, attachment.filename)
            visible = extracted[offset : offset + max_chars]
            truncated = len(extracted) > offset + max_chars
            chunk = {
                "content": visible,
                "offset": offset,
                "returned_characters": len(visible),
                "truncated": truncated,
                "next_offset": offset + len(visible) if truncated else None,
            }
            content_type = "audio_transcript"
        else:
            chunk = _extract_text_chunk(
                downloaded.content,
                mimetype=normalized_mimetype,
                filename=attachment.filename,
                offset=offset,
                max_chars=max_chars,
            )
            content_type = "text"
    except Exception:
        logger.exception("Failed to extract email attachment %s", attachment_id)
        return _error(
            "attachment_extraction_failed",
            f"{attachment.filename} could not be parsed.",
        )

    if not chunk["content"] and offset == 0:
        return _error(
            "attachment_content_empty",
            f"No readable content was found in {attachment.filename}.",
        )

    return {
        "status": "success",
        "internet_message_id": source.internet_message_id,
        "attachment_id": attachment.attachment_id,
        "filename": attachment.filename,
        "mime_type": normalized_mimetype,
        "size_bytes": downloaded.size_bytes,
        "content_type": content_type,
        **chunk,
    }


def create_read_email_attachment(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind the one-email, one-attachment reader to immutable agent context."""

    async def read_email_attachment_tool(
        internet_message_id: str,
        attachment_id: str,
        filename: str | None = None,
        mailbox_email: str | None = None,
        deal_id: str | None = None,
        offset: int = 0,
        max_chars: int = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS,
    ) -> dict[str, Any] | AgnoToolResult:
        """Read one email attachment using IDs returned by `get_email_details`.

        Use the same `mailbox_email` or `deal_id` scope used to read the owning
        email. If `truncated` is true and the remaining content could affect the
        answer, call this tool again with `next_offset` before answering. Pass
        the manifest filename for a readable progress label; it is not used for
        attachment selection or authorization.
        """
        display_filename = " ".join((filename or "").split())
        return cast(
            dict[str, Any] | AgnoToolResult,
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="read_email_attachment",
                tool_func=read_email_attachment,
                parameters={
                    "internet_message_id": internet_message_id,
                    "attachment_id": attachment_id,
                    "context": context,
                    "mailbox_email": mailbox_email,
                    "deal_id": deal_id,
                    "offset": offset,
                    "max_chars": max_chars,
                },
                description=(
                    f"Reading email attachment: {display_filename}"
                    if display_filename
                    else "Reading email attachment"
                ),
                fallback_value=_error(
                    "attachment_read_failed",
                    "The email attachment could not be read.",
                ),
            ),
        )

    return read_email_attachment_tool


__all__ = [
    "ReadableEmailSource",
    "create_read_email_attachment",
    "list_email_attachments",
    "read_email_attachment",
    "resolve_readable_email_source",
]
