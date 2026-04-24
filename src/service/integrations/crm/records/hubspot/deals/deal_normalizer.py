"""HubSpot deal → NormalizedHubSpotDeal.

Pure functions, no I/O. HubSpot returns each deal as:

    {
      "id": "123",
      "properties": {"dealname": "...", "amount": "1000.00", ...},
      "propertiesWithHistory": {"dealstage": [...], ...},   # if requested
      "associations": {"companies": {...}, "contacts": {...}},
      "createdAt": "...",
      "updatedAt": "...",
      "archived": false
    }

We:
  * Key canonical columns off `HUBSPOT_DEAL_CANONICAL_FIELDS` (same source of
    truth used by the schema sync — avoids drift).
  * Coerce HubSpot's string-typed numerics to `Decimal` and its ISO-8601
    timestamps to `datetime`. Bad values silently become None on a per-field
    basis; only a completely malformed record (no `id`, non-dict, or archived)
    is dropped as `skipped_malformed`.
  * Derive `deal_status` from `hs_is_closed_won` / `hs_is_closed_lost`
    booleans so downstream code doesn't have to interpret stage semantics.
  * Pick a primary company and primary contact from the inline
    `associations` block, preferring HubSpot's own `_primary` association
    type when present.
  * Preserve the entire raw record (including `propertiesWithHistory` and the
    `associations` block) inside `data_payload` — phase 7 reads stage-change
    history from there without a second HubSpot round-trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.domain.models.organisation_tables.sync.crm.associations import (
    CRMAssociationCategory,
    CRMObjectType,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStatus,
    CRMParticipantType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    NormalizedDealAssociation,
    NormalizedDealParticipant,
    NormalizedHubSpotDeal,
    NormalizedStageTransition,
)
from src.service.integrations.crm.schema.hubspot.deals.mapping import (
    HUBSPOT_DEAL_CANONICAL_FIELDS,
)

logger = get_logger(__name__)


# Properties the writer materialises into dedicated CRMDeal columns (NOT into
# the `properties` JSONB overflow). Everything else the fetcher pulls lands in
# the overflow bucket for observability.
_CANONICAL_PROPERTY_NAMES: frozenset[str] = frozenset(HUBSPOT_DEAL_CANONICAL_FIELDS.keys())

# Money-flavoured HubSpot properties surfaced on a dedicated column. Kept
# separate from the canonical map because HubSpot returns them as strings —
# normaliser coerces via `_coerce_decimal` rather than blindly stashing.
_AMOUNT_FIELDS: frozenset[str] = frozenset(
    {
        "amount",
        "amount_in_home_currency",
        "hs_projected_amount",
    }
)


def normalize_deals(
    raw_deals: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedHubSpotDeal], int]:
    """Normalise every raw deal. Returns (valid list, skipped_malformed)."""
    normalized: list[NormalizedHubSpotDeal] = []
    skipped_malformed = 0
    for raw in raw_deals:
        deal = _normalize_one(raw)
        if deal is None:
            skipped_malformed += 1
            continue
        normalized.append(deal)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedHubSpotDeal | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict HubSpot deal payload")
        return None

    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed HubSpot deal (no usable id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    if raw.get("archived") is True:
        logger.warning(
            "Skipping archived HubSpot deal",
            extra={"deal_id": external_id},
        )
        return None

    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    associations = raw.get("associations")
    if not isinstance(associations, dict):
        associations = None

    name = _coerce_str(props.get("dealname"))
    amount = _coerce_decimal(props.get("amount"))
    amount_home_currency = _coerce_decimal(props.get("amount_in_home_currency"))
    currency = _coerce_str(props.get("deal_currency_code")) or _coerce_str(props.get("currency"))
    predicted_amount = _coerce_decimal(props.get("hs_projected_amount"))
    probability = _coerce_probability(props.get("hs_deal_stage_probability"))
    deal_status = _derive_deal_status(props)

    close_date = _parse_iso(props.get("closedate"))
    expected_close_date = _parse_iso(props.get("hs_expected_close_date"))
    closed_won_at = _parse_iso(props.get("hs_closed_won_date"))
    closed_lost_at = _parse_iso(props.get("hs_closed_lost_date"))
    closed_reason = (
        _coerce_str(props.get("closed_won_reason"))
        or _coerce_str(props.get("closed_lost_reason"))
        or _coerce_str(props.get("hs_closed_reason"))
    )
    crm_url = _coerce_str(props.get("hs_object_url")) or _coerce_str(raw.get("hs_object_url"))

    pipeline_external_id = _coerce_str(props.get("pipeline"))
    stage_external_id = _coerce_str(props.get("dealstage"))
    forecast_category = _coerce_str(props.get("hs_forecast_category"))
    deal_type = _coerce_str(props.get("dealtype"))
    priority = _coerce_str(props.get("hs_priority"))
    lead_source = _coerce_str(props.get("lead_source")) or _coerce_str(props.get("hs_lead_source"))

    owner_external_id = _coerce_str(props.get("hubspot_owner_id"))
    created_by_user_external_id = _coerce_str(props.get("hs_created_by_user_id"))
    updated_by_user_external_id = _coerce_str(props.get("hs_updated_by_user_id"))

    primary_company_external_id = _select_primary_association_id(
        associations, "companies", primary_type="deal_to_company_primary"
    )
    primary_contact_external_id = _select_primary_association_id(
        associations, "contacts", primary_type="deal_to_contact_primary"
    )

    source_created_at = _parse_iso(raw.get("createdAt")) or _parse_iso(props.get("createdate"))
    source_modified_at = _parse_iso(raw.get("updatedAt")) or _parse_iso(
        props.get("hs_lastmodifieddate")
    )
    last_activity_at = _parse_iso(props.get("notes_last_updated")) or _parse_iso(
        props.get("lastactivitydate")
    )
    stage_entered_at = _parse_iso(props.get("hs_date_entered_current_stage"))
    stage_updated_at = _parse_iso(props.get("hs_date_exited_current_stage")) or source_modified_at
    days_to_close = _coerce_int(props.get("days_to_close"))

    associated_contact_count = _coerce_int(props.get("num_associated_contacts"))
    associated_company_count = _coerce_int(props.get("num_associated_companies"))
    note_count = _coerce_int(props.get("num_notes"))

    description = _coerce_str(props.get("description"))

    metrics = _collect_metrics(props)
    overflow = _collect_overflow_properties(props)

    return NormalizedHubSpotDeal(
        external_id=external_id,
        name=name,
        amount=amount,
        amount_home_currency=amount_home_currency,
        currency=currency,
        deal_status=deal_status,
        probability=probability,
        predicted_amount=predicted_amount,
        close_date=close_date,
        expected_close_date=expected_close_date,
        closed_won_at=closed_won_at,
        closed_lost_at=closed_lost_at,
        closed_reason=closed_reason,
        crm_url=crm_url,
        pipeline_external_id=pipeline_external_id,
        stage_external_id=stage_external_id,
        forecast_category=forecast_category,
        deal_type=deal_type,
        priority=priority,
        lead_source=lead_source,
        owner_external_id=owner_external_id,
        created_by_user_external_id=created_by_user_external_id,
        updated_by_user_external_id=updated_by_user_external_id,
        primary_company_external_id=primary_company_external_id,
        primary_contact_external_id=primary_contact_external_id,
        source_created_at=source_created_at,
        source_modified_at=source_modified_at,
        last_activity_at=last_activity_at,
        stage_entered_at=stage_entered_at,
        stage_updated_at=stage_updated_at,
        days_to_close=days_to_close,
        associated_contact_count=associated_contact_count,
        associated_company_count=associated_company_count,
        note_count=note_count,
        description=description,
        properties=overflow,
        metrics=metrics,
        associations_payload=associations,
        data_payload=raw,
    )


def _derive_deal_status(props: dict[str, Any]) -> CRMDealStatus:
    """Map HubSpot's boolean-ish closed flags onto our CRMDealStatus enum.

    HubSpot returns `"true"` / `"false"` strings for these flags. We prefer
    won > lost > closed > open so a deal flagged as both won *and* closed
    (which HubSpot does) lands on WON rather than the generic CLOSED bucket.
    """
    if _is_truthy(props.get("hs_is_closed_won")):
        return CRMDealStatus.WON
    if _is_truthy(props.get("hs_is_closed_lost")):
        return CRMDealStatus.LOST
    if _is_truthy(props.get("hs_is_closed")):
        return CRMDealStatus.CLOSED
    return CRMDealStatus.OPEN


def _collect_metrics(props: dict[str, Any]) -> dict[str, Any]:
    """Revenue-flavoured HubSpot properties the UI surfaces as deal metrics.

    Kept separate from the `properties` overflow bucket so analytics can read
    them without paging through every custom field. Values remain strings
    here — the analytics layer coerces as needed.
    """
    keys = (
        "hs_arr",
        "hs_mrr",
        "hs_tcv",
        "hs_acv",
        "hs_forecast_amount",
        "hs_forecast_probability",
        "hs_recurring_revenue_amount",
        "hs_recurring_revenue_deal_type",
        "hs_recurring_revenue_inactive_reason",
        "hs_recurring_revenue_end_date",
        "hs_projected_amount_in_home_currency",
        "hs_exchange_rate",
        "hs_time_in_current_stage",
        "closed_amount",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if key in props:
            out[key] = props[key]
    return out


def _collect_overflow_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Everything in `props` that isn't mapped to a dedicated column or metrics bucket.

    Custom properties (portal-specific) land here. Metrics keys are excluded
    because they already live in `metrics`. This gives the writer a clean
    JSONB payload to stash on `CRMDeal.properties` without duplicating data
    already persisted in typed columns.
    """
    metrics_keys = {
        "hs_arr",
        "hs_mrr",
        "hs_tcv",
        "hs_acv",
        "hs_forecast_amount",
        "hs_forecast_probability",
        "hs_recurring_revenue_amount",
        "hs_recurring_revenue_deal_type",
        "hs_recurring_revenue_inactive_reason",
        "hs_recurring_revenue_end_date",
        "hs_projected_amount_in_home_currency",
        "hs_exchange_rate",
        "hs_time_in_current_stage",
        "closed_amount",
    }
    out: dict[str, Any] = {}
    for key, value in props.items():
        if key in _CANONICAL_PROPERTY_NAMES:
            continue
        if key in _AMOUNT_FIELDS:
            continue
        if key in metrics_keys:
            continue
        out[key] = value
    return out


