"""Backfill participant-backed stakeholder state from legacy deal stakeholders.

The default mode is a read-only dry run. `--apply` updates canonical
`CRMContact` and `CRMDealParticipant` rows or creates app-owned contact
participants only when no matching participant exists. Legacy `deal_stakeholders`
rows are never deleted or mutated by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from dotenv import load_dotenv
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectMapping,
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_master_session

from scripts.org_admin_session import acquire_org_admin_async_session

APPLY_COMMIT_BATCH_SIZE = 500


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
    primary_contact_flags_updated: int = 0
    single_crm_contact_primary_flags_updated: int = 0
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
            "primary_contact_flags_updated": self.primary_contact_flags_updated,
            "single_crm_contact_primary_flags_updated": (
                self.single_crm_contact_primary_flags_updated
            ),
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


@dataclass
class ParticipantLookup:
    by_source_reference: dict[tuple[str, str], list[Any]]
    by_contact_id: dict[tuple[str, str], list[Any]]
    by_email: dict[tuple[str, str], list[Any]]
    by_identity: dict[tuple[str, str], list[Any]]


@dataclass
class ContactLookup:
    by_contact_id: dict[str, Any]
    by_email: dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill CRMDealParticipant stakeholder fields from deal_stakeholders.",
    )
    parser.add_argument("--organisation-id", type=UUID)
    parser.add_argument("--all", action="store_true", help="Process all approved organisations.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    return parser


def load_organisation_ids(organisation_id: UUID | None, run_all: bool) -> list[UUID]:
    session_gen = acquire_master_session()
    session = next(session_gen)
    try:
        if run_all:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) != OrganisationStatus.FAILED
            )
        elif organisation_id:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.organisation_id) == organisation_id
            )
        else:
            return []
        return [row[0] for row in session.execute(stmt).all()]
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _set_value(row: Any, key: str, value: Any) -> None:
    if isinstance(row, dict):
        row[key] = value
    else:
        setattr(row, key, value)


def _mark_modified(row: Any, key: str) -> None:
    if isinstance(row, dict):
        return
    with contextlib.suppress(Exception):
        flag_modified(row, key)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _is_crm_source(stakeholder: Any) -> bool:
    return _enum_value(_value(stakeholder, "source")) == "CRM"


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


def _id_key(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


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


def build_participant_lookup(participants: list[Any]) -> ParticipantLookup:
    lookup = ParticipantLookup(
        by_source_reference={},
        by_contact_id={},
        by_email={},
        by_identity={},
    )
    for participant in participants:
        add_participant_to_lookup(lookup, participant)
    return lookup


def add_participant_to_lookup(lookup: ParticipantLookup, participant: Any) -> None:
    deal_id = _id_key(_value(participant, "deal_id"))
    if not deal_id:
        return

    participant_id = _id_key(_value(participant, "participant_id"))
    if participant_id:
        lookup.by_source_reference.setdefault((deal_id, participant_id), []).append(participant)

    related_local_id = _id_key(_value(participant, "related_local_id"))
    if related_local_id:
        lookup.by_contact_id.setdefault((deal_id, related_local_id), []).append(participant)
        lookup.by_identity.setdefault((deal_id, related_local_id), []).append(participant)

    email = normalize_email(_value(participant, "related_email"))
    if email:
        lookup.by_email.setdefault((deal_id, email), []).append(participant)


def choose_participant_match_for_stakeholder(
    stakeholder: Any,
    participants: list[Any],
    lookup: ParticipantLookup | None = None,
) -> ParticipantMatch:
    participant_lookup = lookup or build_participant_lookup(participants)
    deal_id = _id_key(_value(stakeholder, "deal_id"))
    if not deal_id:
        return ParticipantMatch(None, None)

    source_reference_id = _value(stakeholder, "source_reference_id")
    if source_reference_id:
        matches = participant_lookup.by_source_reference.get(
            (deal_id, str(source_reference_id)),
            [],
        )
        if matches:
            return ParticipantMatch(choose_canonical_participant(matches), "source_reference")

    contact_id = _value(stakeholder, "contact_id")
    if contact_id:
        matches = participant_lookup.by_contact_id.get((deal_id, str(contact_id)), [])
        if matches:
            return ParticipantMatch(choose_canonical_participant(matches), "contact_id")

    stakeholder_email = normalize_email(_value(stakeholder, "email"))
    if stakeholder_email:
        matches = participant_lookup.by_email.get((deal_id, stakeholder_email), [])
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
        _mark_modified(contact, "properties")
        changed = True
    external_links = _value(stakeholder, "external_links")
    if external_links and not properties.get("external_links"):
        properties["external_links"] = external_links
        _mark_modified(contact, "properties")
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


def _participants_for_identity(
    participant: CRMDealParticipant,
    participants: list[CRMDealParticipant],
    lookup: ParticipantLookup | None,
) -> list[CRMDealParticipant]:
    related_local_id = _id_key(_value(participant, "related_local_id"))
    deal_id = _id_key(_value(participant, "deal_id"))
    if not related_local_id or not deal_id:
        return []
    if lookup:
        return list(lookup.by_identity.get((deal_id, related_local_id), []))
    return [
        candidate
        for candidate in participants
        if _same_id(_value(candidate, "deal_id"), deal_id)
        and _same_id(_value(candidate, "related_local_id"), related_local_id)
    ]


def _duplicate_participants_for_canonical(
    canonical: CRMDealParticipant,
    participants: list[CRMDealParticipant],
    lookup: ParticipantLookup | None = None,
) -> list[CRMDealParticipant]:
    if canonical.related_local_id is None:
        return []
    return [
        participant
        for participant in _participants_for_identity(canonical, participants, lookup)
        if participant.participant_id != canonical.participant_id
    ]


def _canonical_for_participant_identity(
    participant: CRMDealParticipant,
    participants: list[CRMDealParticipant],
    lookup: ParticipantLookup | None = None,
) -> CRMDealParticipant:
    if participant.related_local_id is None:
        return participant
    matches = [
        candidate
        for candidate in _participants_for_identity(participant, participants, lookup)
        if not candidate.is_deleted
    ]
    return choose_canonical_participant(matches) or participant


async def _repoint_duplicate_participant_references(
    session: Any,
    organisation_id: UUID,
    *,
    canonical: CRMDealParticipant,
    duplicates: list[CRMDealParticipant],
    apply: bool,
) -> int:
    duplicate_ids = [participant.participant_id for participant in duplicates]
    if not duplicate_ids:
        return 0

    stmt = select(CRMSourceObjectMapping).where(
        col(CRMSourceObjectMapping.organisation_id) == organisation_id,
        col(CRMSourceObjectMapping.local_object_type) == CRMSourceObjectType.DEAL_PARTICIPANT,
        col(CRMSourceObjectMapping.local_object_id).in_(duplicate_ids),
    )
    mappings = list((await session.execute(stmt)).scalars().all())
    if apply:
        for mapping in mappings:
            mapping.local_object_id = canonical.participant_id
            session.add(mapping)
        for duplicate in duplicates:
            duplicate.is_deleted = True
            session.add(duplicate)
    return len(duplicates)


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


def _contact_lookup_values_for_stakeholders(
    stakeholders: list[DealStakeholder],
) -> tuple[set[UUID], set[str]]:
    contact_ids: set[UUID] = set()
    emails: set[str] = set()
    for stakeholder in stakeholders:
        contact_id = _value(stakeholder, "contact_id")
        if contact_id:
            parsed_contact_id = _uuid_or_none(contact_id)
            if parsed_contact_id:
                contact_ids.add(parsed_contact_id)

        email = normalize_email(_value(stakeholder, "email"))
        if email:
            emails.add(email)
    return contact_ids, emails


async def _load_contacts(
    session: Any,
    organisation_id: UUID,
    stakeholders: list[DealStakeholder],
) -> list[CRMContact]:
    contact_ids, emails = _contact_lookup_values_for_stakeholders(stakeholders)
    lookup_conditions = []
    if contact_ids:
        lookup_conditions.append(col(CRMContact.contact_id).in_(contact_ids))
    if emails:
        lookup_conditions.append(func.lower(func.trim(col(CRMContact.email))).in_(emails))

    if not lookup_conditions:
        return []

    stmt = select(CRMContact).where(
        col(CRMContact.organisation_id) == organisation_id,
        col(CRMContact.is_deleted).is_(False),
        or_(*lookup_conditions),
    )
    return list((await session.execute(stmt)).scalars().all())


PRIMARY_CONTACT_PARTICIPANT_COUNT_SQL = """
SELECT count(*)
FROM crm_deal_participants AS participants
JOIN crm_deals AS deals
  ON deals.organisation_id = participants.organisation_id
 AND deals.deal_id = participants.deal_id
