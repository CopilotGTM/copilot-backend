"""DTOs for Salesforce opportunity/deal fact sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.integrations.crm.records.common.record_sync_types import RecordSyncResult


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceOpportunity:
    external_id: str
    name: str | None
    amount: float | None
    amount_home_currency: float | None
    currency: str | None
    deal_status: CRMDealStatus
    probability: float | None
    predicted_amount: float | None
    close_date: datetime | None
    expected_close_date: datetime | None
    closed_won_at: datetime | None
    closed_lost_at: datetime | None
    closed_reason: str | None
    crm_url: str | None
    pipeline_external_id: str | None
    stage_external_id: str | None
    forecast_category: str | None
    deal_type: str | None
    priority: str | None
    lead_source: str | None
    owner_external_id: str | None
    created_by_user_external_id: str | None
    updated_by_user_external_id: str | None
    primary_company_external_id: str | None
    primary_contact_external_id: str | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    last_activity_at: datetime | None
    stage_entered_at: datetime | None
    stage_updated_at: datetime | None
    days_to_close: int | None
    associated_contact_count: int | None
    associated_company_count: int | None
    note_count: int | None
    description: str | None
    properties: dict[str, Any]
    metrics: dict[str, Any]
    associations_payload: dict[str, Any] | None
    data_payload: dict[str, Any]


@dataclass(slots=True)
class DealWriteResult:
    deals_created: int = 0
    deals_updated: int = 0
    unresolved_pipeline_link: int = 0
    unresolved_stage_link: int = 0
    unresolved_owner_link: int = 0
    unresolved_company_link: int = 0
    unresolved_contact_link: int = 0
    unresolved_created_by_link: int = 0
    unresolved_updated_by_link: int = 0


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceStageTransition:
    deal_external_id: str
    stage_external_id: str
    from_stage_external_id: str | None
    entered_at: datetime
    exited_at: datetime | None
    elapsed_seconds: int | None
    amount_at_stage: float | None
    probability_at_stage: float | None
    source: str | None
    data_payload: dict[str, Any] | None


@dataclass(slots=True)
class StageHistoryWriteResult:
    stage_history_created: int = 0
    stage_history_updated: int = 0
    unresolved_stage: int = 0
    unresolved_from_stage: int = 0


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceDealParticipant:
    deal_external_id: str
    participant_type: str
    related_external_id: str
    relationship_role: str | None
    is_primary: bool
    data_payload: dict[str, Any] | None


@dataclass(slots=True)
class ParticipantWriteResult:
    participants_created: int = 0
    participants_updated: int = 0
    unresolved_related: int = 0


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceDealAssociation:
    from_external_id: str
    from_object_type: str
    to_external_id: str
    to_object_type: str
    association_category: str
    association_type_id: str | None
    association_label: str | None
    role_label: str | None
    is_primary: bool
    data_payload: dict[str, Any] | None


@dataclass(slots=True)
class AssociationWriteResult:
    associations_created: int = 0
    associations_updated: int = 0
    unresolved_to_local: int = 0


@dataclass(slots=True, frozen=True)
class DimensionHit:
    internal_id: Any
    name: str | None = None
    label: str | None = None
    email: str | None = None
    domain: str | None = None
    pipeline_id: Any | None = None


@dataclass(slots=True, frozen=True)
class DealResolutionMaps:
    pipeline_map: dict[str, DimensionHit] = field(default_factory=dict)
    stage_map: dict[str, DimensionHit] = field(default_factory=dict)
    user_map: dict[str, DimensionHit] = field(default_factory=dict)
    company_map: dict[str, DimensionHit] = field(default_factory=dict)
    contact_map: dict[str, DimensionHit] = field(default_factory=dict)


@dataclass(slots=True)
class DealParticipantExtraction:
    participants: list[NormalizedSalesforceDealParticipant] = field(default_factory=list)
    associations: list[NormalizedSalesforceDealAssociation] = field(default_factory=list)
    primary_contact_by_deal: dict[str, str] = field(default_factory=dict)
    contact_count_by_deal: dict[str, int] = field(default_factory=dict)
    skipped_malformed: int = 0


@dataclass(slots=True)
class DealSyncResult(RecordSyncResult):
    fetched_opportunities: int = 0
    property_list_size: int = 0
    fetched_stage_history_rows: int = 0
    fetched_participant_rows: int = 0
    deals_created: int = 0
    deals_updated: int = 0
    unresolved_pipeline_link: int = 0
    unresolved_stage_link: int = 0
    unresolved_owner_link: int = 0
    unresolved_company_link: int = 0
    unresolved_contact_link: int = 0
    unresolved_created_by_link: int = 0
    unresolved_updated_by_link: int = 0
    skipped_malformed_opportunities: int = 0
    skipped_malformed_stage_history: int = 0
    skipped_malformed_participants: int = 0
    stage_history_created: int = 0
    stage_history_updated: int = 0
    stage_history_unresolved_stage: int = 0
    stage_history_unresolved_from_stage: int = 0
    participants_created: int = 0
    participants_updated: int = 0
    participants_unresolved_related: int = 0
    associations_created: int = 0
    associations_updated: int = 0
    associations_unresolved_to_local: int = 0
    deal_backfill_rows_updated: int = 0
    deal_backfill_per_op: dict[str, int] = field(default_factory=dict)
    error_class: str | None = None
    error_message: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        payload = self.base_metadata()
        payload.update(
            {
                "fetched_opportunities": self.fetched_opportunities,
                "property_list_size": self.property_list_size,
                "fetched_stage_history_rows": self.fetched_stage_history_rows,
                "fetched_participant_rows": self.fetched_participant_rows,
                "deals_created": self.deals_created,
                "deals_updated": self.deals_updated,
                "unresolved_pipeline_link": self.unresolved_pipeline_link,
                "unresolved_stage_link": self.unresolved_stage_link,
                "unresolved_owner_link": self.unresolved_owner_link,
                "unresolved_company_link": self.unresolved_company_link,
                "unresolved_contact_link": self.unresolved_contact_link,
                "unresolved_created_by_link": self.unresolved_created_by_link,
                "unresolved_updated_by_link": self.unresolved_updated_by_link,
                "skipped_malformed_opportunities": self.skipped_malformed_opportunities,
                "skipped_malformed_stage_history": self.skipped_malformed_stage_history,
                "skipped_malformed_participants": self.skipped_malformed_participants,
                "stage_history_created": self.stage_history_created,
                "stage_history_updated": self.stage_history_updated,
                "stage_history_unresolved_stage": self.stage_history_unresolved_stage,
                "stage_history_unresolved_from_stage": self.stage_history_unresolved_from_stage,
                "participants_created": self.participants_created,
                "participants_updated": self.participants_updated,
                "participants_unresolved_related": self.participants_unresolved_related,
                "associations_created": self.associations_created,
                "associations_updated": self.associations_updated,
                "associations_unresolved_to_local": self.associations_unresolved_to_local,
                "deal_backfill_rows_updated": self.deal_backfill_rows_updated,
                "deal_backfill_per_op": self.deal_backfill_per_op,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