def _select_primary_association_id(
    associations: dict[str, Any] | None,
    kind: str,
    *,
    primary_type: str,
) -> str | None:
    """Pick the HubSpot id for the primary associated object of `kind`.

    Prefers an entry whose `type` matches HubSpot's `_primary` association
    label; falls back to the first result if no primary is marked.
    """
    if not associations:
        return None
    block = associations.get(kind)
    if not isinstance(block, dict):
        return None
    results = block.get("results")
    if not isinstance(results, list) or not results:
        return None

    chosen: dict[str, Any] | None = None
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == primary_type:
            chosen = entry
            break
    if chosen is None:
        first = results[0]
        if isinstance(first, dict):
            chosen = first
    if chosen is None:
        return None
    return _coerce_id(chosen.get("id"))


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _coerce_decimal(value: Any) -> Decimal | None:
    """HubSpot returns money as JSON strings; coerce defensively."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower() == "null":
            return None
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None
    return None


def _coerce_probability(value: Any) -> float | None:
    """HubSpot reports `hs_deal_stage_probability` as a decimal string 0..1
    *or* 0..100 depending on the portal. We coerce to 0..100 for the
    CRMDeal.probability column's CheckConstraint.
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            parsed = float(cleaned)
        elif isinstance(value, int | float):
            parsed = float(value)
        else:
            return None
    except ValueError:
        return None
    if parsed <= 1.0:
        parsed *= 100.0
    if parsed < 0.0 or parsed > 100.0:
        return None
    return parsed


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if isinstance(value, int | float):
        return bool(value)
    return False