WHERE participants.organisation_id = :organisation_id
  AND participants.participant_type = 'contact'
  AND participants.is_deleted = false
  AND participants.related_local_id = deals.primary_contact_id
  AND deals.primary_contact_id IS NOT NULL
  AND deals.is_deleted = false
  AND participants.is_primary_contact = false
"""


PRIMARY_CONTACT_PARTICIPANT_UPDATE_SQL = """
UPDATE crm_deal_participants AS participants
SET is_primary_contact = true
FROM crm_deals AS deals
WHERE participants.organisation_id = :organisation_id
  AND deals.organisation_id = participants.organisation_id
  AND deals.deal_id = participants.deal_id
  AND participants.participant_type = 'contact'
  AND participants.is_deleted = false
  AND participants.related_local_id = deals.primary_contact_id
  AND deals.primary_contact_id IS NOT NULL
  AND deals.is_deleted = false
  AND participants.is_primary_contact = false
RETURNING participants.participant_id
"""


SINGLE_CRM_CONTACT_PRIMARY_COUNT_SQL = """
WITH single_crm_contact_deals AS (
    SELECT participants.organisation_id, participants.deal_id
    FROM crm_deal_participants AS participants
    JOIN crm_deals AS deals
      ON deals.organisation_id = participants.organisation_id
     AND deals.deal_id = participants.deal_id
    WHERE participants.organisation_id = :organisation_id
      AND participants.participant_type = 'contact'
      AND participants.participant_source = 'CRM'
      AND participants.is_deleted = false
      AND deals.is_deleted = false
    GROUP BY participants.organisation_id, participants.deal_id
    HAVING count(*) = 1
       AND bool_or(participants.is_primary_contact) = false
)
SELECT count(*)
FROM crm_deal_participants AS participants
JOIN single_crm_contact_deals AS single_deals
  ON single_deals.organisation_id = participants.organisation_id
 AND single_deals.deal_id = participants.deal_id
