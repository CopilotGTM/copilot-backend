#!/usr/bin/env python3
"""Backfill CRMContact and CRMDealParticipant records for existing stakeholders.

Background:
    Before this fix, manually-added stakeholders were created in the DealStakeholder
    table but had no corresponding CRMContact or CRMDealParticipant record. This meant:
    - No cross-deal person identity (contact_id was NULL)
    - Calendar/email matching couldn't discover other attendees from the same domain
    - The stakeholder was invisible to the matching pipelines

    This script fixes existing data to match the new behavior where manual stakeholder
    creation automatically ensures a CRMContact and CRMDealParticipant exist.

Scope:
    - Only processes stakeholders on INTERNAL CRM deals (not HubSpot/Salesforce)
    - Skips personal email providers (gmail, yahoo, hotmail, etc.)
    - MANUAL stakeholders: find-or-create CRMContact + create CRMDealParticipant
    - CALENDAR/EMAIL_DISCOVERY stakeholders: link to existing CRMContact if found, skip otherwise

Two passes:
    1. Orphaned stakeholders (contact_id=NULL) — creates contacts + participants
    2. Already-linked stakeholders (have contact_id but no participant) — creates participants only

Usage:
    uv run python -m scripts.maintenance.backfill_stakeholder_contacts --all --dry-run
    uv run python -m scripts.maintenance.backfill_stakeholder_contacts --all
    uv run python -m scripts.maintenance.backfill_stakeholder_contacts --org-id <UUID>

Idempotent: safe to run multiple times. Checks for existing records before creating.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dotenv import load_dotenv
from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    StakeholderSource,
)
from src.infra.database.sessions import (
    acquire_master_session,
    acquire_org_async_session,
)
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService

SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill CRMContact for orphaned stakeholders.")
    parser.add_argument("--org-id", type=UUID, help="Single org to process.")
    parser.add_argument("--all", action="store_true", help="Process all approved orgs.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without committing.")
    return parser.parse_args()


def load_org_ids(org_id: UUID | None, run_all: bool) -> list[UUID]:
    session_gen = acquire_master_session()
    session = next(session_gen)
    try:
        if run_all:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
        elif org_id:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.organisation_id) == org_id
            )
        else:
            return []
        result = session.execute(stmt)
        return [row[0] for row in result.all()]
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()


async def _ensure_participant(
    session,
    org_id: UUID,
    integration_id: UUID,
    stakeholder: DealStakeholder,
    contact_id: UUID,
    email: str,
) -> bool:
    """Create CRMDealParticipant if the deal is internal CRM and no participant exists."""
    # Check if deal belongs to internal CRM
    deal = await session.get(CRMDeal, stakeholder.deal_id)
    if not deal or deal.integration_id != integration_id:
        return False

    # Check if participant already exists
    existing_stmt = select(CRMDealParticipant).where(
        col(CRMDealParticipant.deal_id) == stakeholder.deal_id,
        col(CRMDealParticipant.related_email) == email,
        col(CRMDealParticipant.is_deleted).is_(False),
    )
    existing_result = await session.execute(existing_stmt)
    if existing_result.scalars().first():
        return False

    now = datetime.now(UTC)
    participant_id = uuid4()
    participant = CRMDealParticipant(
        participant_id=participant_id,
        organisation_id=org_id,
        integration_id=integration_id,
        external_id=str(participant_id),
        deal_id=stakeholder.deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_external_id=str(contact_id),
        related_local_id=contact_id,
        related_name=stakeholder.name,
        related_email=email,
        synced_at=now,
        source_modified_at=now,
        last_seen_at=now,
        data_payload={"source": "backfill_stakeholder_contacts"},
    )
    session.add(participant)
    return True


PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "mail.com",
    "protonmail.com",
    "live.com",
    "msn.com",
    "ymail.com",
    "googlemail.com",
}


def _is_personal_email(email: str) -> bool:
    domain = email.split("@")[-1] if "@" in email else ""
    return domain in PERSONAL_EMAIL_DOMAINS


async def backfill_org(org_id: UUID, dry_run: bool) -> dict[str, int]:
    stats = {
        "linked": 0,
        "created": 0,
        "participants_created": 0,
        "skipped": 0,
        "errors": 0,
    }

    async for session in acquire_org_async_session(org_id):
        # Get internal CRM integration
        setup = InternalCRMSetupService(session, org_id, SYSTEM_USER_ID)
        integration = await setup.get_internal_integration()

        if not integration:
            print("  No internal CRM integration found, skipping")
            return stats

        # Get internal CRM deal IDs
        int_id = integration.integration_id
        deal_ids_stmt = select(col(CRMDeal.deal_id)).where(
            col(CRMDeal.organisation_id) == org_id,
            col(CRMDeal.integration_id) == int_id,
        )
        deal_result = await session.execute(deal_ids_stmt)
        internal_deal_ids = [row[0] for row in deal_result.all()]

        if not internal_deal_ids:
            print("  No internal CRM deals found, skipping")
            return stats

        print(f"  Found {len(internal_deal_ids)} internal CRM deal(s)")

        # Find orphaned stakeholders on internal CRM deals only
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == org_id,
            col(DealStakeholder.deal_id).in_(internal_deal_ids),
            col(DealStakeholder.contact_id).is_(None),
            col(DealStakeholder.is_deleted).is_(False),
        )
        result = await session.execute(stmt)
        orphans = list(result.scalars().all())

        if not orphans:
            print("  No orphaned stakeholders found")
            return stats

        print(f"  Found {len(orphans)} orphaned stakeholder(s)")

        for stakeholder in orphans:
            if not stakeholder.email:
                stats["skipped"] += 1
                continue

            email = stakeholder.email.lower().strip()

            # Skip personal email providers and internal domains
            if _is_personal_email(email):
                stats["skipped"] += 1
                continue

            # Look up existing contact by email
            contact_stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == org_id,
                col(CRMContact.email) == email,
            )
            contact_result = await session.execute(contact_stmt)
            contact = contact_result.scalars().first()

            resolved_contact_id: UUID | None = None

            if contact:
                stakeholder.contact_id = contact.contact_id
                resolved_contact_id = contact.contact_id
                stats["linked"] += 1
                print(f"    Linked: {email} -> contact {contact.contact_id}")
            elif stakeholder.source == StakeholderSource.MANUAL:
                if not integration:
                    print(f"    Skip (no internal CRM): {email}")
                    stats["skipped"] += 1
                    continue

                contact_id = uuid4()
                now = datetime.now(UTC)
                new_contact = CRMContact(
                    contact_id=contact_id,
                    organisation_id=org_id,
                    integration_id=integration.integration_id,
                    external_id=str(contact_id),
                    email=email,
                    full_name=stakeholder.name,
                    job_title=stakeholder.job_title,
                    phone=stakeholder.phone_number,
                    synced_at=now,
                    last_seen_at=now,
                )
                session.add(new_contact)
                await session.flush()
                stakeholder.contact_id = new_contact.contact_id
                resolved_contact_id = new_contact.contact_id
                stats["created"] += 1
                print(f"    Created: {email} -> contact {new_contact.contact_id}")
            else:
                # Discovery/AI source — don't create, just skip
                stats["skipped"] += 1

            # Create CRMDealParticipant for MANUAL stakeholders on internal CRM deals
            if (
                resolved_contact_id
                and integration
                and stakeholder.source == StakeholderSource.MANUAL
            ):
                created = await _ensure_participant(
                    session,
                    org_id,
                    integration.integration_id,
                    stakeholder,
                    resolved_contact_id,
                    email,
                )
                if created:
                    stats["participants_created"] += 1
                    print(f"    Participant: {email} -> deal {stakeholder.deal_id}")

        # Second pass: MANUAL stakeholders on internal CRM deals that already have
        # contact_id but are missing CRMDealParticipant (from previous backfill runs)
        linked_stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == org_id,
            col(DealStakeholder.deal_id).in_(internal_deal_ids),
            col(DealStakeholder.contact_id).is_not(None),
            col(DealStakeholder.source) == StakeholderSource.MANUAL,
            col(DealStakeholder.is_deleted).is_(False),
        )
        linked_result = await session.execute(linked_stmt)
        linked_stakeholders = list(linked_result.scalars().all())

        for stakeholder in linked_stakeholders:
            if not stakeholder.email or not stakeholder.contact_id:
                continue
            email = stakeholder.email.lower().strip()
            created = await _ensure_participant(
                session,
                org_id,
                int_id,
                stakeholder,
                stakeholder.contact_id,
                email,
            )
            if created:
                stats["participants_created"] += 1
                print(f"    Participant (existing contact): {email} -> deal {stakeholder.deal_id}")

        if not dry_run:
            await session.commit()
            print("  Committed changes")
        else:
            await session.rollback()
            print("  DRY RUN — rolled back, no changes persisted")

    return stats


async def main_async(org_ids: list[UUID], dry_run: bool) -> None:
    totals = {
        "linked": 0,
        "created": 0,
        "participants_created": 0,
        "skipped": 0,
        "errors": 0,
    }

    for org_id in org_ids:
        print(f"\n[ORG] {org_id}")
        try:
            org_stats = await backfill_org(org_id, dry_run)
            for k in totals:
                totals[k] += org_stats[k]
        except Exception as exc:
            totals["errors"] += 1
            print(f"  ERROR: {exc}")

    print(
        f"\n[SUMMARY] linked={totals['linked']} created={totals['created']} "
        f"participants={totals['participants_created']} "
        f"skipped={totals['skipped']} errors={totals['errors']}"
    )


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.all and not args.org_id:
        print("Specify --all or --org-id <UUID>")
        return

    org_ids = load_org_ids(args.org_id, args.all)
    if not org_ids:
        print("No organisations found.")
        return

    print(f"Processing {len(org_ids)} organisation(s){'  [DRY RUN]' if args.dry_run else ''}")
    asyncio.run(main_async(org_ids, args.dry_run))


if __name__ == "__main__":
    main()
