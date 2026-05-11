"""Extract email attachments and create DealAsset records.

For each deal, finds associated emails with attachments, downloads files from
Gmail/Outlook via provider APIs, uploads to Azure Blob Storage, and inserts
DealAsset records with source_type=EMAIL_ATTACHMENT.

Follows the fresh session pattern — sessions acquired only for DB ops and
released before long-running external calls (API downloads, blob uploads).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import col
from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetDirection,
    DealAssetSourceType,
    DealAssetStatus,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.configs import chat_file_upload_config
from src.infra.database.sessions import acquire_org_async_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.email.attachment_downloader import (
    AttachmentMeta,
    download_gmail_attachment,
    download_outlook_attachment,
    filter_junk_attachments,
    list_gmail_attachments,
    list_outlook_attachments,
)

logger = get_logger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STORAGE_FILENAME_LENGTH = 128

# Bound execution time per deal
MAX_ATTACHMENTS_PER_DEAL = 50


@dataclass
class _EmailWithAttachments:
    """Email record with its integration info, ready for attachment download."""

    email_id: UUID
    external_message_id: str
    internet_message_id: str
    subject: str | None
    integration_id: UUID
    integration_type: IntegrationType
    token_info: IntegrationTokenInfo
    direction: str
    deal_id: UUID


def _build_attachment_source_record_id(internet_message_id: str, attachment_id: str) -> str:
    """Build a stable source ID for one provider attachment on one email."""
    msg_hash = hashlib.sha256(internet_message_id.encode()).hexdigest()[:16]
    attachment_hash = hashlib.sha256(attachment_id.encode()).hexdigest()[:16]
    return f"email:{msg_hash}:attachment:{attachment_hash}"


def _build_storage_path(
    deal_id: UUID,
    internet_message_id: str,
    attachment_id: str,
    filename: str,
) -> str:
    """Build Azure Blob Storage path for an email attachment asset."""
    msg_hash = hashlib.sha256(internet_message_id.encode()).hexdigest()[:12]
    attachment_hash = hashlib.sha256(attachment_id.encode()).hexdigest()[:12]
    sanitized = _safe_asset_filename(filename)
    return f"deal_assets/{deal_id}/email_attachments/{msg_hash}/{attachment_hash}/{sanitized}"


def _safe_asset_filename(filename: str) -> str:
    """Return a storage-safe bounded filename while preserving useful extensions."""
    sanitized = _SANITIZE_RE.sub("_", filename).strip("_")
    if not sanitized:
        sanitized = "attachment"
    if len(sanitized) > _MAX_STORAGE_FILENAME_LENGTH:
        stem, dot, extension = sanitized.rpartition(".")
        if dot and extension:
            suffix = f".{extension[:16]}"
            sanitized = f"{stem[: _MAX_STORAGE_FILENAME_LENGTH - len(suffix)]}{suffix}"
        else:
            sanitized = sanitized[:_MAX_STORAGE_FILENAME_LENGTH]
    return sanitized


def _map_email_direction_to_asset_direction(
    direction: DealEmailDirection | str,
) -> str:
    """Map email direction to deal asset direction."""
    direction_str = direction.value if isinstance(direction, DealEmailDirection) else direction
    mapping = {
        DealEmailDirection.INBOUND.value: DealAssetDirection.RECEIVED.value,
        DealEmailDirection.OUTBOUND.value: DealAssetDirection.SHARED.value,
        DealEmailDirection.INTERNAL.value: DealAssetDirection.INTERNAL.value,
    }
    return mapping.get(direction_str, DealAssetDirection.INTERNAL.value)


def _normalize_mimetype(mimetype: str | None) -> str:
    return (mimetype or "application/octet-stream").split(";", maxsplit=1)[0].strip().lower()


def _is_supported_attachment_mimetype(mimetype: str | None) -> bool:
    return bool(mimetype) and _normalize_mimetype(mimetype) in {
        item.lower() for item in chat_file_upload_config.ALLOWED_MIMETYPES
    }


def _is_gmail(integration_type: IntegrationType) -> bool:
    return integration_type == IntegrationType.GMAIL


class DealAssetEmailAttachmentService:
    """Extract email attachments for a deal and create DealAsset records.

    Called by the Celery task layer. Manages its own sessions using the
    fresh session pattern.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)

    async def process_deal(self, deal_id: UUID) -> dict[str, Any]:
        """Process all unprocessed email attachments for a single deal.

        Entry point when called directly (per-deal task). Owns storage
        lifecycle — closes storage when done.
        """
        try:
            return await self._process_deal_internal(deal_id)
        finally:
            await self._close_storage()

    async def process_organisation(self) -> dict[str, Any]:
        """Process email attachments for all deals in the organisation.

        Owns storage lifecycle — closes once after all deals are processed.
        """
        stats: dict[str, Any] = {
            "organisation_id": str(self.organisation_id),
            "deals_processed": 0,
            "total_assets_created": 0,
            "total_errors": 0,
        }

        try:
            deal_ids = await self._find_deals_with_unprocessed_attachments()
            logger.info(
                "Found %d deals with unprocessed email attachments in org %s",
                len(deal_ids),
                self.organisation_id,
            )

            for deal_id in deal_ids:
                deal_stats = await self._process_deal_internal(deal_id)
                stats["deals_processed"] += 1
                stats["total_assets_created"] += deal_stats["assets_created"]
                stats["total_errors"] += deal_stats["errors"]

            return stats

        except Exception as e:
            logger.error(
                "Failed to process attachments for org %s: %s",
                self.organisation_id,
                e,
                exc_info=True,
            )
            stats["total_errors"] += 1
            return stats

        finally:
            await self._close_storage()

    async def _process_deal_internal(self, deal_id: UUID) -> dict[str, Any]:
        """Core deal processing logic. Does NOT close storage — caller owns that."""
        stats: dict[str, Any] = {
            "deal_id": str(deal_id),
            "emails_with_attachments": 0,
            "assets_created": 0,
            "assets_skipped_dedup": 0,
            "errors": 0,
        }

        try:
            emails = await self._find_emails_needing_extraction(deal_id)
            stats["emails_with_attachments"] = len(emails)

            if not emails:
                return stats

            total_created = 0
            for email_rec in emails:
                if total_created >= MAX_ATTACHMENTS_PER_DEAL:
                    logger.info(
                        "Hit max attachments (%d) for deal %s",
                        MAX_ATTACHMENTS_PER_DEAL,
                        deal_id,
                    )
                    break

                created, skipped, errs = await self._process_single_email(
                    email_rec,
                    remaining=MAX_ATTACHMENTS_PER_DEAL - total_created,
                )
                stats["assets_created"] += created
                stats["assets_skipped_dedup"] += skipped
                stats["errors"] += errs
                total_created += created

            return stats

        except Exception as e:
            logger.error(
                "Failed to process attachments for deal %s: %s",
                deal_id,
                e,
                exc_info=True,
            )
            stats["errors"] += 1
            return stats

    # ── Phase 1: DB queries (session acquired and released) ─────────

    async def _find_deals_with_unprocessed_attachments(
        self,
    ) -> list[UUID]:
        """Find deals that have email associations where the email has
        attachments. Over-triggers intentionally — the per-deal
        _find_emails_needing_extraction does the precise dedup.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(col(DealEmailAssociation.deal_id))
                .join(
                    Email,
                    col(Email.internet_message_id) == col(DealEmailAssociation.internet_message_id),
                )
                .where(
                    col(DealEmailAssociation.organisation_id) == self.organisation_id,
                    col(DealEmailAssociation.status) == "ACTIVE",
                    col(Email.organisation_id) == self.organisation_id,
                    col(Email.has_attachments) == True,  # noqa: E712
                )
                .distinct()
            )

            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

        return []

    async def _find_emails_needing_extraction(self, deal_id: UUID) -> list[_EmailWithAttachments]:
        """Find emails associated with a deal that have attachments not yet
        extracted. Picks one email per internet_message_id (the one with an
        active integration we can download from).
        """
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(
                    Email.email_id,
                    Email.external_message_id,
                    Email.internet_message_id,
                    Email.subject,
                    Email.integration_id,
                    KnowledgeIntegration,
                    KnowledgeIntegration.integration_type,
                    DealEmailAssociation.direction,
                    DealEmailAssociation.deal_id,
                )
                .join(
                    DealEmailAssociation,
                    col(Email.internet_message_id) == col(DealEmailAssociation.internet_message_id),
                )
                .join(
                    KnowledgeIntegration,
                    col(Email.integration_id) == col(KnowledgeIntegration.integration_id),
                )
                .where(
                    col(DealEmailAssociation.deal_id) == deal_id,
                    col(DealEmailAssociation.organisation_id) == self.organisation_id,
                    col(DealEmailAssociation.status) == "ACTIVE",
                    col(Email.organisation_id) == self.organisation_id,
                    col(Email.has_attachments) == True,  # noqa: E712
                    col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                    col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                    col(KnowledgeIntegration.integration_type).in_(
                        [IntegrationType.GMAIL.value, IntegrationType.OUTLOOK_EMAIL.value]
                    ),
                )
                .distinct(Email.internet_message_id)
                .order_by(Email.internet_message_id, Email.email_id)
            )

            result = await session.execute(stmt)
            rows = result.all()

            emails: list[_EmailWithAttachments] = []
            for row in rows:
                integration = row[5]
                integration_type = IntegrationType(row[6])
                emails.append(
                    _EmailWithAttachments(
                        email_id=row[0],
                        external_message_id=row[1],
                        internet_message_id=row[2],
                        subject=row[3],
                        integration_id=row[4],
                        integration_type=integration_type,
                        token_info=IntegrationTokenInfo.from_integration(
                            integration,
                            self.organisation_id,
                        ),
                        direction=row[7],
                        deal_id=row[8],
                    )
                )
            return emails

        return []

    # ── Phase 2: Download + Upload (no DB session held) ─────────────

    async def _process_single_email(
        self,
        email_rec: _EmailWithAttachments,
        remaining: int,
    ) -> tuple[int, int, int]:
        """Process attachments for one email. Returns (created, skipped, errors)."""
        created = 0
        skipped = 0
        errors = 0

        try:
            is_gmail_type = _is_gmail(email_rec.integration_type)

            if is_gmail_type:
                from src.workers.integrations_worker.providers._google_api import (
                    get_access_token,
                )
            else:
                from src.workers.integrations_worker.providers._microsoft_graph import (  # noqa: E501
                    get_access_token,
                )

            token = await get_access_token(email_rec.token_info)

            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=60.0,
            ) as client:
                # List attachments with metadata
                if is_gmail_type:
                    payload = await self._fetch_gmail_payload(client, email_rec.external_message_id)
                    att_metas = list_gmail_attachments(payload)
                else:
                    att_metas = await list_outlook_attachments(
                        client, email_rec.external_message_id
                    )

                filtered = filter_junk_attachments(att_metas)
                source_ids = [
                    _build_attachment_source_record_id(
                        email_rec.internet_message_id,
                        att_meta.attachment_id,
                    )
                    for att_meta in filtered
                ]
                existing_source_ids = await self._find_existing_attachment_source_ids(
                    email_rec.deal_id,
                    source_ids,
                )

                for att_meta in filtered:
                    if created >= remaining:
                        break

                    if not _is_supported_attachment_mimetype(att_meta.mime_type):
                        logger.info(
                            "Skipping email attachment %s: unsupported mimetype=%s",
                            att_meta.filename,
                            att_meta.mime_type,
                        )
                        skipped += 1
                        continue

                    source_record_id = _build_attachment_source_record_id(
                        email_rec.internet_message_id,
                        att_meta.attachment_id,
                    )
                    if source_record_id in existing_source_ids:
                        skipped += 1
                        continue

                    try:
                        c, s = await self._download_and_create_asset(
                            client,
                            email_rec,
                            att_meta,
                            is_gmail_type,
                            source_record_id,
                        )
                        created += c
                        skipped += s
                    except Exception as e:
                        logger.warning(
                            "Failed to process attachment %s from email %s: %s",
                            att_meta.filename,
                            email_rec.email_id,
                            e,
                        )
                        errors += 1

        except Exception as e:
            logger.error(
                "Failed to process email %s attachments: %s",
                email_rec.email_id,
                e,
                exc_info=True,
            )
            errors += 1

        return created, skipped, errors

    async def _download_and_create_asset(
        self,
        client: httpx.AsyncClient,
        email_rec: _EmailWithAttachments,
        att_meta: AttachmentMeta,
        is_gmail_type: bool,
        source_record_id: str,
    ) -> tuple[int, int]:
        """Download one attachment and create DealAsset. Returns (created, skipped)."""
        if is_gmail_type:
            downloaded = await download_gmail_attachment(
                client,
                email_rec.external_message_id,
                att_meta.attachment_id,
            )
        else:
            downloaded = await download_outlook_attachment(
                client,
                email_rec.external_message_id,
                att_meta.attachment_id,
            )

        storage_path = _build_storage_path(
            email_rec.deal_id,
            email_rec.internet_message_id,
            att_meta.attachment_id,
            att_meta.filename,
        )
        await self.storage.upload_bytes(
            storage_path,
            downloaded.content,
            content_type=_normalize_mimetype(att_meta.mime_type),
        )

        inserted = await self._create_deal_asset(
            email_rec,
            att_meta,
            storage_path,
            downloaded.size_bytes,
            source_record_id,
        )
        return (1, 0) if inserted else (0, 1)

    async def _fetch_gmail_payload(
        self, client: httpx.AsyncClient, message_id: str
    ) -> dict[str, Any]:
        """Fetch Gmail message payload for attachment metadata."""
        from src.workers.integrations_worker.providers._google_api import (
            GMAIL_BASE_URL,
        )

        url = f"{GMAIL_BASE_URL}/gmail/v1/users/me/messages/{quote(message_id, safe='')}"
        # format=full required to get the MIME parts tree with attachmentIds
        response = await client.get(url, params={"format": "full"})
        response.raise_for_status()
        return response.json().get("payload", {})

    # ── Phase 3: Create DealAsset record (fresh session) ────────────

    async def _find_existing_attachment_source_ids(
        self,
        deal_id: UUID,
        source_record_ids: list[str],
    ) -> set[str]:
        """Return already-processed email attachment source IDs for this deal."""
        if not source_record_ids:
            return set()

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(col(DealAsset.source_record_id)).where(
                col(DealAsset.organisation_id) == self.organisation_id,
                col(DealAsset.deal_id) == deal_id,
                col(DealAsset.source_type) == DealAssetSourceType.EMAIL_ATTACHMENT.value,
                col(DealAsset.source_record_id).in_(source_record_ids),
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all() if row[0]}

        return set()

    async def _create_deal_asset(
        self,
        email_rec: _EmailWithAttachments,
        att_meta: AttachmentMeta,
        storage_path: str,
        size_bytes: int,
        source_record_id: str,
    ) -> bool:
        """Insert a DealAsset. Returns True if inserted, False if dedup hit."""
        asset_filename = _safe_asset_filename(att_meta.filename)
        source_context = f"Email: {email_rec.subject or '(no subject)'}"[:500]
        direction = _map_email_direction_to_asset_direction(email_rec.direction)
        inserted_asset_id: UUID | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                insert(DealAsset)
                .values(
                    organisation_id=self.organisation_id,
                    deal_id=email_rec.deal_id,
                    added_by_user_id=AUTOMATED_PIPELINE_USER_ID,
                    name=asset_filename,
                    filename=asset_filename,
                    mimetype=_normalize_mimetype(att_meta.mime_type),
                    size_bytes=size_bytes,
                    storage_path=storage_path,
                    status=DealAssetStatus.PROCESSING.value,
                    direction=direction,
                    source_type=DealAssetSourceType.EMAIL_ATTACHMENT.value,
                    source_record_id=source_record_id,
                    source_context=source_context,
                    asset_metadata={
                        "email_id": str(email_rec.email_id),
                        "internet_message_id": email_rec.internet_message_id,
                        "provider_attachment_id": att_meta.attachment_id,
                        "integration_type": email_rec.integration_type.value,
                    },
                )
                .on_conflict_do_nothing(constraint="uq_deal_asset_source_dedup")
                .returning(DealAsset.asset_id)
            )
            result = await session.execute(stmt)
            row = result.first()
            await session.commit()

            if row:
                inserted_asset_id = row[0]
                logger.info(
                    "Created DealAsset for %s from email %s on deal %s",
                    att_meta.filename,
                    email_rec.internet_message_id,
                    email_rec.deal_id,
                )

        # Session released — dispatch post-upload outside the session block
        if inserted_asset_id:
            self._dispatch_post_upload(inserted_asset_id)
            return True

        return False

    def _dispatch_post_upload(self, asset_id: UUID) -> None:
        """Dispatch post-upload task for checksum + thumbnail generation."""
        try:
            from src.workers.deal_worker.client import send_deal_task

            send_deal_task(
                "deal_asset.post_upload",
                args=[str(self.organisation_id), str(asset_id)],
                queue="deal",
            )
        except Exception:
            logger.warning(
                "Failed to dispatch post-upload for asset %s — "
                "asset will stay in PROCESSING until manually resolved",
                asset_id,
                exc_info=True,
            )

    # ── Helpers ──────────────────────────────────────────────────────

    async def _close_storage(self) -> None:
        try:
            close_coro = self.storage.close()
            if close_coro is not None:
                await close_coro
        except Exception:
            logger.debug("Failed to close storage client", exc_info=True)