# ── Stage history / participants / associations extraction ──────────────────


def extract_stage_transitions(
    raw: dict[str, Any],
) -> list[NormalizedStageTransition]:
    """Walk `propertiesWithHistory.dealstage` to produce one row per transition.

    HubSpot returns the history array newest-first. For each entry at index
    `i` we treat it as a transition INTO `entry.value`:

      entered_at = entry.timestamp
      exited_at  = entries[i-1].timestamp  if i > 0 else None  (still in stage)
      from_stage = entries[i+1].value      if i+1 < len else None  (no prior)

    `amount_at_stage` / `probability_at_stage` are looked up from the
    co-temporal `amount` / `hs_deal_stage_probability` history (closest
    timestamp ≤ entered_at). `source` carries HubSpot's `sourceType`
    (USER / WORKFLOW / API / IMPORT etc.) for provenance.
    """
    deal_external_id = _coerce_id(raw.get("id"))
    if deal_external_id is None:
        return []

    history_block = raw.get("propertiesWithHistory")
    if not isinstance(history_block, dict):
        return []
    stage_history = _normalize_history_array(history_block.get("dealstage"))
    if not stage_history:
        return []

    amount_history = _normalize_history_array(history_block.get("amount"))
    probability_history = _normalize_history_array(history_block.get("hs_deal_stage_probability"))

    transitions: list[NormalizedStageTransition] = []
    for i, entry in enumerate(stage_history):
        stage_external_id = _coerce_str(entry.get("value"))
        entered_at = _parse_iso(entry.get("timestamp"))
        if stage_external_id is None or entered_at is None:
            continue

        exited_at = _parse_iso(stage_history[i - 1].get("timestamp")) if i > 0 else None
        from_stage_external_id = (
            _coerce_str(stage_history[i + 1].get("value")) if i + 1 < len(stage_history) else None
        )

        elapsed: int | None = None
        if exited_at is not None:
            delta = exited_at - entered_at
            elapsed = max(int(delta.total_seconds()), 0)

        amount_at = _coerce_decimal(_history_value_at(amount_history, entered_at))
        probability_at = _coerce_probability(_history_value_at(probability_history, entered_at))

        transitions.append(
            NormalizedStageTransition(
                deal_external_id=deal_external_id,
                stage_external_id=stage_external_id,
                from_stage_external_id=from_stage_external_id,
                entered_at=entered_at,
                exited_at=exited_at,
                elapsed_seconds=elapsed,
                amount_at_stage=amount_at,
                probability_at_stage=probability_at,
                source=_coerce_str(entry.get("sourceType")),
            )
        )
    return transitions