WHERE participants.organisation_id = :organisation_id
  AND participants.participant_type = 'contact'
  AND participants.participant_source = 'CRM'
  AND participants.is_deleted = false
  AND participants.is_primary_contact = false
"""


SINGLE_CRM_CONTACT_PRIMARY_UPDATE_SQL = """
WITH single_crm_contact_deals AS (
    SELECT participants.organisation_id, participants.deal_id
    FROM crm_deal_participants AS participants
    JOIN crm_deals AS deals
      ON deals.organisation_id = participants.organisation_id
     AND deals.deal_id = participants.deal_id
    WHERE participants.organisation_id = :organisation_id
      AND participants.participant_type = 'contact'
      AND participants.participant_source = 'CRM'
      AND participants.is_deleted = false
      AND deals.is_deleted = false
    GROUP BY participants.organisation_id, participants.deal_id
    HAVING count(*) = 1
       AND bool_or(participants.is_primary_contact) = false
)
UPDATE crm_deal_participants AS participants
SET is_primary_contact = true
FROM single_crm_contact_deals AS single_deals
WHERE participants.organisation_id = :organisation_id
  AND single_deals.organisation_id = participants.organisation_id
  AND single_deals.deal_id = participants.deal_id
  AND participants.participant_type = 'contact'
  AND participants.participant_source = 'CRM'
  AND participants.is_deleted = false
  AND participants.is_primary_contact = false
