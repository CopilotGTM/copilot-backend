"""
Sync service utilities to persist integration data into existing knowledge models.

This service intentionally reuses OrganisationSource and OrganisationDocument
instead of introducing new tables, as requested.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationDocument,
    OrganisationSource,
    OrganisationSourceStatus,
    SourceType,
)
from src.domain.models.organisation_tables.sync import KnowledgeIntegration


class SyncService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID) -> None:
        self.db = org_session
        self.organisation_id = organisation_id

    # ---------- Helpers ----------
    @staticmethod
    def _hash_key(*parts: str) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(p.encode("utf-8"))
            h.update(b"|")
        return h.hexdigest()

    async def _get_or_create_integration_source(
        self, *, integration: KnowledgeIntegration, label: str
    ) -> OrganisationSource:
        stmt = select(OrganisationSource).where(
            OrganisationSource.organisation_id == self.organisation_id,
            OrganisationSource.integration_id == integration.integration_id,
            OrganisationSource.source_type == SourceType.INTEGRATION,
            OrganisationSource.storage_path == label,
        )
        result = await self.db.execute(stmt)
        src = result.scalars().first()
        if src:
            return src

        src = OrganisationSource(
            organisation_id=self.organisation_id,
            user_id=integration.created_by_user_id,
            source_type=SourceType.INTEGRATION,
            storage_path=label,
            integration_id=integration.integration_id,
            status=OrganisationSourceStatus.COMPLETED,  # no heavy processing for event rows
        )
        self.db.add(src)
        await self.db.commit()
        await self.db.refresh(src)
        return src

    # ---------- Calendar (Google/Outlook) ----------
    async def upsert_calendar_events(
        self,
        *,
        integration: KnowledgeIntegration,
        events: Iterable[dict[str, Any]],
        provider: str,
    ) -> int:
        """
        Persist calendar events as OrganisationDocument entries under an INTEGRATION source.

        - Uses content_hash for idempotent upsert: sha256("calendar|{provider}|{external_id}")
        - Stores raw event payload in meta_data
        - Stores a human-readable summary in content
        """
        # Use a synthetic path to uniquely identify the integration's calendar container
        label = f"integration://{integration.integration_type.value}/calendar"
        source = await self._get_or_create_integration_source(integration=integration, label=label)

        upserted = 0
        for ev in events:
            external_id = str(ev.get("id") or ev.get("eventId") or "")
            if not external_id:
                continue

            # Build content, metadata and filters
            summary = ev.get("summary") or ev.get("subject") or "(no title)"
            start = (
                ev.get("start", {}).get("dateTime")
                if isinstance(ev.get("start"), dict)
                else ev.get("start")
            )
            end = (
                ev.get("end", {}).get("dateTime")
                if isinstance(ev.get("end"), dict)
                else ev.get("end")
            )
            organizer = (
                (ev.get("organizer") or {}).get("email")
                if isinstance(ev.get("organizer"), dict)
                else ev.get("organizer")
            )

            content = f"Event: {summary}\nStart: {start}\nEnd: {end}\nOrganizer: {organizer}"

            # Idempotency key
            content_hash = self._hash_key("calendar", provider, external_id)

            # Lookup existing document by content_hash
            stmt = select(OrganisationDocument).where(
                OrganisationDocument.content_hash == content_hash,
            )
            result = await self.db.execute(stmt)
            doc = result.scalars().first()

            if doc:
                # Update existing
                doc.name = summary
                doc.content = content
                doc.meta_data = ev
                doc.source_id = source.source_id
                self.db.add(doc)
            else:
                # Insert new
                doc = OrganisationDocument(
                    source_id=source.source_id,
                    name=summary,
                    content=content,
                    meta_data=ev,
                    content_hash=content_hash,
                    is_org_wide=True,
                )
                self.db.add(doc)

            upserted += 1

        # Update integration metadata
        integration.last_synced_at = datetime.now(UTC)
        self.db.add(integration)

        await self.db.commit()
        return upserted
