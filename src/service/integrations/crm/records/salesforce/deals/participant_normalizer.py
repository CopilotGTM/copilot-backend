"""Salesforce OpportunityContactRole + Account link -> participants and associations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from src.domain.models.organisation_tables.sync.crm.associations import (
    CRMAssociationCategory,
    CRMObjectType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DealParticipantExtraction,
    NormalizedSalesforceDealAssociation,
    NormalizedSalesforceDealParticipant,
    SalesforceContactReferenceField,
)

logger = get_logger(__name__)


def contact_reference_fields_from_definitions(
    definitions: Iterable[Any],
) -> tuple[SalesforceContactReferenceField, ...]:
    fields: list[SalesforceContactReferenceField] = []
    for definition in definitions:
        raw = getattr(definition, "data_payload", None)
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("type") or "").lower() != "reference":
            continue
        if "Contact" not in (raw.get("referenceTo") or []):
            continue
        if raw.get("queryable") is False or raw.get("deprecatedAndHidden") is True:
            continue

        field_name = _coerce_str(raw.get("name") or getattr(definition, "provider_field_key", None))
        if field_name is None:
            continue
        label = _coerce_str(raw.get("label") or getattr(definition, "label", None))
        semantics = _field_semantics(field_name, label)
        fields.append(
            SalesforceContactReferenceField(
                field_name=field_name,
                relationship_role=label or field_name,
                is_primary=semantics["is_primary"],
                decision_power=semantics["decision_power"],
                stance=semantics["stance"],
            )
        )
    return tuple(sorted(fields, key=lambda field: field.field_name.casefold()))


def extract_contact_reference_participants(
    raw_opportunities: Iterable[dict[str, Any]],
    *,
    fields: tuple[SalesforceContactReferenceField, ...],
) -> DealParticipantExtraction:
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    primary_contact_by_deal: dict[str, str] = {}
    contact_ids_by_deal: dict[str, set[str]] = defaultdict(set)
    skipped_malformed = 0

    for raw in raw_opportunities:
        if not isinstance(raw, dict):
            skipped_malformed += 1
            continue
        deal_external_id = _coerce_str(raw.get("Id"))
        if deal_external_id is None:
            skipped_malformed += 1
            continue
        for field in fields:
            contact_external_id = _coerce_str(raw.get(field.field_name))
            if contact_external_id is None:
                continue
            contact_ids_by_deal[deal_external_id].add(contact_external_id)
            identity = (deal_external_id, contact_external_id)
            aggregate = by_identity.setdefault(
                identity,
                {
                    "field_names": [],
                    "relationship_role": None,
                    "relationship_role_rank": -1,
                    "is_primary": False,
                    "decision_power": DecisionPower.UNKNOWN.value,
                    "stance": StakeholderStance.UNKNOWN.value,
                },
            )
            aggregate["field_names"].append(field.field_name)
            role_rank = _contact_reference_role_rank(field)
            if role_rank > aggregate["relationship_role_rank"]:
                aggregate["relationship_role"] = field.relationship_role
                aggregate["relationship_role_rank"] = role_rank
            aggregate["is_primary"] = aggregate["is_primary"] or field.is_primary
            if (
                aggregate["decision_power"] == DecisionPower.UNKNOWN.value
                and field.decision_power != DecisionPower.UNKNOWN.value
            ):
                aggregate["decision_power"] = field.decision_power
            if (
                aggregate["stance"] == StakeholderStance.UNKNOWN.value
                and field.stance != StakeholderStance.UNKNOWN.value
            ):
                aggregate["stance"] = field.stance
            if field.is_primary:
                primary_contact_by_deal.setdefault(deal_external_id, contact_external_id)

    participants = [
        NormalizedSalesforceDealParticipant(
            deal_external_id=deal_external_id,
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id=contact_external_id,
            relationship_role=aggregate["relationship_role"],
            is_primary=aggregate["is_primary"],
            data_payload={
                "source": "Opportunity contact reference",
                "opportunity_id": deal_external_id,
                "contact_id": contact_external_id,
                "field_names": aggregate["field_names"],
            },
            decision_power=aggregate["decision_power"],
            stance=aggregate["stance"],
        )
        for (deal_external_id, contact_external_id), aggregate in by_identity.items()
    ]
    return DealParticipantExtraction(
        participants=participants,
        primary_contact_by_deal=primary_contact_by_deal,
        contact_count_by_deal={
            deal_id: len(contact_ids) for deal_id, contact_ids in contact_ids_by_deal.items()
        },
        skipped_malformed=skipped_malformed,
    )


def extract_participants(
    raw_contact_roles: Iterable[dict[str, Any]],
    *,
    company_external_id_by_deal: Mapping[str, str | None],
    additional_primary_contact_by_deal: Mapping[str, str] | None = None,
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
        is_primary = (
            _coerce_bool(raw.get("IsPrimary"))
            or (additional_primary_contact_by_deal or {}).get(deal_external_id)
            == contact_external_id
        )
        contact_ids_by_deal[deal_external_id].add(contact_external_id)
        if is_primary:
            primary_contact_by_deal.setdefault(deal_external_id, contact_external_id)

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
    positions: dict[tuple[str, str, str], int] = {}
    out: list[NormalizedSalesforceDealParticipant] = []
    for participant in participants:
        key = (
            participant.deal_external_id,
            participant.participant_type,
            participant.related_external_id,
        )
        position = positions.get(key)
        if position is not None:
            current = out[position]
            incoming_role_is_business = bool(
                participant.relationship_role
                and "primary contact" not in _normalized_label(participant.relationship_role)
            )
            current_role_is_business = bool(
                current.relationship_role
                and "primary contact" not in _normalized_label(current.relationship_role)
            )
            use_incoming_role = incoming_role_is_business and not current_role_is_business
            out[position] = replace(
                current,
                relationship_role=(
                    participant.relationship_role
                    if use_incoming_role
                    else current.relationship_role or participant.relationship_role
                ),
                is_primary=current.is_primary
                or participant.is_primary
                or _is_primary_contact_role(current.relationship_role)
                or _is_primary_contact_role(participant.relationship_role),
                data_payload=participant.data_payload
                if use_incoming_role
                else current.data_payload,
                decision_power=(
                    participant.decision_power
                    if current.decision_power in {None, DecisionPower.UNKNOWN.value}
                    and participant.decision_power not in {None, DecisionPower.UNKNOWN.value}
                    else current.decision_power
                ),
                stance=(
                    participant.stance
                    if current.stance in {None, StakeholderStance.UNKNOWN.value}
                    and participant.stance not in {None, StakeholderStance.UNKNOWN.value}
                    else current.stance
                ),
            )
            continue
        positions[key] = len(out)
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


def _is_primary_contact_role(role: str | None) -> bool:
    return bool(role and "primary contact" in _normalized_label(role))


def _normalized_label(value: str) -> str:
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _field_semantics(field_name: str, label: str | None) -> dict[str, Any]:
    normalized = " ".join(
        f"{field_name} {label or ''}".lower()
        .replace("__c", " ")
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )
    decision_power = DecisionPower.UNKNOWN.value
    stance = StakeholderStance.UNKNOWN.value

    if any(
        phrase in normalized
        for phrase in (
            "decision maker",
            "executive sponsor",
        )
    ):
        decision_power = DecisionPower.DECISION_MAKER.value
    elif "technical buyer" in normalized or "evaluator" in normalized:
        decision_power = DecisionPower.EVALUATOR.value
    elif "influencer" in normalized:
        decision_power = DecisionPower.INFLUENCER.value
    elif "business user" in normalized or "end user" in normalized:
        decision_power = DecisionPower.END_USER.value
    elif "gatekeeper" in normalized:
        decision_power = DecisionPower.GATEKEEPER.value

    if "champion" in normalized:
        stance = StakeholderStance.CHAMPION.value
    elif "supporter" in normalized:
        stance = StakeholderStance.SUPPORTER.value
    elif "detractor" in normalized:
        stance = StakeholderStance.DETRACTOR.value
    elif "blocker" in normalized:
        stance = StakeholderStance.BLOCKER.value
    elif "neutral" in normalized:
        stance = StakeholderStance.NEUTRAL.value

    return {
        "is_primary": "primary contact" in normalized,
        "decision_power": decision_power,
        "stance": stance,
    }


def _contact_reference_role_rank(field: SalesforceContactReferenceField) -> int:
    role = " ".join(field.relationship_role.lower().replace("__c", " ").replace("_", " ").split())
    priorities = (
        (("economic buyer", "budget owner"), 80),
        (("decision maker",), 70),
        (("executive sponsor",), 60),
        (("technical buyer", "evaluator"), 50),
        (("influencer",), 40),
        (("business user", "end user"), 30),
        (("champion",), 20),
        (("primary contact",), 10),
    )
    for phrases, rank in priorities:
        if any(phrase in role for phrase in phrases):
            return rank
    return 0
