"""DTOs for HubSpot deals record sync (phase 6).

A deal is a *fact-table* row whose FKs resolve against the already-populated
dimensions (pipelines, stages, users, companies, contacts) loaded in the same
write session. The normaliser stashes the provider-side `*_external_id`s on
`NormalizedHubSpotDeal`; the writer resolves them to internal UUIDs at insert
time using session-scoped maps.

Unresolved links leave the FK column NULL and tick a counter for visibility —
we do not fail the run. A late-arriving dimension row can patch deal rows on
a subsequent sync via `backfill_crm_deals`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedHubSpotDeal:
    """A HubSpot deal after normalisation — provider IDs only, no FK resolution.

    `data_payload` holds the raw HubSpot record (including
    `propertiesWithHistory` and `associations`) so downstream phases
    (stage-history, associations fact table) don't need a re-fetch.
    """

    external_id: str
    name: str | None
    amount: Decimal | None
    amount_home_currency: Decimal | None
    currency: str | None
    deal_status: CRMDealStatus
    probability: float | None
    predicted_amount: Decimal | None
    close_date: datetime | None
    expected_close_date: datetime | None
    closed_won_at: datetime | None
    closed_lost_at: datetime | None
    closed_reason: str | None
    crm_url: str | None

    # Sales-process dimensions (IDs only — writer resolves FKs + denorms)
    pipeline_external_id: str | None
    stage_external_id: str | None
    forecast_category: str | None
    deal_type: str | None
    priority: str | None
    lead_source: str | None

    # Ownership / audit (IDs only)
    owner_external_id: str | None
    created_by_user_external_id: str | None
    updated_by_user_external_id: str | None

    # Primary company + contact from inline associations block
    primary_company_external_id: str | None
    primary_contact_external_id: str | None

    # Timing
    source_created_at: datetime | None
    source_modified_at: datetime | None
    last_activity_at: datetime | None
    stage_entered_at: datetime | None
    stage_updated_at: datetime | None
    days_to_close: int | None

    # Counts
    associated_contact_count: int | None
    associated_company_count: int | None
    note_count: int | None

    # Freeform
    description: str | None

    # JSONB payloads
    properties: dict[str, Any]
    metrics: dict[str, Any]
    associations_payload: dict[str, Any] | None
    data_payload: dict[str, Any]


@dataclass(slots=True)
class DealWriteResult:
    """Counters returned from `HubSpotDealWriter.apply()`.

    No deactivation counter: CRMDeal uses `deal_status` (OPEN/WON/LOST/CLOSED)
    rather than `is_active`, and we deliberately do not soft-delete deals that
    disappear from HubSpot — historical deal data is frequently still wanted
    even after a HubSpot archive. A future stale-deal reaper phase may revise.
    """

    deals_created: int = 0
    deals_updated: int = 0
    deals_skipped_stale: int = 0

    # Unresolved-link counters. A non-null `*_external_id` on the deal that
    # failed to find a matching dimension row in the in-session snapshot.
    # Row is still persisted with the FK NULL; the `*_external_id` stays so a
    # future sync (or backfill_crm_deals call) can resolve it.
    unresolved_pipeline_link: int = 0
    unresolved_stage_link: int = 0
    unresolved_owner_link: int = 0
    unresolved_company_link: int = 0
    unresolved_contact_link: int = 0
    unresolved_created_by_link: int = 0
    unresolved_updated_by_link: int = 0


@dataclass(slots=True)
class StageHistoryWriteResult:
    """Counters from `HubSpotDealStageHistoryWriter.apply()`."""

    stage_history_created: int = 0
    stage_history_updated: int = 0
    # Transitions whose `stage_external_id` didn't match any crm_deal_stages
    # row. The history row still lands; `stage_id` left NULL.
    unresolved_stage: int = 0
    # Same for from_stage_external_id (the stage exited from).
    unresolved_from_stage: int = 0


@dataclass(slots=True)
class ParticipantWriteResult:
    """Counters from `HubSpotDealParticipantWriter.apply()`."""

    participants_created: int = 0
    participants_updated: int = 0
    # Participants we couldn't resolve to a local dimension row (contact/
    # company/user not yet synced or archived). Row still lands with
    # related_local_id=NULL and the external id preserved.
    unresolved_related: int = 0


@dataclass(slots=True)
class AssociationWriteResult:
    """Counters from `HubSpotDealAssociationWriter.apply()`."""

    associations_created: int = 0
    associations_updated: int = 0
    # Associations whose `to_external_id` didn't match a known dimension
    # (line_item rows are expected to miss — we have no line_items dimension).
    unresolved_to_local: int = 0


@dataclass(slots=True, frozen=True)
class NormalizedStageTransition:
    """One row in `propertiesWithHistory.dealstage` after normalisation.

    HubSpot returns dealstage history newest-first. We compute `exited_at` as
    the timestamp of the next-newer entry (None for the current stage) and
    `from_stage_external_id` as the value of the next-older entry.
    """

    deal_external_id: str
    stage_external_id: str
    from_stage_external_id: str | None
    entered_at: datetime
    exited_at: datetime | None
    elapsed_seconds: int | None
    amount_at_stage: Any | None  # Decimal | None — Any avoids extra import noise
    probability_at_stage: float | None
    source: str | None  # provenance from HubSpot's `sourceType`


@dataclass(slots=True, frozen=True)
class NormalizedDealParticipant:
    """One participant link extracted from a HubSpot deal.

    Covers contacts and companies pulled from the inline `associations` block
    plus collaborator and split owners pulled from the corresponding dealroom
    properties. Role labels reflect HubSpot's association `type` string
    (e.g. `deal_to_contact_primary`) or a synthetic role for owner-derived
    participants (`collaborator`, `split_owner`).
    """

    deal_external_id: str
    participant_type: str  # CRMParticipantType value
    related_external_id: str
    relationship_role: str | None
    is_primary: bool
    data_payload: dict[str, Any] | None


@dataclass(slots=True, frozen=True)
class NormalizedDealAssociation:
    """One generic edge for `crm_object_associations`.

    Used for HubSpot association types that don't have a first-class
    representation in CRMDealParticipant (notably `line_items`, but also kept
    in sync with the participant edges so consumers that prefer the generic
    association graph have the full picture).
    """

    from_external_id: str
    from_object_type: str  # CRMObjectType value
    to_external_id: str
    to_object_type: str  # CRMObjectType value
    association_category: str  # CRMAssociationCategory value
    association_type_id: str | None
    association_label: str | None
    role_label: str | None
    is_primary: bool
    data_payload: dict[str, Any] | None


@dataclass(slots=True)
class DealSyncResult(RecordSyncResult):
    """End-to-end sync summary for HubSpot deals record sync."""

    fetched_deals: int = 0
    property_list_size: int = 0
    deals_created: int = 0
    deals_updated: int = 0
    deals_skipped_stale: int = 0
    unresolved_pipeline_link: int = 0
    unresolved_stage_link: int = 0
    unresolved_owner_link: int = 0
    unresolved_company_link: int = 0
    unresolved_contact_link: int = 0
    unresolved_created_by_link: int = 0
    unresolved_updated_by_link: int = 0
    skipped_malformed: int = 0
    # Stage history fact table.
    stage_history_created: int = 0
    stage_history_updated: int = 0
    stage_history_unresolved_stage: int = 0
    stage_history_unresolved_from_stage: int = 0
    # Deal participants (contacts, companies, collaborator owners, split owners).
    participants_created: int = 0
    participants_updated: int = 0
    participants_unresolved_related: int = 0
    # Generic object-association edges (line_items, etc.).
    associations_created: int = 0
    associations_updated: int = 0
    associations_unresolved_to_local: int = 0
    error_class: str | None = None
    error_message: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Build task-safe metadata from the result counters."""

        payload = self.base_metadata()
        payload.update(
            {
                "fetched_deals": self.fetched_deals,
                "property_list_size": self.property_list_size,
                "deals_created": self.deals_created,
                "deals_updated": self.deals_updated,
                "deals_skipped_stale": self.deals_skipped_stale,
                "unresolved_pipeline_link": self.unresolved_pipeline_link,
                "unresolved_stage_link": self.unresolved_stage_link,
                "unresolved_owner_link": self.unresolved_owner_link,
                "unresolved_company_link": self.unresolved_company_link,
                "unresolved_contact_link": self.unresolved_contact_link,
                "unresolved_created_by_link": self.unresolved_created_by_link,
                "unresolved_updated_by_link": self.unresolved_updated_by_link,
                "skipped_malformed": self.skipped_malformed,
                "stage_history_created": self.stage_history_created,
                "stage_history_updated": self.stage_history_updated,
                "stage_history_unresolved_stage": self.stage_history_unresolved_stage,
                "stage_history_unresolved_from_stage": (self.stage_history_unresolved_from_stage),
                "participants_created": self.participants_created,
                "participants_updated": self.participants_updated,
                "participants_unresolved_related": self.participants_unresolved_related,
                "associations_created": self.associations_created,
                "associations_updated": self.associations_updated,
                "associations_unresolved_to_local": self.associations_unresolved_to_local,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload


@dataclass(slots=True, frozen=True)
class DimensionHit:
    """One resolution hit from a dimension map.

    Carries the internal PK plus whichever denormalised fields the deal row
    wants to display (label/name/email/domain) so the writer doesn't have to
    reach back into the dimension row each time.
    """

    internal_id: Any  # UUID, but keeping loose to avoid circular typing noise
    name: str | None = None
    label: str | None = None
    email: str | None = None
    domain: str | None = None
    pipeline_id: Any | None = None  # only set on stage hits


@dataclass(slots=True, frozen=True)
class DealResolutionMaps:
    """Four session-scoped maps the writer consults to resolve deal FKs.

    - `pipeline_map[pipeline_external_id] = DimensionHit(internal_id, name, label)`
    - `stage_map[stage_external_id]       = DimensionHit(internal_id, name, label, pipeline_id=...)`
    - `user_map[owner_external_id]        = DimensionHit(internal_id, name, email)`
    - `company_map[company_external_id]   = DimensionHit(internal_id, name, domain)`
    - `contact_map[contact_external_id]   = DimensionHit(internal_id, name, email)`
    """

    pipeline_map: dict[str, DimensionHit] = field(default_factory=dict)
    stage_map: dict[str, DimensionHit] = field(default_factory=dict)
    user_map: dict[str, DimensionHit] = field(default_factory=dict)
    company_map: dict[str, DimensionHit] = field(default_factory=dict)
    contact_map: dict[str, DimensionHit] = field(default_factory=dict)
