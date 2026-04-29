"""Tests for RiskBlockerExtractionService item creation.

Verifies the AI-extraction path writes items directly with status=OPEN,
attributes them to the AUTOMATED_PIPELINE_USER_ID sentinel, and seeds a
properly shaped audit-trail "opened" event so the suggested→accepted
transit is fully removed.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from src.agents.workflows.risk_blocker_extraction import ExtractedRiskBlocker
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.workers.deal_worker.services.deal_insight_services.risk_blocker_extraction_service import (
    AI_EXTRACTION_ACTOR_NAME,
    AUTOMATED_PIPELINE_USER_ID,
    RiskBlockerExtractionService,
)


class _CapturingSession:
    """Stub session that captures every object passed to add()."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


def _build_extracted(
    *,
    item_type: RiskBlockerType = RiskBlockerType.RISK,
    confidence: float = 0.82,
) -> ExtractedRiskBlocker:
    return ExtractedRiskBlocker(
        item_type=item_type,
        category=RiskBlockerCategory.BUDGET,
        severity=RiskBlockerSeverity.HIGH,
        title="Customer mentioned budget freeze through Q4",
        description="Procurement signaled budget is frozen until next FY.",
        quote="We can't move on procurement before January.",
        reasoning="Direct quote indicates timing risk on close.",
        confidence=confidence,
        source_context="Discovery Call - Apr 27",
        source_record_id="meeting-123",
        source_type=RiskBlockerSource.MEETING_GOOGLE,
        is_duplicate=False,
        duplicate_of_id=None,
        suggested_owner=None,
    )


def test_extraction_creates_item_with_open_status_and_ai_attribution() -> None:
    """AI-extracted items land directly as OPEN, attributed to the pipeline sentinel."""
    org_id = uuid4()
    deal_id = uuid4()
    service = RiskBlockerExtractionService(organisation_id=org_id)
    session = _CapturingSession()
    extracted = _build_extracted()

    item = service.create_risk_blocker_from_extracted(
        session=session,
        deal_id=deal_id,
        extracted=extracted,
        source_type=RiskBlockerSource.MEETING_GOOGLE,
        source_record_id="meeting-123",
    )

    assert isinstance(item, DealRiskBlocker)
    assert item.status == RiskBlockerStatus.OPEN
    assert item.is_ai_generated is True
    assert item.organisation_id == org_id
    assert item.deal_id == deal_id
    assert item.created_by_user_id == AUTOMATED_PIPELINE_USER_ID
    assert item.created_by_user_name == AI_EXTRACTION_ACTOR_NAME
    assert item.last_modified_by_user_id == AUTOMATED_PIPELINE_USER_ID
    assert item.last_modified_by_user_name == AI_EXTRACTION_ACTOR_NAME
    assert item.ai_confidence == 0.82


def test_extraction_seeds_opened_audit_entry_with_system_actor() -> None:
    """Initial audit entry is a system-actor 'opened' event with extraction context."""
    service = RiskBlockerExtractionService(organisation_id=uuid4())
    session = _CapturingSession()
    extracted = _build_extracted(confidence=0.91)

    item = service.create_risk_blocker_from_extracted(
        session=session,
        deal_id=uuid4(),
        extracted=extracted,
        source_type=RiskBlockerSource.MEETING_GOOGLE,
    )

    audit_trail = item.risk_blocker_data["audit_trail"]
    assert len(audit_trail) == 1
    entry = audit_trail[0]
    assert entry["action"] == "opened"
    assert entry["actor_type"] == "system"
    assert entry["actor_id"] == str(AUTOMATED_PIPELINE_USER_ID)
    assert entry["actor_name"] == AI_EXTRACTION_ACTOR_NAME
    assert entry["details"]["source"] == "ai_extraction"
    assert entry["details"]["confidence"] == 0.91
    assert "reasoning" in entry["details"]

    # Sentinel is the only modifier so far
    assert item.risk_blocker_data["unique_modifiers"] == [str(AUTOMATED_PIPELINE_USER_ID)]


def test_extraction_emits_timeline_event_matching_item_type() -> None:
    """RISK extractions emit RISK_FLAGGED, BLOCKER extractions emit BLOCKER_ADDED."""
    service = RiskBlockerExtractionService(organisation_id=uuid4())

    risk_session = _CapturingSession()
    service.create_risk_blocker_from_extracted(
        session=risk_session,
        deal_id=uuid4(),
        extracted=_build_extracted(item_type=RiskBlockerType.RISK),
        source_type=RiskBlockerSource.MEETING_GOOGLE,
    )
    risk_event_types = {getattr(o, "event_type", None) for o in risk_session.added}
    assert TimelineEventType.RISK_FLAGGED in risk_event_types

    blocker_session = _CapturingSession()
    service.create_risk_blocker_from_extracted(
        session=blocker_session,
        deal_id=uuid4(),
        extracted=_build_extracted(item_type=RiskBlockerType.BLOCKER),
        source_type=RiskBlockerSource.MEETING_GOOGLE,
    )
    blocker_event_types = {getattr(o, "event_type", None) for o in blocker_session.added}
    assert TimelineEventType.BLOCKER_ADDED in blocker_event_types


def test_automated_pipeline_user_id_is_the_project_sentinel() -> None:
    """Sanity-check the sentinel matches the codebase-wide convention."""
    assert UUID("00000000-0000-0000-0000-000000000001") == AUTOMATED_PIPELINE_USER_ID