def extract_participants(
    raw: dict[str, Any],
) -> list[NormalizedDealParticipant]:
    """Emit one participant row per associated contact / company / collab owner.

    Sources walked:
      * `associations.contacts.results`        → CONTACT participants
      * `associations.companies.results`       → COMPANY participants
      * `properties.hs_all_collaborator_owner_ids` → USER participants (collaborator)
      * `properties.hs_all_deal_split_owner_ids`   → USER participants (split_owner)

    HubSpot returns its association `type` string (e.g. `deal_to_contact`,
    `deal_to_contact_primary`) which we keep verbatim as `relationship_role`.
    `is_primary` is set when the type matches the well-known `_primary` suffix.
    """
    deal_external_id = _coerce_id(raw.get("id"))
    if deal_external_id is None:
        return []

    out: list[NormalizedDealParticipant] = []
    associations_raw = raw.get("associations")
    associations: dict[str, Any] = associations_raw if isinstance(associations_raw, dict) else {}

    out.extend(
        _participants_from_assoc_block(
            deal_external_id=deal_external_id,
            assoc_block=associations.get("contacts"),
            participant_type=CRMParticipantType.CONTACT.value,
            primary_type_label="deal_to_contact_primary",
        )
    )
    out.extend(
        _participants_from_assoc_block(
            deal_external_id=deal_external_id,
            assoc_block=associations.get("companies"),
            participant_type=CRMParticipantType.COMPANY.value,
            primary_type_label="deal_to_company_primary",
        )
    )

    props_raw = raw.get("properties")
    props: dict[str, Any] = props_raw if isinstance(props_raw, dict) else {}
    out.extend(
        _participants_from_owner_property(
            deal_external_id=deal_external_id,
            property_value=props.get("hs_all_collaborator_owner_ids"),
            relationship_role="collaborator",
        )
    )
    out.extend(
        _participants_from_owner_property(
            deal_external_id=deal_external_id,
            property_value=props.get("hs_all_deal_split_owner_ids"),
            relationship_role="split_owner",
        )
    )

    return _dedup_participants(out)


def extract_associations(
    raw: dict[str, Any],
) -> list[NormalizedDealAssociation]:
    """Emit one generic-edge row per HubSpot association on this deal.

    Covers contacts, companies, AND line_items so consumers that prefer the
    generic association graph over the deal-centric participant table see the
    full picture (including line items, which have no participant
    representation today).
    """
    deal_external_id = _coerce_id(raw.get("id"))
    if deal_external_id is None:
        return []

    associations_raw = raw.get("associations")
    associations: dict[str, Any] = associations_raw if isinstance(associations_raw, dict) else {}
    if not associations:
        return []

    edges: list[NormalizedDealAssociation] = []
    for kind, to_object_type, primary_type in (
        ("contacts", CRMObjectType.CONTACT.value, "deal_to_contact_primary"),
        ("companies", CRMObjectType.COMPANY.value, "deal_to_company_primary"),
        ("line_items", CRMObjectType.LINE_ITEM.value, None),
    ):
        edges.extend(
            _edges_from_assoc_block(
                deal_external_id=deal_external_id,
                assoc_block=associations.get(kind),
                to_object_type=to_object_type,
                primary_type=primary_type,
            )
        )
    return edges


