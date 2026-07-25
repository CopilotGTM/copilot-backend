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
from itertools import groupby
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
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
from src.service.integrations.email.attachment_provider import (
    AttachmentMeta,
    download_gmail_attachment,
    download_outlook_attachment,
    filter_junk_attachments,
    list_gmail_message_attachments,
    list_outlook_attachments,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)

logger = get_logger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STORAGE_FILENAME_LENGTH = 128

# Bound execution time per deal
MAX_ATTACHMENTS_PER_DEAL = 50
EMAIL_PROVIDER_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE.value,
    IntegrationType.MICROSOFT.value,
)


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


def _build_legacy_attachment_source_record_id(
    internet_message_id: str,
    attachment_id: str,
) -> str:
    """Reproduce the provider-local source key used by existing asset rows."""
    msg_hash = hashlib.sha256(internet_message_id.encode()).hexdigest()[:16]
    attachment_hash = hashlib.sha256(attachment_id.encode()).hexdigest()[:16]
    return f"email:{msg_hash}:attachment:{attachment_hash}"


def _build_attachment_source_record_id(
    internet_message_id: str,
    filename: str,
    content_sha256: str,
) -> str:
    """Build the mailbox-independent identity for one canonical message file."""
    msg_hash = hashlib.sha256(internet_message_id.encode()).hexdigest()[:16]
    normalized_filename = _safe_asset_filename(filename).casefold()
    file_hash = hashlib.sha256(f"{normalized_filename}\0{content_sha256}".encode()).hexdigest()[:32]
    return f"email:{msg_hash}:file:{file_hash}"


def _build_storage_path(
    deal_id: UUID,
    internet_message_id: str,
    source_identity: str,
    filename: str,
) -> str:
    """Build Azure Blob Storage path for an email attachment asset."""
    msg_hash = hashlib.sha256(internet_message_id.encode()).hexdigest()[:12]
    attachment_hash = hashlib.sha256(source_identity.encode()).hexdigest()[:12]
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
    return integration_type in {IntegrationType.GMAIL, IntegrationType.GOOGLE}


def _email_has_attachments_filter() -> ColumnElement[bool]:
    return col(Email.has_attachments) == "true"