RETURNING participants.participant_id
"""


async def sync_primary_contact_flags_from_deals(
    session: Any,
    organisation_id: UUID,
    *,
    apply: bool,
) -> int:
    if not apply:
        count = (
            await session.execute(
                text(PRIMARY_CONTACT_PARTICIPANT_COUNT_SQL),
                {"organisation_id": organisation_id},
            )
        ).scalar_one()
        return int(count)

    rows = (
        await session.execute(
            text(PRIMARY_CONTACT_PARTICIPANT_UPDATE_SQL),
            {"organisation_id": organisation_id},
        )
    ).all()
    return len(rows)


async def infer_primary_contact_flags_from_single_crm_contact(
    session: Any,
    organisation_id: UUID,
    *,
    apply: bool,
) -> int:
    if not apply:
        count = (
            await session.execute(
                text(SINGLE_CRM_CONTACT_PRIMARY_COUNT_SQL),
                {"organisation_id": organisation_id},
            )
        ).scalar_one()
        return int(count)

    rows = (
        await session.execute(
            text(SINGLE_CRM_CONTACT_PRIMARY_UPDATE_SQL),
            {"organisation_id": organisation_id},
        )
    ).all()
    return len(rows)


def build_contact_lookup(contacts: list[Any]) -> ContactLookup:
    lookup = ContactLookup(by_contact_id={}, by_email={})
    for contact in contacts:
        add_contact_to_lookup(lookup, contact)
    return lookup


def add_contact_to_lookup(lookup: ContactLookup, contact: Any) -> None:
    contact_id = _id_key(_value(contact, "contact_id"))
    if contact_id:
        lookup.by_contact_id.setdefault(contact_id, contact)

    email = normalize_email(_value(contact, "email"))
    if email:
        lookup.by_email.setdefault(email, contact)


def choose_contact_for_stakeholder(
    stakeholder: Any,
    contacts: list[Any],
    lookup: ContactLookup | None = None,
) -> Any | None:
    contact_lookup = lookup or build_contact_lookup(contacts)
    contact_id = _value(stakeholder, "contact_id")
    if contact_id:
        contact = contact_lookup.by_contact_id.get(str(contact_id))
        if contact:
            return contact

    stakeholder_email = normalize_email(_value(stakeholder, "email"))
    if not stakeholder_email:
        return None
    return contact_lookup.by_email.get(stakeholder_email)


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
        contacts = await _load_contacts(session, organisation_id, stakeholders)
        participant_lookup = build_participant_lookup(participants)
        contact_lookup = build_contact_lookup(contacts)
        duplicate_ids_processed: set[UUID] = set()
        stats.primary_contact_flags_updated = await sync_primary_contact_flags_from_deals(
            session,
            organisation_id,
            apply=apply,
        )
        stats.single_crm_contact_primary_flags_updated = (
            await infer_primary_contact_flags_from_single_crm_contact(
                session,
                organisation_id,
                apply=apply,
            )
        )
        if apply:
            await session.commit()

        for stakeholder in stakeholders:
            stats.stakeholders_scanned += 1
            email_normalized = normalize_email(stakeholder.email)
            if not email_normalized:
                stats.stakeholders_without_email += 1

            match = choose_participant_match_for_stakeholder(
                stakeholder,
                participants,
                participant_lookup,
            )
            participant = match.participant
            contact = None
            if participant is not None:
                participant = _canonical_for_participant_identity(
                    participant,
                    participants,
                    participant_lookup,
                )
                if match.match_type == "source_reference":
                    stats.participants_matched_by_source_reference += 1
                elif match.match_type == "contact_id":
                    stats.participants_matched_by_contact_id += 1
                elif match.match_type == "email":
                    stats.participants_matched_by_email += 1
                contact = choose_contact_for_stakeholder(stakeholder, contacts, contact_lookup)
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
                    add_contact_to_lookup(contact_lookup, contact)
            elif email_normalized:
                if _is_crm_source(stakeholder):
                    stats.conflicts += 1
                    continue

                integration_id = await _resolve_integration_id(
                    session,
                    organisation_id,
                    stakeholder.deal_id,
                )
                if not integration_id:
                    stats.conflicts += 1
                    continue
                contact = choose_contact_for_stakeholder(stakeholder, contacts, contact_lookup)
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
                    add_contact_to_lookup(contact_lookup, contact)

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
                add_participant_to_lookup(participant_lookup, participant)
            else:
                stats.conflicts += 1
                continue

            stats.canonical_rows_selected += 1
            if contact:
                contact_changed = fill_contact_profile_from_stakeholder(contact, stakeholder)
                if contact_changed:
                    stats.contact_profile_fields_filled += 1
                research_changed = copy_stakeholder_research_to_contact(contact, stakeholder)
                if research_changed:
                    stats.contact_research_fields_updated += 1
                if apply and (contact_changed or research_changed):
                    session.add(contact)

                snapshot_changed = refresh_participant_profile_snapshot_from_contact(
                    participant,
                    contact,
                )
                if snapshot_changed:
                    stats.participant_profile_snapshots_updated += 1
                if apply and snapshot_changed:
                    session.add(participant)

            behavior_changed = copy_stakeholder_behavior_to_participant(participant, stakeholder)
            if behavior_changed:
                stats.participants_updated += 1
                if apply:
                    session.add(participant)

            duplicates = [
                duplicate
                for duplicate in _duplicate_participants_for_canonical(
                    participant,
                    participants,
                    participant_lookup,
                )
                if duplicate.participant_id not in duplicate_ids_processed
            ]
            if duplicates:
                stats.duplicate_rows_pointed_to_canonical += (
                    await _repoint_duplicate_participant_references(
                        session,
                        organisation_id,
                        canonical=participant,
                        duplicates=duplicates,
                        apply=apply,
                    )
                )
                duplicate_ids_processed.update(duplicate.participant_id for duplicate in duplicates)

            if apply and stats.stakeholders_scanned % APPLY_COMMIT_BATCH_SIZE == 0:
                await session.commit()

        if apply:
            await session.commit()
        else:
            await session.rollback()

    return stats


def print_stats(stats: BackfillStats) -> None:
    for key, value in stats.as_dict().items():
        print(f"{key}={value}")


def _merge_stats(total: BackfillStats, item: BackfillStats) -> BackfillStats:
    for key, value in item.as_dict().items():
        setattr(total, key, getattr(total, key) + value)
    return total


async def run(args: argparse.Namespace) -> BackfillStats:
    if args.apply == args.dry_run:
        raise SystemExit("Pass exactly one of --dry-run or --apply.")
    if args.all == bool(args.organisation_id):
        raise SystemExit("Pass exactly one of --organisation-id or --all.")

    total = BackfillStats()
    for organisation_id in load_organisation_ids(args.organisation_id, args.all):
        print(f"organisation_id={organisation_id}")
        stats = await backfill_org(organisation_id, apply=args.apply, limit=args.limit)
        print_stats(stats)
        _merge_stats(total, stats)
    return total


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    stats = asyncio.run(run(args))
    print("total")
    print_stats(stats)


if __name__ == "__main__":
    main()