def _participants_from_assoc_block(
    *,
    deal_external_id: str,
    assoc_block: Any,
    participant_type: str,
    primary_type_label: str,
) -> list[NormalizedDealParticipant]:
    if not isinstance(assoc_block, dict):
        return []
    results = assoc_block.get("results")
    if not isinstance(results, list):
        return []
    out: list[NormalizedDealParticipant] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        related_external_id = _coerce_id(entry.get("id"))
        if related_external_id is None:
            continue
        type_label = _coerce_str(entry.get("type"))
        is_primary = type_label == primary_type_label
        out.append(
            NormalizedDealParticipant(
                deal_external_id=deal_external_id,
                participant_type=participant_type,
                related_external_id=related_external_id,
                relationship_role=type_label,
                is_primary=is_primary,
                data_payload=entry,
            )
        )
    return out


def _participants_from_owner_property(
    *,
    deal_external_id: str,
    property_value: Any,
    relationship_role: str,
) -> list[NormalizedDealParticipant]:
    """`hs_all_collaborator_owner_ids` and friends are semicolon-delimited strings."""
    if not isinstance(property_value, str) or not property_value:
        return []
    seen: set[str] = set()
    out: list[NormalizedDealParticipant] = []
    for raw_id in property_value.split(";"):
        cleaned = raw_id.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(
            NormalizedDealParticipant(
                deal_external_id=deal_external_id,
                participant_type=CRMParticipantType.USER.value,
                related_external_id=cleaned,
                relationship_role=relationship_role,
                is_primary=False,
                data_payload=None,
            )
        )
    return out


def _dedup_participants(
    participants: list[NormalizedDealParticipant],
) -> list[NormalizedDealParticipant]:
    """Same (type, related_external_id, role) emitted twice → keep first.

    Avoids duplicate `crm_deal_participants` rows when, e.g., HubSpot returns
    the primary contact twice in the same association block (rare, but
    possible after data-migration imports).
    """
    seen: set[tuple[str, str, str | None]] = set()
    out: list[NormalizedDealParticipant] = []
    for p in participants:
        key = (p.participant_type, p.related_external_id, p.relationship_role)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _edges_from_assoc_block(
    *,
    deal_external_id: str,
    assoc_block: Any,
    to_object_type: str,
    primary_type: str | None,
) -> list[NormalizedDealAssociation]:
    if not isinstance(assoc_block, dict):
        return []
    results = assoc_block.get("results")
    if not isinstance(results, list):
        return []
    out: list[NormalizedDealAssociation] = []
    seen: set[tuple[str, str | None]] = set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        to_external_id = _coerce_id(entry.get("id"))
        if to_external_id is None:
            continue
        type_label = _coerce_str(entry.get("type"))
        key = (to_external_id, type_label)
        if key in seen:
            continue
        seen.add(key)
        is_primary = primary_type is not None and type_label == primary_type
        out.append(
            NormalizedDealAssociation(
                from_external_id=deal_external_id,
                from_object_type=CRMObjectType.DEAL.value,
                to_external_id=to_external_id,
                to_object_type=to_object_type,
                association_category=CRMAssociationCategory.HUBSPOT_DEFINED.value,
                association_type_id=type_label,
                association_label=type_label,
                role_label=type_label,
                is_primary=is_primary,
                data_payload=entry,
            )
        )
    return out


def _normalize_history_array(value: Any) -> list[dict[str, Any]]:
    """Defensive coercion: keep only dict entries from a HubSpot history array.

    HubSpot returns these newest-first; we preserve that ordering so callers
    can rely on it for `entered_at`/`exited_at` derivation.
    """
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _history_value_at(
    history: list[dict[str, Any]],
    moment: datetime,
) -> Any | None:
    """Return the `value` of the latest history entry whose timestamp ≤ moment.

    History is newest-first. We walk from the end (oldest) forwards and pick
    the latest entry not strictly after `moment`. Returns None when no entry
    qualifies (e.g. amount was set after the deal entered the stage).
    """
    chosen: Any | None = None
    for entry in reversed(history):
        ts = _parse_iso(entry.get("timestamp"))
        if ts is None or ts > moment:
            continue
        chosen = entry.get("value")
    return chosen
