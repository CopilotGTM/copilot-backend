"""Salesforce OpportunityContactRole + Account link -> participants and associations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from src.domain.models.organisation_tables.sync.crm.associations import (
    CRMAssociationCategory,
    CRMObjectType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DealParticipantExtraction,
    NormalizedSalesforceDealAssociation,
    NormalizedSalesforceDealParticipant,
)

logger = get_logger(__name__)


def extract_participants(
    raw_contact_roles: Iterable[dict[str, Any]],
    *,
    company_external_id_by_deal: Mapping[str, str | None],
) -> DealParticipantExtraction:
    participants: list[NormalizedSalesforceDealParticipant] = []
    associations: list[NormalizedSalesforceDealAssociation] = []
    primary_contact_by_deal: dict[str, str] = {}
    contact_ids_by_deal: dict[str, set[str]] = defaultdict(set)
    skipped_malformed = 0

    for raw in raw_contact_roles:
        if not isinstance(raw, dict):
            skipped_malformed += 1
            continue
        deal_external_id = _coerce_str(raw.get("OpportunityId"))
        contact_external_id = _coerce_str(raw.get("ContactId"))
        if deal_external_id is None or contact_external_id is None:
            skipped_malformed += 1
            continue

        role_label = _coerce_str(raw.get("Role"))
        is_primary = _coerce_bool(raw.get("IsPrimary"))
        contact_ids_by_deal[deal_external_id].add(contact_external_id)
        if is_primary:
            primary_contact_by_deal.setdefault(deal_external_id, contact_external_id)

        if role_label is not None or not is_primary:
            participants.append(
                NormalizedSalesforceDealParticipant(
                    deal_external_id=deal_external_id,
                    participant_type=CRMParticipantType.CONTACT.value,
                    related_external_id=contact_external_id,
                    relationship_role=role_label,
                    is_primary=is_primary,
                    data_payload=raw,
                )
            )
        if is_primary:
            participants.append(
                NormalizedSalesforceDealParticipant(
                    deal_external_id=deal_external_id,
                    participant_type=CRMParticipantType.CONTACT.value,
                    related_external_id=contact_external_id,
                    relationship_role="primary_contact",
                    is_primary=True,
                    data_payload=raw,
                )
            )

        associations.append(
            NormalizedSalesforceDealAssociation(
                from_external_id=deal_external_id,
                from_object_type=CRMObjectType.DEAL.value,
                to_external_id=contact_external_id,
                to_object_type=CRMObjectType.CONTACT.value,
                association_category=CRMAssociationCategory.SALESFORCE_STANDARD.value,
                association_type_id="OpportunityContactRole",
                association_label="Opportunity Contact Role",
                role_label=role_label,
                is_primary=is_primary,
                data_payload=raw,
            )
        )

    for deal_external_id, company_external_id in company_external_id_by_deal.items():
        if company_external_id is None:
            continue
        payload = {
            "source": "Opportunity.AccountId",
            "opportunity_id": deal_external_id,
            "account_id": company_external_id,
        }
        participants.append(
            NormalizedSalesforceDealParticipant(
                deal_external_id=deal_external_id,
                participant_type=CRMParticipantType.COMPANY.value,
                related_external_id=company_external_id,
                relationship_role=None,
                is_primary=True,
                data_payload=payload,
            )
        )
        associations.append(
            NormalizedSalesforceDealAssociation(
                from_external_id=deal_external_id,
                from_object_type=CRMObjectType.DEAL.value,
                to_external_id=company_external_id,
                to_object_type=CRMObjectType.COMPANY.value,
                association_category=CRMAssociationCategory.SALESFORCE_STANDARD.value,
                association_type_id="Opportunity.AccountId",
                association_label="Opportunity Account",
                role_label=None,
                is_primary=True,
                data_payload=payload,
            )
        )

    return DealParticipantExtraction(
        participants=_dedup_participants(participants),
        associations=_dedup_associations(associations),
        primary_contact_by_deal=primary_contact_by_deal,
        contact_count_by_deal={
            deal_id: len(contact_ids) for deal_id, contact_ids in contact_ids_by_deal.items()
        },
        skipped_malformed=skipped_malformed,
    )


def _dedup_participants(
    participants: list[NormalizedSalesforceDealParticipant],
) -> list[NormalizedSalesforceDealParticipant]:
    seen: set[tuple[str, str, str, str | None]] = set()
    out: list[NormalizedSalesforceDealParticipant] = []
    for participant in participants:
        key = (
            participant.deal_external_id,
            participant.participant_type,
            participant.related_external_id,
            participant.relationship_role,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(participant)
    return out


def _dedup_associations(
    associations: list[NormalizedSalesforceDealAssociation],
) -> list[NormalizedSalesforceDealAssociation]:
    seen: set[tuple[str, str, str, str | None]] = set()
    out: list[NormalizedSalesforceDealAssociation] = []
    for association in associations:
        key = (
            association.from_external_id,
            association.to_object_type,
            association.to_external_id,
            association.association_type_id,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(association)
    return out


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return value != 0
    return False
