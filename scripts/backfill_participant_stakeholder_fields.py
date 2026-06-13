"""Backfill participant-backed stakeholder state from legacy deal stakeholders.

The default mode is a read-only dry run. `--apply` updates canonical
`CRMContact` and `CRMDealParticipant` rows or creates app-owned contact
participants only when no matching participant exists. Legacy `deal_stakeholders`
rows are never deleted or mutated by this script.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from dotenv import load_dotenv
from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)

from scripts.org_admin_session import acquire_org_admin_async_session


@dataclass
class BackfillStats:
    stakeholders_scanned: int = 0
    participants_matched_by_source_reference: int = 0
    participants_matched_by_contact_id: int = 0
    participants_matched_by_email: int = 0
    contacts_matched: int = 0
    contacts_created: int = 0
    contact_profile_fields_filled: int = 0
    contact_research_fields_updated: int = 0
    participant_profile_snapshots_updated: int = 0
    participants_created: int = 0
    participants_updated: int = 0
    canonical_rows_selected: int = 0
    duplicate_rows_pointed_to_canonical: int = 0
    stakeholders_without_email: int = 0
    conflicts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "stakeholders_scanned": self.stakeholders_scanned,
            "participants_matched_by_source_reference": (
                self.participants_matched_by_source_reference
            ),
            "participants_matched_by_contact_id": self.participants_matched_by_contact_id,
            "participants_matched_by_email": self.participants_matched_by_email,
            "contacts_matched": self.contacts_matched,
            "contacts_created": self.contacts_created,
            "contact_profile_fields_filled": self.contact_profile_fields_filled,
            "contact_research_fields_updated": self.contact_research_fields_updated,
            "participant_profile_snapshots_updated": self.participant_profile_snapshots_updated,
            "participants_created": self.participants_created,
            "participants_updated": self.participants_updated,
            "canonical_rows_selected": self.canonical_rows_selected,
            "duplicate_rows_pointed_to_canonical": self.duplicate_rows_pointed_to_canonical,
            "stakeholders_without_email": self.stakeholders_without_email,
            "conflicts": self.conflicts,
        }


@dataclass(frozen=True)
class ParticipantMatch:
    participant: Any | None
    match_type: str | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill CRMDealParticipant stakeholder fields from deal_stakeholders.",
    )
    parser.add_argument("--organisation-id", required=True, type=UUID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    return parser


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _set_value(row: Any, key: str, value: Any) -> None:
    if isinstance(row, dict):
        row[key] = value
    else:
        setattr(row, key, value)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def normalize_email(email: Any) -> str | None:
    if not email:
        return None
    normalized = str(email).strip().lower()
    return normalized or None


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _same_id(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _same_deal(stakeholder: Any, participant: Any) -> bool:
    return _same_id(_value(stakeholder, "deal_id"), _value(participant, "deal_id"))


def _canonical_rank(participant: Any) -> tuple[int, int, int, str, str]:
    created_at = _value(participant, "created_at")
    role = _value(participant, "relationship_role")
    return (
        0 if role == "primary_contact" else 1,
        0 if _value(participant, "related_local_id") else 1,
        0 if created_at else 1,
        str(created_at or ""),
        str(_value(participant, "participant_id") or ""),
    )


def choose_canonical_participant(participants: list[Any]) -> Any | None:
    if not participants:
        return None
    return sorted(participants, key=_canonical_rank)[0]


def choose_participant_match_for_stakeholder(
    stakeholder: Any,
    participants: list[Any],
) -> ParticipantMatch:
    same_deal = [
        participant for participant in participants if _same_deal(stakeholder, participant)
    ]
    source_reference_id = _value(stakeholder, "source_reference_id")
    if source_reference_id:
        matches = [
            participant
            for participant in same_deal
            if _same_id(source_reference_id, _value(participant, "participant_id"))
        ]
        if matches:
            return ParticipantMatch(choose_canonical_participant(matches), "source_reference")

    contact_id = _value(stakeholder, "contact_id")
    if contact_id:
        matches = [
            participant
            for participant in same_deal
            if _same_id(contact_id, _value(participant, "related_local_id"))
        ]
        if matches:
            return ParticipantMatch(choose_canonical_participant(matches), "contact_id")

    stakeholder_email = normalize_email(_value(stakeholder, "email"))
    if stakeholder_email:
        matches = [
            participant
            for participant in same_deal
            if normalize_email(_value(participant, "related_email")) == stakeholder_email
        ]
        if matches:
            return ParticipantMatch(choose_canonical_participant(matches), "email")

    return ParticipantMatch(None, None)


def choose_participant_for_stakeholder(stakeholder: Any, participants: list[Any]) -> Any | None:
    return choose_participant_match_for_stakeholder(stakeholder, participants).participant


def _properties(row: Any) -> dict[str, Any]:
    properties = _value(row, "properties")
    if isinstance(properties, dict):
        return properties
    properties = {}
    _set_value(row, "properties", properties)
    return properties


def _set_if_missing(row: Any, key: str, value: Any) -> bool:
    if value is None:
        return False
    if key == "email":
        value = normalize_email(value)
        if value is None:
            return False
    existing = _value(row, key)
    if existing:
        return False
    _set_value(row, key, value)
    return True


def fill_contact_profile_from_stakeholder(contact: Any, stakeholder: Any) -> bool:
    changed = False
    changed |= _set_if_missing(contact, "full_name", _value(stakeholder, "name"))
    changed |= _set_if_missing(contact, "email", _value(stakeholder, "email"))
    changed |= _set_if_missing(contact, "job_title", _value(stakeholder, "job_title"))
    changed |= _set_if_missing(contact, "phone", _value(stakeholder, "phone_number"))

    properties = _properties(contact)
    department = _value(stakeholder, "department")
    if department and not properties.get("department"):
        properties["department"] = department
        changed = True
    external_links = _value(stakeholder, "external_links")
    if external_links and not properties.get("external_links"):
        properties["external_links"] = external_links
        changed = True
    return changed


def refresh_participant_profile_snapshot_from_contact(participant: Any, contact: Any) -> bool:
    snapshot_values = {
        "related_local_id": _value(contact, "contact_id"),
        "related_name": _value(contact, "full_name"),
        "related_email": normalize_email(_value(contact, "email")),
        "job_title": _value(contact, "job_title"),
        "phone_number": _value(contact, "phone") or _value(contact, "mobile_phone"),
        "department": _properties(contact).get("department"),
        "external_links": _properties(contact).get("external_links"),
    }
    changed = False
    for field_name, value in snapshot_values.items():
        if value is None:
            continue
        if _value(participant, field_name) != value:
            _set_value(participant, field_name, value)
            changed = True
    return changed


def copy_stakeholder_research_to_contact(contact: Any, stakeholder: Any) -> bool:
    research = _value(stakeholder, "stakeholder_research")
    if not research:
        return False

    incoming_researched_at = _value(stakeholder, "last_researched_at")
    existing_research = _value(contact, "contact_research")
    existing_researched_at = _value(contact, "last_researched_at")
    if existing_research and (
        incoming_researched_at is None
        or (existing_researched_at is not None and existing_researched_at >= incoming_researched_at)
    ):
        return False

    changed = False
    if existing_research != research:
        _set_value(contact, "contact_research", research)
        changed = True
    if incoming_researched_at is not None and existing_researched_at != incoming_researched_at:
        _set_value(contact, "last_researched_at", incoming_researched_at)
        changed = True
    return changed


def copy_stakeholder_behavior_to_participant(
    participant: Any,
    stakeholder: Any,
) -> bool:
    updates = {
        "created_by_user_id": _value(stakeholder, "created_by_user_id"),
        "stance": _enum_value(_value(stakeholder, "stance")),
        "decision_power": _enum_value(_value(stakeholder, "decision_power")),
        "motivation": _value(stakeholder, "motivation"),
        "pain_points": _value(stakeholder, "pain_points"),
        "keywords": _value(stakeholder, "keywords"),
        "last_contact_at": _value(stakeholder, "last_contact_at"),
        "temperature": _enum_value(_value(stakeholder, "temperature")),
        "participant_source": _enum_value(_value(stakeholder, "source")),
        "source_reference_id": _value(stakeholder, "source_reference_id"),
        "approval_status": _enum_value(_value(stakeholder, "approval_status")),
        "stakeholder_research": _value(stakeholder, "stakeholder_research"),
        "last_researched_at": _value(stakeholder, "last_researched_at"),
    }
    changed = False
    for field_name, value in updates.items():
        if value is None:
            continue
        if _value(participant, field_name) != value:
            _set_value(participant, field_name, value)
            changed = True
    return changed


def _duplicate_participants_for_canonical(
    canonical: CRMDealParticipant,
    participants: list[CRMDealParticipant],
) -> list[CRMDealParticipant]:
    canonical_email = normalize_email(canonical.related_email)
    return [
        participant
        for participant in participants
        if participant.participant_id != canonical.participant_id
        and participant.deal_id == canonical.deal_id
        and (
            (
                canonical.related_local_id is not None
                and participant.related_local_id == canonical.related_local_id
            )
            or (
                canonical.related_local_id is None
                and canonical_email is not None
                and normalize_email(participant.related_email) == canonical_email
            )
        )
    ]


async def _load_stakeholders(
    session: Any,
    organisation_id: UUID,
    *,
    limit: int | None,
) -> list[DealStakeholder]:
    stmt = (
        select(DealStakeholder)
        .where(
            col(DealStakeholder.organisation_id) == organisation_id,
            col(DealStakeholder.is_deleted).is_(False),
        )
        .order_by(col(DealStakeholder.deal_id), col(DealStakeholder.created_at))
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def _load_participants(session: Any, organisation_id: UUID) -> list[CRMDealParticipant]:
    stmt = select(CRMDealParticipant).where(
        col(CRMDealParticipant.organisation_id) == organisation_id,
        col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
        col(CRMDealParticipant.is_deleted).is_(False),
    )
    return list((await session.execute(stmt)).scalars().all())


async def _load_contacts(session: Any, organisation_id: UUID) -> list[CRMContact]:
    stmt = select(CRMContact).where(
        col(CRMContact.organisation_id) == organisation_id,
        col(CRMContact.is_deleted).is_(False),
    )
    return list((await session.execute(stmt)).scalars().all())


def choose_contact_for_stakeholder(stakeholder: Any, contacts: list[Any]) -> Any | None:
    contact_id = _value(stakeholder, "contact_id")
    if contact_id:
        for contact in contacts:
            if _same_id(contact_id, _value(contact, "contact_id")):
                return contact

    stakeholder_email = normalize_email(_value(stakeholder, "email"))
    if not stakeholder_email:
        return None
    for contact in contacts:
        if normalize_email(_value(contact, "email")) == stakeholder_email:
            return contact
    return None


async def _resolve_integration_id(
    session: Any,
    organisation_id: UUID,
    deal_id: UUID,
) -> UUID | None:
    internal_stmt = select(col(KnowledgeIntegration.integration_id)).where(
        col(KnowledgeIntegration.organisation_id) == organisation_id,
        col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        col(KnowledgeIntegration.is_active).is_(True),
    )
    internal = (await session.execute(internal_stmt)).scalars().first()
    if internal:
        return _uuid_or_none(internal)

    deal_stmt = select(col(CRMDeal.integration_id)).where(
        col(CRMDeal.organisation_id) == organisation_id,
        col(CRMDeal.deal_id) == deal_id,
        col(CRMDeal.is_deleted).is_(False),
    )
    return _uuid_or_none((await session.execute(deal_stmt)).scalars().first())


def _new_contact_from_stakeholder(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    stakeholder: DealStakeholder,
    email_normalized: str,
    contact_id: UUID | None = None,
) -> CRMContact:
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=integration_id,
        contact_id=contact_id or _value(stakeholder, "contact_id") or uuid4(),
        full_name=_value(stakeholder, "name"),
        email=email_normalized,
        job_title=_value(stakeholder, "job_title"),
        phone=_value(stakeholder, "phone_number"),
        properties={},
    )
    fill_contact_profile_from_stakeholder(contact, stakeholder)
    return contact


def _new_app_owned_participant(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    stakeholder: DealStakeholder,
    email_normalized: str,
    contact: CRMContact | None,
) -> CRMDealParticipant:
    participant = CRMDealParticipant(
        participant_id=stakeholder.stakeholder_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=stakeholder.deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact.contact_id if contact else stakeholder.contact_id,
        related_name=contact.full_name if contact else stakeholder.name,
        related_email=normalize_email(contact.email) if contact and contact.email else email_normalized,
        participant_source=_enum_value(stakeholder.source),
        source_reference_id=stakeholder.source_reference_id,
        approval_status=_enum_value(stakeholder.approval_status),
    )
    if contact:
        refresh_participant_profile_snapshot_from_contact(participant, contact)
    return participant


async def backfill_org(
    organisation_id: UUID,
    *,
    apply: bool,
    limit: int | None,
) -> BackfillStats:
    stats = BackfillStats()
    async with acquire_org_admin_async_session(organisation_id) as session:
        stakeholders = await _load_stakeholders(session, organisation_id, limit=limit)
        participants = await _load_participants(session, organisation_id)
        contacts = await _load_contacts(session, organisation_id)

        for stakeholder in stakeholders:
            stats.stakeholders_scanned += 1
            email_normalized = normalize_email(stakeholder.email)
            if not email_normalized:
                stats.stakeholders_without_email += 1

            match = choose_participant_match_for_stakeholder(stakeholder, participants)
            participant = match.participant
            contact = None
            if participant is not None:
                if match.match_type == "source_reference":
                    stats.participants_matched_by_source_reference += 1
                elif match.match_type == "contact_id":
                    stats.participants_matched_by_contact_id += 1
                elif match.match_type == "email":
                    stats.participants_matched_by_email += 1
                contact = choose_contact_for_stakeholder(stakeholder, contacts)
                if contact:
                    stats.contacts_matched += 1
                elif email_normalized:
                    integration_id = participant.integration_id or await _resolve_integration_id(
                        session,
                        organisation_id,
                        stakeholder.deal_id,
                    )
                    if not integration_id:
                        stats.conflicts += 1
                        continue
                    contact = _new_contact_from_stakeholder(
                        organisation_id=organisation_id,
                        integration_id=integration_id,
                        stakeholder=stakeholder,
                        email_normalized=email_normalized,
                        contact_id=_value(participant, "related_local_id"),
                    )
                    stats.contacts_created += 1
                    if apply:
                        session.add(contact)
                        await session.flush()
                    contacts.append(contact)
            elif email_normalized:
                integration_id = await _resolve_integration_id(
                    session,
                    organisation_id,
                    stakeholder.deal_id,
                )
                if not integration_id:
                    stats.conflicts += 1
                    continue
                contact = choose_contact_for_stakeholder(stakeholder, contacts)
                if contact:
                    stats.contacts_matched += 1
                elif email_normalized:
                    contact = _new_contact_from_stakeholder(
                        organisation_id=organisation_id,
                        integration_id=integration_id,
                        stakeholder=stakeholder,
                        email_normalized=email_normalized,
                    )
                    stats.contacts_created += 1
                    if apply:
                        session.add(contact)
                        await session.flush()
                    contacts.append(contact)

                participant = _new_app_owned_participant(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    stakeholder=stakeholder,
                    email_normalized=email_normalized,
                    contact=contact,
                )
                stats.participants_created += 1
                if apply:
                    session.add(participant)
                    await session.flush()
                participants.append(participant)
            else:
                stats.conflicts += 1
                continue

            stats.canonical_rows_selected += 1
            if contact:
                if fill_contact_profile_from_stakeholder(contact, stakeholder):
                    stats.contact_profile_fields_filled += 1
                if copy_stakeholder_research_to_contact(contact, stakeholder):
                    stats.contact_research_fields_updated += 1
                if refresh_participant_profile_snapshot_from_contact(participant, contact):
                    stats.participant_profile_snapshots_updated += 1

            if apply:
                if copy_stakeholder_behavior_to_participant(participant, stakeholder):
                    stats.participants_updated += 1
            else:
                stats.participants_updated += 1

            duplicates = _duplicate_participants_for_canonical(participant, participants)
            stats.duplicate_rows_pointed_to_canonical += len(duplicates)

        if apply:
            await session.commit()
        else:
            await session.rollback()

    return stats


def print_stats(stats: BackfillStats) -> None:
    for key, value in stats.as_dict().items():
        print(f"{key}={value}")


async def run(args: argparse.Namespace) -> BackfillStats:
    if args.apply == args.dry_run:
        raise SystemExit("Pass exactly one of --dry-run or --apply.")
    return await backfill_org(args.organisation_id, apply=args.apply, limit=args.limit)


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    stats = asyncio.run(run(args))
    print_stats(stats)


if __name__ == "__main__":
    main()