class DealAssetEmailAttachmentService:
    """Extract email attachments for a deal and create DealAsset records.

    Called by the Celery task layer. Manages its own sessions using the
    fresh session pattern.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)

    async def process_deal(
        self,
        deal_id: UUID,
        *,
        email_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        """Process all unprocessed email attachments for a single deal.

        Entry point when called directly (per-deal task). Owns storage
        lifecycle — closes storage when done.
        """
        try:
            return await self._process_deal_internal(deal_id, email_ids=email_ids)
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
            "post_upload_asset_ids": [],
            "continuations": [],
        }

        try:
            deal_ids = await self._find_deals_with_unprocessed_attachments()
            logger.info(
                "Found %d deals with unprocessed email attachments in org %s",
                len(deal_ids),
                self.organisation_id,
            )

            for deal_id in deal_ids:
                deal_stats = await self._process_deal_internal(deal_id, email_ids=None)
                stats["deals_processed"] += 1
                stats["total_assets_created"] += deal_stats["assets_created"]
                stats["total_errors"] += deal_stats["errors"]
                stats["post_upload_asset_ids"].extend(deal_stats.get("post_upload_asset_ids", []))
                remaining_email_ids = deal_stats.get("remaining_email_ids", [])
                if deal_stats.get("limit_reached") and remaining_email_ids:
                    stats["continuations"].append(
                        {
                            "deal_id": str(deal_id),
                            "email_ids": list(remaining_email_ids),
                        }
                    )

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

    async def _process_deal_internal(
        self,
        deal_id: UUID,
        *,
        email_ids: list[UUID] | None,
    ) -> dict[str, Any]:
        """Core deal processing logic. Does NOT close storage — caller owns that."""
        stats: dict[str, Any] = {
            "deal_id": str(deal_id),
            "emails_with_attachments": 0,
            "assets_created": 0,
            "assets_skipped_dedup": 0,
            "errors": 0,
            "limit_reached": False,
            "remaining_email_ids": [],
            "post_upload_asset_ids": [],
        }

        try:
            emails = await self._find_emails_needing_extraction(
                deal_id,
                email_ids=email_ids,
            )
            canonical_messages = [
                list(copies)
                for _message_id, copies in groupby(
                    emails,
                    key=lambda email: email.internet_message_id,
                )
            ]
            stats["emails_with_attachments"] = len(canonical_messages)

            if not canonical_messages:
                return stats

            total_created = 0
            scoped_email_ids = set(email_ids or [])

            def remaining_ids(start_index: int) -> list[str]:
                """Choose one replayable copy ID for each unvisited message."""
                return [
                    str(
                        next(
                            (copy.email_id for copy in copies if copy.email_id in scoped_email_ids),
                            copies[0].email_id,
                        )
                    )
                    for copies in canonical_messages[start_index:]
                ]

            for message_index, copies in enumerate(canonical_messages):
                if total_created >= MAX_ATTACHMENTS_PER_DEAL:
                    logger.info(
                        "Hit max attachments (%d) for deal %s",
                        MAX_ATTACHMENTS_PER_DEAL,
                        deal_id,
                    )
                    stats["limit_reached"] = True
                    stats["remaining_email_ids"] = remaining_ids(message_index)
                    break

                message_succeeded = False
                # copilotgtm: connected copies are normally single digits; try each one
                # before adding health ranking or a cap based on measured mailbox volume.
                for email_rec in copies:
                    try:
                        (
                            created,
                            skipped,
                            post_upload_asset_ids,
                            limit_reached,
                        ) = await self._process_single_email(
                            email_rec,
                            remaining=MAX_ATTACHMENTS_PER_DEAL - total_created,
                        )
                    except Exception:
                        logger.warning(
                            "Mailbox copy failed for canonical message %s",
                            email_rec.internet_message_id,
                            exc_info=True,
                        )
                        continue
                    stats["assets_created"] += created
                    stats["assets_skipped_dedup"] += skipped
                    stats["post_upload_asset_ids"].extend(
                        str(asset_id) for asset_id in post_upload_asset_ids
                    )
                    total_created += created
                    stats["limit_reached"] = limit_reached
                    message_succeeded = True
                    if limit_reached:
                        stats["remaining_email_ids"] = remaining_ids(message_index)
                    break
                if not message_succeeded:
                    stats["errors"] += 1
                if stats["limit_reached"]:
                    break

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
                    col(Email.is_deleted).is_(False),
                    _email_has_attachments_filter(),
                )
                .distinct()
            )

            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

        return []

    async def _find_emails_needing_extraction(
        self,
        deal_id: UUID,
        *,
        email_ids: list[UUID] | None = None,
    ) -> list[_EmailWithAttachments]:
        """Find every active mailbox copy that can provide associated attachments."""
        async for session in acquire_org_async_session(self.organisation_id):
            scoped_message_ids = None
            if email_ids is not None:
                seed_email = aliased(Email)
                scoped_message_ids = select(seed_email.internet_message_id).where(
                    seed_email.organisation_id == self.organisation_id,
                    seed_email.email_id.in_(email_ids),
                    seed_email.is_deleted.is_(False),
                    seed_email.has_attachments == "true",
                    seed_email.internet_message_id.is_not(None),
                )
            stmt = (
                select(
                    col(Email.email_id),
                    col(Email.external_message_id),
                    col(Email.internet_message_id),
                    col(Email.subject),
                    col(Email.integration_id),
                    KnowledgeIntegration,
                    col(KnowledgeIntegration.integration_type),
                    col(DealEmailAssociation.direction),
                    col(DealEmailAssociation.deal_id),
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
                    col(Email.is_deleted).is_(False),
                    _email_has_attachments_filter(),
                    col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                    col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                    col(KnowledgeIntegration.integration_type).in_(
                        EMAIL_PROVIDER_INTEGRATION_TYPES
                    ),
                )
                .order_by(
                    col(Email.internet_message_id),
                    col(Email.integration_id),
                    col(Email.email_id),
                )
            )
            if scoped_message_ids is not None:
                stmt = stmt.where(col(Email.internet_message_id).in_(scoped_message_ids))

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
    ) -> tuple[int, int, list[UUID], bool]:
        """Process one mailbox copy and return committed work without publishing it."""
        created = 0
        skipped = 0
        post_upload_asset_ids: list[UUID] = []
        is_gmail_type = _is_gmail(email_rec.integration_type)

        token = await get_access_token(email_rec.token_info)

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        ) as client:
            if is_gmail_type:
                att_metas = await list_gmail_message_attachments(
                    client,
                    email_rec.external_message_id,
                )
            else:
                att_metas = await list_outlook_attachments(client, email_rec.external_message_id)

            filtered = filter_junk_attachments(att_metas)
            source_ids = [
                _build_legacy_attachment_source_record_id(
                    email_rec.internet_message_id,
                    att_meta.attachment_id,
                )
                for att_meta in filtered
            ]
            existing_assets = await self._find_existing_attachment_assets(
                email_rec.deal_id,
                source_ids,
            )

            for att_meta in filtered:
                if not _is_supported_attachment_mimetype(att_meta.mime_type):
                    logger.info(
                        "Skipping email attachment with unsupported mimetype=%s",
                        att_meta.mime_type,
                    )
                    skipped += 1
                    continue

                legacy_source_id = _build_legacy_attachment_source_record_id(
                    email_rec.internet_message_id,
                    att_meta.attachment_id,
                )
                if existing_asset := existing_assets.get(legacy_source_id):
                    skipped += 1
                    if (
                        existing_asset.status
                        in {
                            DealAssetStatus.PROCESSING.value,
                            DealAssetStatus.FAILED.value,
                        }
                        and existing_asset.storage_path
                    ):
                        post_upload_asset_ids.append(existing_asset.asset_id)
                    continue

                if created >= remaining:
                    return created, skipped, post_upload_asset_ids, True

                (
                    attachment_created,
                    attachment_skipped,
                    post_upload_asset_id,
                ) = await self._download_and_create_asset(
                    client,
                    email_rec,
                    att_meta,
                    is_gmail_type,
                )
                created += attachment_created
                skipped += attachment_skipped
                if post_upload_asset_id is not None:
                    post_upload_asset_ids.append(post_upload_asset_id)

        return created, skipped, post_upload_asset_ids, False

    async def _download_and_create_asset(
        self,
        client: httpx.AsyncClient,
        email_rec: _EmailWithAttachments,
        att_meta: AttachmentMeta,
        is_gmail_type: bool,
    ) -> tuple[int, int, UUID | None]:
        """Download one attachment and return counts plus required post-upload work."""
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

        checksum = hashlib.sha256(downloaded.content).hexdigest()
        existing_asset = await self._find_legacy_asset_by_content(
            email_rec.deal_id,
            email_rec.internet_message_id,
            att_meta.filename,
            checksum,
        )
        if existing_asset is not None:
            needs_post_upload = (
                existing_asset.status
                in {
                    DealAssetStatus.PROCESSING.value,
                    DealAssetStatus.FAILED.value,
                }
                and existing_asset.storage_path
            )
            return 0, 1, existing_asset.asset_id if needs_post_upload else None

        source_record_id = _build_attachment_source_record_id(
            email_rec.internet_message_id,
            att_meta.filename,
            checksum,
        )
        storage_path = _build_storage_path(
            email_rec.deal_id,
            email_rec.internet_message_id,
            source_record_id,
            att_meta.filename,
        )
        await self.storage.upload_bytes(
            storage_path,
            downloaded.content,
            content_type=_normalize_mimetype(att_meta.mime_type),
        )

        inserted, post_upload_asset_id = await self._create_deal_asset(
            email_rec,
            att_meta,
            storage_path,
            downloaded.size_bytes,
            source_record_id,
            checksum,
        )
        return (1, 0, post_upload_asset_id) if inserted else (0, 1, post_upload_asset_id)

    # ── Phase 3: Create DealAsset record (fresh session) ────────────

    async def _find_existing_attachment_assets(
        self,
        deal_id: UUID,
        source_record_ids: list[str],
    ) -> dict[str, DealAsset]:
        """Return existing rows so retries can distinguish complete and stranded work."""
        if not source_record_ids:
            return {}

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(DealAsset).where(
                col(DealAsset.organisation_id) == self.organisation_id,
                col(DealAsset.deal_id) == deal_id,
                col(DealAsset.source_type) == DealAssetSourceType.EMAIL_ATTACHMENT.value,
                col(DealAsset.source_record_id).in_(source_record_ids),
            )
            result = await session.execute(stmt)
            return {
                asset.source_record_id: asset
                for asset in result.scalars().all()
                if asset.source_record_id
            }

        return {}

    async def _find_legacy_asset_by_content(
        self,
        deal_id: UUID,
        internet_message_id: str,
        filename: str,
        checksum: str,
    ) -> DealAsset | None:
        """Reuse a verified legacy row when another mailbox exposes different provider IDs."""
        candidates: list[DealAsset] = []
        normalized_filename = _safe_asset_filename(filename).casefold()
        async for session in acquire_org_async_session(self.organisation_id):
            result = await session.execute(
                select(DealAsset).where(
                    col(DealAsset.organisation_id) == self.organisation_id,
                    col(DealAsset.deal_id) == deal_id,
                    col(DealAsset.source_type) == DealAssetSourceType.EMAIL_ATTACHMENT.value,
                    func.lower(col(DealAsset.filename)) == normalized_filename,
                )
            )
            candidates = list(result.scalars().all())
            break

        for candidate in candidates:
            metadata = dict(candidate.asset_metadata or {})
            if metadata.get("internet_message_id") != internet_message_id:
                continue
            if candidate.checksum == checksum:
                return candidate
            if candidate.checksum is not None or not candidate.storage_path:
                continue

            # The candidate query is complete before Azure I/O so a slow legacy checksum
            # recovery never pins an organisation database connection.
            legacy_checksum = await self.storage.compute_checksum(candidate.storage_path)
            if legacy_checksum != checksum:
                continue
            async for session in acquire_org_async_session(self.organisation_id):
                stored = await session.get(DealAsset, candidate.asset_id)
                if stored is not None and stored.checksum is None:
                    stored.checksum = legacy_checksum
                    session.add(stored)
                    await session.commit()
                break
            candidate.checksum = legacy_checksum
            return candidate
        return None

    async def _create_deal_asset(
        self,
        email_rec: _EmailWithAttachments,
        att_meta: AttachmentMeta,
        storage_path: str,
        size_bytes: int,
        source_record_id: str,
        checksum: str,
    ) -> tuple[bool, UUID | None]:
        """Insert one asset and return whether it was created plus post-upload work."""
        asset_filename = _safe_asset_filename(att_meta.filename)
        source_context = f"Email: {email_rec.subject or '(no subject)'}"[:500]
        direction = _map_email_direction_to_asset_direction(email_rec.direction)

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
                    checksum=checksum,
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
                .returning(col(DealAsset.asset_id))
            )
            result = await session.execute(stmt)
            row = result.first()
            await session.commit()

            if row:
                logger.info(
                    "Created DealAsset for %s from email %s on deal %s",
                    att_meta.filename,
                    email_rec.internet_message_id,
                    email_rec.deal_id,
                )
                return True, row[0]

            existing_result = await session.execute(
                select(DealAsset).where(
                    col(DealAsset.organisation_id) == self.organisation_id,
                    col(DealAsset.deal_id) == email_rec.deal_id,
                    col(DealAsset.source_type) == DealAssetSourceType.EMAIL_ATTACHMENT.value,
                    col(DealAsset.source_record_id) == source_record_id,
                    col(DealAsset.filename) == asset_filename,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if (
                existing
                and existing.status
                in {
                    DealAssetStatus.PROCESSING.value,
                    DealAssetStatus.FAILED.value,
                }
                and existing.storage_path
            ):
                return False, existing.asset_id
            return False, None

        return False, None

    # ── Helpers ──────────────────────────────────────────────────────

    async def _close_storage(self) -> None:
        try:
            close_coro = self.storage.close()
            if close_coro is not None:
                await close_coro
        except Exception:
            logger.debug("Failed to close storage client", exc_info=True)
