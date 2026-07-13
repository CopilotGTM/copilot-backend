"""Fixtures for CRM API tests (stakeholders, insights, deals).

This module provides comprehensive fake services and fixtures for testing
the stakeholder, insight, and deal API endpoints without database dependencies.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.crm.deal_notes_handler import get_deal_notes_service
from src.api.routers.crm.deals_handler import (
    get_deal_job_service,
    get_deals_access_service,
)
from src.api.routers.crm.meetings_handler import get_deal_meeting_job_service, get_meetings_service
from src.api.routers.crm.risks_blockers_handler import get_risk_blocker_service
from src.api.routers.crm.stakeholder_insights_handler import get_insight_service
from src.api.routers.crm.stakeholders_handler import (
    get_stakeholder_deal_job_service,
    get_stakeholder_job_visibility_service,
    get_stakeholder_read_service,
    get_stakeholder_write_service,
    get_stakeholders_service,
)
from src.api.routers.crm.tasks_handler import get_tasks_access_service
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
)
from src.service.crm.access.tasks_access_service import TasksAccessServiceError
from src.service.crm.deals.read_service import (
    _deal_search_cursor_name_key,
    _decode_deal_search_cursor,
    _encode_deal_search_cursor,
)
from src.service.crm.notes.contracts import NoteReadRecord
from src.service.crm.tasks.contracts import TaskReadRecord

logger = logging.getLogger(__name__)


async def _override_rate_limit():
    return None


class _EmptyScalarResult:
    def all(self):
        return []


class _EmptyExecuteResult:
    def scalars(self):
        return _EmptyScalarResult()

    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class FakeAsyncSession:
    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def scalar(self, *_args, **_kwargs):
        return 0

    async def execute(self, *_args, **_kwargs):
        return _EmptyExecuteResult()


# =============================================================================
# FAKE MODELS - Mirror the real domain models with all fields
# =============================================================================


class FakeStakeholder:
    """Fake DealStakeholder model for testing.

    Includes all fields from the real model to ensure complete test coverage.
    """

    def __init__(
        self,
        stakeholder_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        email: str | None,
        name: str | None = None,
        job_title: str | None = None,
        phone_number: str | None = None,
        linkedin_url: str | None = None,
        department: str | None = None,
        source: StakeholderSource = StakeholderSource.CRM,
        approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
        stance: StakeholderStance = StakeholderStance.UNKNOWN,
        temperature: StakeholderTemperature = StakeholderTemperature.UNKNOWN,
        decision_power: DecisionPower = DecisionPower.UNKNOWN,
        last_contact_at: datetime | None = None,
        contact_id: UUID | None = None,
        motivation: str | None = None,
        pain_points: list[str] | None = None,
        keywords: list[str] | None = None,
        external_links: list[dict[str, Any]] | None = None,
        stakeholder_engagement_history: list[dict[str, Any]] | None = None,
        stakeholder_data: dict[str, Any] | None = None,
        stakeholder_research: str | None = None,
        last_researched_at: datetime | None = None,
    ):
        self.stakeholder_id = stakeholder_id
        self.participant_id = stakeholder_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.email = email
        self.name = name
        self.job_title = job_title
        self.phone_number = phone_number
        self.linkedin_url = linkedin_url
        self.department = department
        self.source = source
        self.approval_status = approval_status
        self.stance = stance
        self.temperature = temperature
        self.decision_power = decision_power
        self.last_contact_at = last_contact_at
        self.contact_id = contact_id
        self.motivation = motivation
        self.pain_points = pain_points or []
        self.keywords = keywords or []
        self.external_links = external_links or []
        self.stakeholder_engagement_history = stakeholder_engagement_history or []
        self.stakeholder_data = stakeholder_data
        self.stakeholder_research = stakeholder_research
        self.last_researched_at = last_researched_at
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.is_deleted = False


class FakeContact:
    """Fake CRMContact model for testing."""

    def __init__(
        self,
        contact_id: UUID,
        organisation_id: UUID,
        email: str | None,
        full_name: str | None = None,
        job_title: str | None = None,
        company_name: str | None = None,
        phone: str | None = None,
        mobile_phone: str | None = None,
        linkedin_url: str | None = None,
        contact_research: str | None = None,
        last_researched_at: datetime | None = None,
        properties: dict[str, Any] | None = None,
    ):
        self.contact_id = contact_id
        self.organisation_id = organisation_id
        self.email = email
        self.full_name = full_name
        self.job_title = job_title
        self.company_name = company_name
        self.phone = phone
        self.mobile_phone = mobile_phone
        self.linkedin_url = linkedin_url
        self.contact_research = contact_research
        self.last_researched_at = last_researched_at
        self.properties = properties or {}


class FakeInsight:
    """Fake DealStakeholderInsight model for testing.

    Includes all fields from the real model to ensure complete test coverage.
    """

    def __init__(
        self,
        insight_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        insight_type: StakeholderInsightType = StakeholderInsightType.ENGAGEMENT_GAP,
        priority: StakeholderInsightPriority = StakeholderInsightPriority.MEDIUM,
        status: StakeholderInsightStatus = StakeholderInsightStatus.ACTIVE,
        title: str = "Test Insight",
        description: str | None = None,
        ai_reasoning: str = "AI detected this pattern based on engagement data.",
        ai_confidence: float = 0.85,
        stakeholder_id: UUID | None = None,
        stakeholder_name: str | None = None,
        stakeholder_title: str | None = None,
        stakeholder_email: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        metric_value: float | None = None,
        metric_unit: str | None = None,
        threshold_value: float | None = None,
        trend_direction: str | None = None,
        source_type: str | None = None,
        related_risk_blocker_id: UUID | None = None,
        identified_at: datetime | None = None,
        expires_at: datetime | None = None,
    ):
        self.insight_id = insight_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.insight_type = insight_type
        self.priority = priority
        self.status = status
        self.title = title
        self.description = description
        self.ai_reasoning = ai_reasoning
        self.ai_confidence = ai_confidence
        self.stakeholder_id = stakeholder_id
        self.stakeholder_name = stakeholder_name
        self.stakeholder_title = stakeholder_title
        self.stakeholder_email = stakeholder_email
        self.actions = actions or []
        self.metric_value = metric_value
        self.metric_unit = metric_unit
        self.threshold_value = threshold_value
        self.trend_direction = trend_direction
        self.source_type = source_type
        self.related_risk_blocker_id = related_risk_blocker_id
        self.identified_at = identified_at or datetime.now(UTC)
        self.expires_at = expires_at
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.resolved_at: datetime | None = None
        self.resolved_by: UUID | None = None
        self.resolution_notes: str | None = None


# =============================================================================
# FAKE SERVICES - Simulate real service behavior
# =============================================================================


class FakeStakeholdersService:
    """Fake StakeholdersService for testing.

    Simulates the real service behavior including filtering, sorting,
    and business logic validation.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.session = FakeAsyncSession()
        self._stakeholders: dict[UUID, list[FakeStakeholder]] = {}  # deal_id -> stakeholders
        self._contacts: dict[UUID, FakeContact] = {}  # contact_id -> contact

    async def close(self):
        """Release fake stakeholder service resources after tests complete."""
        pass

    def add_stakeholder(self, stakeholder: FakeStakeholder):
        """Add a seeded stakeholder row to the fake service."""
        if stakeholder.deal_id not in self._stakeholders:
            self._stakeholders[stakeholder.deal_id] = []
        self._stakeholders[stakeholder.deal_id].append(stakeholder)

    def add_contact(self, contact: FakeContact):
        """Add a seeded contact row for CRM enrichment lookups."""
        self._contacts[contact.contact_id] = contact

    async def list_stakeholders_by_deal(
        self,
        deal_id: UUID,
        include_pending: bool = True,
        include_rejected: bool = False,
    ):
        """Return stakeholders for a deal matching the API response format (StakeholderListResponse)."""
        stakeholders = self._stakeholders.get(deal_id, [])

        # Build list of allowed statuses
        allowed_statuses = [ApprovalStatus.APPROVED]
        if include_pending:
            allowed_statuses.append(ApprovalStatus.PENDING)
        if include_rejected:
            allowed_statuses.append(ApprovalStatus.REJECTED)

        # Convert to response format (StakeholderDetail)
        stakeholder_list = [
            {
                "participant_id": s.participant_id,
                "deal_id": s.deal_id,
                "contact_id": s.contact_id,
                "email": s.email,
                "name": s.name,
                "job_title": s.job_title,
                "phone_number": s.phone_number,
                "external_links": s.external_links,
                "stance": s.stance.value if hasattr(s.stance, "value") else s.stance,
                "decision_power": s.decision_power.value
                if hasattr(s.decision_power, "value")
                else s.decision_power,
                "motivation": s.motivation,
                "pain_points": s.pain_points,
                "keywords": s.keywords,
                "last_contact_at": s.last_contact_at,
                "temperature": s.temperature.value
                if hasattr(s.temperature, "value")
                else s.temperature,
                "source": s.source.value if hasattr(s.source, "value") else s.source,
                "approval_status": s.approval_status.value
                if hasattr(s.approval_status, "value")
                else s.approval_status,
            }
            for s in stakeholders
            if not s.is_deleted and s.approval_status in allowed_statuses
        ]

        # Return StakeholderListResponse format
        return {
            "company_domain": "example.com",
            "company_name": "Example Corp",
            "stakeholders": stakeholder_list,
        }

    def _get_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> FakeStakeholder | None:
        """Get a specific stakeholder by ID."""
        for s in self._stakeholders.get(deal_id, []):
            if s.stakeholder_id == stakeholder_id:
                return s
        return None

    async def remove_stakeholder(self, deal_id: UUID, stakeholder_id: UUID):
        """Soft-delete a stakeholder from the deal."""
        from src.service.crm.insights.stakeholders_service import StakeholdersServiceError

        stakeholder = self._get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        stakeholder.is_deleted = True
        stakeholder.updated_at = datetime.now(UTC)
        return stakeholder

    async def update_stakeholder(self, deal_id: UUID, stakeholder_id: UUID, request):
        """Partially update stakeholder fields."""
        from src.service.crm.insights.stakeholders_service import StakeholdersServiceError

        stakeholder = self._get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        updates = request.model_dump(exclude_unset=True)
        if not updates:
            raise StakeholdersServiceError("No fields provided for update", status_code=400)

        non_nullable_fields = {"stance", "decision_power"}
        null_non_nullable = [
            field for field in non_nullable_fields if field in updates and updates[field] is None
        ]
        if null_non_nullable:
            raise StakeholdersServiceError(
                "Cannot set the following fields to null: " + ", ".join(sorted(null_non_nullable)),
                status_code=400,
            )

        for field_name, value in updates.items():
            setattr(stakeholder, field_name, value)

        stakeholder_data = dict(stakeholder.stakeholder_data or {})
        manual_overrides = set(stakeholder_data.get("manual_overrides") or [])
        manual_overrides.update(updates.keys())
        stakeholder_data["manual_overrides"] = sorted(manual_overrides)
        stakeholder.stakeholder_data = stakeholder_data
        stakeholder.updated_at = datetime.now(UTC)
        return stakeholder

    async def create_stakeholder(self, deal_id: UUID, request, *, context=None):
        """Create a manual stakeholder for route contract tests."""
        from src.service.crm.insights.stakeholders_service import StakeholdersServiceError

        email = request.email.lower().strip() if request.email else None
        name = request.name
        contact_id = getattr(request, "contact_id", None)
        contact = self._contacts.get(contact_id) if contact_id else None
        if contact_id and not contact:
            raise StakeholdersServiceError("Contact not found", status_code=404)
        if not contact and request.linkedin_url:
            contact = next(
                (
                    candidate
                    for candidate in self._contacts.values()
                    if candidate.linkedin_url
                    and candidate.linkedin_url.strip().lower()
                    == request.linkedin_url.strip().lower()
                ),
                None,
            )
            if not contact and not email:
                raise StakeholdersServiceError(
                    "Unable to resolve LinkedIn URL to a contact",
                    status_code=404,
                )
        if contact:
            email = contact.email.lower().strip() if contact.email else email
            name = name or contact.full_name
        existing = [
            s
            for s in self._stakeholders.get(deal_id, [])
            if email and s.email and s.email.lower() == email
        ]
        if existing:
            stakeholder = existing[0]
            stakeholder.approval_status = ApprovalStatus.APPROVED
            stakeholder.is_deleted = False
            stakeholder.source = StakeholderSource.MANUAL
            stakeholder.name = name
            if contact:
                stakeholder.job_title = contact.job_title
                stakeholder.phone_number = contact.phone
                stakeholder.department = (contact.properties or {}).get("department")
            else:
                stakeholder.job_title = request.job_title
                stakeholder.phone_number = request.phone_number
                stakeholder.department = request.department
            stakeholder.stance = request.stance
            stakeholder.decision_power = request.decision_power
            stakeholder.motivation = request.motivation
            stakeholder.pain_points = request.pain_points or []
            stakeholder.external_links = [
                link.model_dump() for link in getattr(request, "external_links", None) or []
            ]
            stakeholder.updated_at = datetime.now(UTC)
            return stakeholder

        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=deal_id,
            organisation_id=self.organisation_id,
            email=email,
            name=name,
            job_title=request.job_title or (contact.job_title if contact else None),
            phone_number=request.phone_number or (contact.phone if contact else None),
            linkedin_url=request.linkedin_url or (contact.linkedin_url if contact else None),
            department=request.department,
            contact_id=contact.contact_id if contact else None,
            source=StakeholderSource.CRM if contact else StakeholderSource.MANUAL,
            approval_status=ApprovalStatus.APPROVED,
            stance=request.stance,
            decision_power=request.decision_power,
            motivation=request.motivation,
            pain_points=request.pain_points,
            external_links=[
                link.model_dump() for link in getattr(request, "external_links", None) or []
            ],
            stakeholder_research=contact.contact_research if contact else None,
            last_researched_at=contact.last_researched_at if contact else None,
        )
        _ = context
        self.add_stakeholder(stakeholder)
        return stakeholder

    async def trigger_deal_mapping_if_ready(self, deal_id: UUID) -> None:
        """No-op stand-in for the current post-create mapping dispatch."""
        return None

    async def get_stakeholder_detail(self, deal_id: UUID, stakeholder_id: UUID) -> dict | None:
        """Get full stakeholder detail including engagement history."""
        stakeholder = self._get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            return None

        # Format engagement history
        engagement_history = []
        for snapshot in stakeholder.stakeholder_engagement_history:
            if isinstance(snapshot, dict):
                engagement_history.append(
                    {
                        "recorded_at": snapshot.get("recorded_at"),
                        "stance": snapshot.get("stance"),
                        "temperature": snapshot.get("temperature"),
                        "decision_power": snapshot.get("decision_power"),
                    }
                )

        return {
            "participant_id": stakeholder.participant_id,
            "deal_id": stakeholder.deal_id,
            "contact_id": stakeholder.contact_id,
            "email": stakeholder.email,
            "name": stakeholder.name,
            "job_title": stakeholder.job_title,
            "phone_number": stakeholder.phone_number,
            "department": stakeholder.department,
            "external_links": stakeholder.external_links,
            "stance": stakeholder.stance.value
            if hasattr(stakeholder.stance, "value")
            else stakeholder.stance,
            "decision_power": stakeholder.decision_power.value
            if hasattr(stakeholder.decision_power, "value")
            else stakeholder.decision_power,
            "motivation": stakeholder.motivation,
            "pain_points": stakeholder.pain_points,
            "keywords": stakeholder.keywords,
            "last_contact_at": stakeholder.last_contact_at,
            "temperature": stakeholder.temperature.value
            if hasattr(stakeholder.temperature, "value")
            else stakeholder.temperature,
            "source": stakeholder.source.value
            if hasattr(stakeholder.source, "value")
            else stakeholder.source,
            "approval_status": stakeholder.approval_status.value
            if hasattr(stakeholder.approval_status, "value")
            else stakeholder.approval_status,
            "engagement_history": engagement_history,
            "created_at": stakeholder.created_at,
            "updated_at": stakeholder.updated_at,
        }

    async def add_contacts_as_stakeholders(self, deal_id: UUID, contact_ids: list[UUID]):
        """Add contacts as stakeholders."""
        added = []
        skipped = []

        existing_emails = {
            s.email.lower()
            for s in self._stakeholders.get(deal_id, [])
            if s.email and not s.is_deleted
        }

        for contact_id in contact_ids:
            contact = self._contacts.get(contact_id)
            if not contact:
                skipped.append(
                    {
                        "contact_id": contact_id,
                        "reason": "Contact not found",
                    }
                )
                continue

            if not contact.email:
                skipped.append(
                    {
                        "contact_id": contact_id,
                        "reason": "Contact has no email address",
                    }
                )
                continue

            if contact.email.lower() in existing_emails:
                skipped.append(
                    {
                        "contact_id": contact_id,
                        "reason": "Contact already exists as stakeholder",
                    }
                )
                continue

            stakeholder = FakeStakeholder(
                stakeholder_id=uuid4(),
                deal_id=deal_id,
                organisation_id=self.organisation_id,
                email=contact.email,
                name=contact.full_name,
                job_title=contact.job_title,
                source=StakeholderSource.MANUAL,
                approval_status=ApprovalStatus.APPROVED,
            )
            self.add_stakeholder(stakeholder)
            existing_emails.add(contact.email.lower())
            # Return full StakeholderDetail-compatible object
            added.append(
                {
                    "participant_id": stakeholder.participant_id,
                    "deal_id": stakeholder.deal_id,
                    "contact_id": None,
                    "email": stakeholder.email,
                    "name": stakeholder.name,
                    "job_title": stakeholder.job_title,
                    "stance": stakeholder.stance.value,
                    "decision_power": stakeholder.decision_power.value,
                    "temperature": stakeholder.temperature.value,
                    "source": stakeholder.source.value,
                    "approval_status": stakeholder.approval_status.value,
                }
            )

        return {
            "added": added,
            "skipped": skipped,
            "total_requested": len(contact_ids),
            "total_added": len(added),
            "total_skipped": len(skipped),
        }


class FakeStakeholderInsightService:
    """Fake StakeholderInsightService for testing.

    Simulates the real service behavior including filtering, sorting by priority,
    and status transitions.
    """

    def __init__(self, organisation_id: UUID, user_id: UUID, user_name: str | None = None):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_name = user_name
        self._insights: dict[UUID, list[FakeInsight]] = {}  # deal_id -> insights

    async def close(self):
        """Release fake insight service state after test scopes are torn down."""
        pass

    def add_insight(self, insight: FakeInsight):
        """Seed an insight result tied to a synthetic deal."""
        if insight.deal_id not in self._insights:
            self._insights[insight.deal_id] = []
        self._insights[insight.deal_id].append(insight)

    def _get_insight(self, insight_id: UUID, deal_id: UUID) -> FakeInsight | None:
        """Get a specific insight."""
        for i in self._insights.get(deal_id, []):
            if i.insight_id == insight_id:
                return i
        return None

    async def list_for_deal(
        self,
        deal_id: UUID,
        limit: int = 20,
        statuses: list[StakeholderInsightStatus] | None = None,
        priority: StakeholderInsightPriority | None = None,
        insight_type: StakeholderInsightType | None = None,
        include_expired: bool = False,
    ):
        """List insights for a deal with filtering and sorting."""
        insights = list(self._insights.get(deal_id, []))

        # Filter by status
        if statuses:
            insights = [i for i in insights if i.status in statuses]
        elif not include_expired:
            # Default: exclude RESOLVED and DISMISSED
            insights = [
                i
                for i in insights
                if i.status
                not in (
                    StakeholderInsightStatus.RESOLVED,
                    StakeholderInsightStatus.DISMISSED,
                    StakeholderInsightStatus.EXPIRED,
                )
            ]

        # Filter by priority
        if priority:
            insights = [i for i in insights if i.priority == priority]

        # Filter by type
        if insight_type:
            insights = [i for i in insights if i.insight_type == insight_type]

        # Sort by priority (CRITICAL > HIGH > MEDIUM > LOW) then by identified_at
        priority_order = {
            StakeholderInsightPriority.CRITICAL: 0,
            StakeholderInsightPriority.HIGH: 1,
            StakeholderInsightPriority.MEDIUM: 2,
            StakeholderInsightPriority.LOW: 3,
        }
        insights.sort(key=lambda i: (priority_order.get(i.priority, 99), i.identified_at))

        total_count = len(insights)
        insights = insights[:limit]

        # Build summary matching StakeholderInsightSummary schema
        all_insights = self._insights.get(deal_id, [])
        active_insights = [i for i in all_insights if i.status == StakeholderInsightStatus.ACTIVE]
        summary = {
            "critical_count": sum(
                1 for i in active_insights if i.priority == StakeholderInsightPriority.CRITICAL
            ),
            "high_count": sum(
                1 for i in active_insights if i.priority == StakeholderInsightPriority.HIGH
            ),
            "medium_count": sum(
                1 for i in active_insights if i.priority == StakeholderInsightPriority.MEDIUM
            ),
            "low_count": sum(
                1 for i in active_insights if i.priority == StakeholderInsightPriority.LOW
            ),
        }

        return insights, total_count, summary

    async def acknowledge(self, insight_id: UUID, deal_id: UUID):
        """Acknowledge an insight."""
        from src.service.crm.insights.stakeholder_insight_service import (
            StakeholderInsightServiceError,
        )

        insight = self._get_insight(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightServiceError("Insight not found", status_code=404)

        insight.status = StakeholderInsightStatus.ACKNOWLEDGED
        insight.updated_at = datetime.now(UTC)
        return insight

    async def dismiss(self, insight_id: UUID, deal_id: UUID):
        """Dismiss an insight."""
        from src.service.crm.insights.stakeholder_insight_service import (
            StakeholderInsightServiceError,
        )

        insight = self._get_insight(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightServiceError("Insight not found", status_code=404)

        insight.status = StakeholderInsightStatus.DISMISSED
        insight.updated_at = datetime.now(UTC)
        return insight

    async def resolve(self, insight_id: UUID, deal_id: UUID, resolution_notes: str | None = None):
        """Resolve an insight."""
        from src.service.crm.insights.stakeholder_insight_service import (
            StakeholderInsightServiceError,
        )

        insight = self._get_insight(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightServiceError("Insight not found", status_code=404)

        insight.status = StakeholderInsightStatus.RESOLVED
        insight.resolved_at = datetime.now(UTC)
        insight.resolved_by = self.user_id
        insight.resolution_notes = resolution_notes
        insight.updated_at = datetime.now(UTC)
        return insight


# =============================================================================
# TEST DATA BUILDERS - Create realistic test data
# =============================================================================


def create_stakeholder(
    deal_id: UUID,
    organisation_id: UUID,
    email: str | None = None,
    name: str | None = None,
    **kwargs,
) -> FakeStakeholder:
    """Factory function to create a stakeholder with sensible defaults."""
    return FakeStakeholder(
        stakeholder_id=uuid4(),
        deal_id=deal_id,
        organisation_id=organisation_id,
        email=email or f"stakeholder_{uuid4().hex[:8]}@example.com",
        name=name or f"Test Stakeholder {uuid4().hex[:4]}",
        **kwargs,
    )


def create_contact(
    organisation_id: UUID, email: str | None = None, full_name: str | None = None, **kwargs
) -> FakeContact:
    """Factory function to create a contact with sensible defaults."""
    return FakeContact(
        contact_id=uuid4(),
        organisation_id=organisation_id,
        email=email or f"contact_{uuid4().hex[:8]}@example.com",
        full_name=full_name or f"Test Contact {uuid4().hex[:4]}",
        **kwargs,
    )


def create_insight(
    deal_id: UUID, organisation_id: UUID, title: str | None = None, **kwargs
) -> FakeInsight:
    """Factory function to create an insight with sensible defaults."""
    return FakeInsight(
        insight_id=uuid4(),
        deal_id=deal_id,
        organisation_id=organisation_id,
        title=title or f"Test Insight {uuid4().hex[:4]}",
        **kwargs,
    )


# =============================================================================
# FAKE MODELS FOR DEALS
# =============================================================================


class FakeDeal:
    """Fake CRMDeal model for testing."""

    def __init__(
        self,
        deal_id: UUID,
        organisation_id: UUID,
        name: str = "Test Deal",
        amount: float | None = 10000.0,
        currency: str | None = "USD",
        deal_status: CRMDealStatus = CRMDealStatus.OPEN,
        probability: float | None = 50.0,
        predicted_amount: float | None = None,
        forecast_category: str | None = None,
        close_date: datetime | None = None,
        expected_close_date: datetime | None = None,
        closed_won_at: datetime | None = None,
        closed_lost_at: datetime | None = None,
        stage_name: str | None = "Qualification",
        stage_label: str | None = "Qualification",
        stage_id: UUID | None = None,
        pipeline_name: str | None = "default",
        pipeline_label: str | None = "Default Pipeline",
        pipeline_id: UUID | None = None,
        integration_id: UUID | None = None,
        owner_id: UUID | None = None,
        owner_name: str | None = None,
        owner_email: str | None = None,
        company_id: UUID | None = None,
        company_name: str | None = None,
        company_domain: str | None = None,
        company_industry: str | None = None,
        company_website: str | None = None,
        company_linkedin_url: str | None = None,
        company_country: str | None = None,
        primary_contact_id: UUID | None = None,
        primary_contact_name: str | None = None,
        primary_contact_email: str | None = None,
        deal_type: str | None = None,
        priority: str | None = None,
        lead_source: str | None = None,
        closed_reason: str | None = None,
        crm_url: str | None = None,
        description: str | None = None,
        stage_entered_at: datetime | None = None,
        stage_updated_at: datetime | None = None,
        deal_summary_data: dict | None = None,
        properties: dict | None = None,
        source_created_at: datetime | None = None,
        last_activity_at: datetime | None = None,
        source_modified_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        associated_contact_count: int | None = None,
        associated_company_count: int | None = None,
        note_count: int | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        synced_at: datetime | None = None,
    ):
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.name = name
        self.amount = amount
        self.currency = currency
        self.deal_status = deal_status
        self.probability = probability
        self.predicted_amount = predicted_amount
        self.forecast_category = forecast_category
        self.close_date = close_date
        self.expected_close_date = expected_close_date
        self.closed_won_at = closed_won_at
        self.closed_lost_at = closed_lost_at
        self.stage_name = stage_name
        self.stage_label = stage_label
        self.stage_id = stage_id
        self.pipeline_name = pipeline_name
        self.pipeline_label = pipeline_label
        self.pipeline_id = pipeline_id or uuid4()
        self.integration_id = integration_id
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.company_id = company_id
        self.company_name = company_name
        self.company_domain = company_domain
        self.company_industry = company_industry
        self.company_website = company_website
        self.company_linkedin_url = company_linkedin_url
        self.company_country = company_country
        self.primary_contact_id = primary_contact_id
        self.primary_contact_name = primary_contact_name
        self.primary_contact_email = primary_contact_email
        self.deal_type = deal_type
        self.priority = priority
        self.lead_source = lead_source
        self.closed_reason = closed_reason
        self.crm_url = crm_url
        self.description = description
        self.stage_entered_at = stage_entered_at
        self.stage_updated_at = stage_updated_at
        self.deal_summary_data = deal_summary_data
        self.properties = properties or {}
        self.source_created_at = source_created_at or datetime.now(UTC) - timedelta(days=30)
        self.last_activity_at = last_activity_at
        self.source_modified_at = source_modified_at
        self.last_seen_at = last_seen_at or datetime.now(UTC)
        self.associated_contact_count = associated_contact_count
        self.associated_company_count = associated_company_count
        self.note_count = note_count
        self.created_at = created_at or datetime.now(UTC)
        self.updated_at = updated_at or datetime.now(UTC)
        self.synced_at = synced_at or datetime.now(UTC)


class FakeCompany:
    """Fake CRMCompany model for testing."""

    def __init__(
        self,
        company_id: UUID,
        organisation_id: UUID,
        name: str | None = None,
        domain: str | None = None,
        industry: str | None = None,
        website: str | None = None,
        linkedin_url: str | None = None,
        country: str | None = None,
    ):
        self.company_id = company_id
        self.organisation_id = organisation_id
        self.name = name
        self.domain = domain
        self.industry = industry
        self.website = website
        self.linkedin_url = linkedin_url
        self.country = country


class FakeDealJob:
    """Fake DealJob model for testing."""

    def __init__(
        self,
        job_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        job_type: DealJobType = DealJobType.DEAL_SUMMARY,
        status: JobStatus = JobStatus.PENDING,
        stakeholder_id: UUID | None = None,
        user_id: UUID | None = None,
    ):
        self.job_id = job_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.job_type = job_type
        self.status = status
        self.stakeholder_id = stakeholder_id
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class FakeStakeholderJobVisibilityService:
    """Fake stakeholder job visibility orchestration for route tests."""

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.created_jobs: list[FakeDealJob] = []

    async def create_stakeholder_processing_jobs(
        self,
        deal_id: UUID,
        stakeholder_id: UUID,
    ) -> list[FakeDealJob]:
        """Return all user-visible processing jobs created by stakeholder updates."""
        jobs = [
            FakeDealJob(
                job_id=uuid4(),
                deal_id=deal_id,
                organisation_id=self.organisation_id,
                job_type=DealJobType.DEAL_EMAIL_REMATCH,
                stakeholder_id=stakeholder_id,
            ),
            FakeDealJob(
                job_id=uuid4(),
                deal_id=deal_id,
                organisation_id=self.organisation_id,
                job_type=DealJobType.DEAL_CALENDAR_REMATCH,
                stakeholder_id=stakeholder_id,
            ),
            FakeDealJob(
                job_id=uuid4(),
                deal_id=deal_id,
                organisation_id=self.organisation_id,
                job_type=DealJobType.STAKEHOLDER_SYNC,
                stakeholder_id=stakeholder_id,
            ),
        ]
        self.created_jobs.extend(jobs)
        return jobs


# =============================================================================
# FAKE SERVICES FOR DEALS
# =============================================================================


class FakeDealsAccessService:
    """Fake DealsAccessService for testing."""

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.session = FakeAsyncSession()
        self._deals: dict[UUID, FakeDeal] = {}
        self._team_members: dict[UUID, list[dict]] = {}  # deal_id -> team members
        self._companies: dict[UUID, FakeCompany] = {}
        self._contacts: dict[UUID, FakeContact] = {}

    async def close(self):
        """Release fake deals service resources after test scope ends."""
        pass

    def add_deal(self, deal: FakeDeal):
        """Seed a synthetic deal for service list/get assertions."""
        self._deals[deal.deal_id] = deal

    def add_team_member(self, deal_id: UUID, member: dict):
        """Attach one team-member row to a deal for bulk team lookups."""
        if deal_id not in self._team_members:
            self._team_members[deal_id] = []
        self._team_members[deal_id].append(member)

    def add_company(self, company: FakeCompany):
        """Retained for older tests that seed company context separately."""
        self._companies[company.company_id] = company

    def add_contact(self, contact: FakeContact):
        """Retained for tests that seed primary-contact detail hydration."""
        self._contacts[contact.contact_id] = contact

    async def get_company_by_id(self, company_id: UUID) -> FakeCompany | None:
        """Get company by local ID."""
        return self._companies.get(company_id)

    async def get_contact_by_id(self, contact_id: UUID) -> FakeContact | None:
        """Get contact by local ID."""
        return self._contacts.get(contact_id)

    async def list_deals(
        self,
        limit: int = 50,
        page: int = 1,
        statuses: list[CRMDealStatus] | None = None,
        pipeline_ids: list[UUID] | None = None,
        owner_ids: list[UUID] | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_close_date: datetime | None = None,
        max_close_date: datetime | None = None,
        min_expected_close_date: datetime | None = None,
        max_expected_close_date: datetime | None = None,
        q: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ):
        """List deals with pagination and filtering."""
        deals = list(self._deals.values())

        # Apply filters
        deals = self._apply_filters(
            deals,
            statuses,
            pipeline_ids,
            owner_ids,
            min_amount,
            max_amount,
            min_close_date,
            max_close_date,
            min_expected_close_date,
            max_expected_close_date,
            q,
        )

        if sort_by == "last_activity_at":
            dated = [d for d in deals if getattr(d, "last_activity_at", None) is not None]
            undated = [d for d in deals if getattr(d, "last_activity_at", None) is None]
            dated.sort(
                key=lambda d: (d.last_activity_at, str(d.deal_id)),
                reverse=sort_order != "asc",
            )
            deals = dated + undated
        else:
            deals.sort(key=lambda d: (d.created_at, str(d.deal_id)), reverse=True)

        total_count = len(deals)
        start = max(page - 1, 0) * limit
        return deals[start : start + limit], total_count

    async def search_deals(
        self,
        *,
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ):
        """Search deals for picker routes using the same identity fields as production."""
        deals = self._apply_filters(
            list(self._deals.values()),
            statuses=None,
            pipeline_ids=None,
            owner_ids=None,
            min_amount=None,
            max_amount=None,
            min_close_date=None,
            max_close_date=None,
            min_expected_close_date=None,
            max_expected_close_date=None,
            q=q,
        )
        deals.sort(
            key=lambda deal: (
                _deal_search_cursor_name_key(deal.name, deal.company_name),
                str(deal.deal_id),
            )
        )
        if cursor:
            cursor_name, cursor_id = _decode_deal_search_cursor(cursor)
            deals = [
                deal
                for deal in deals
                if (
                    _deal_search_cursor_name_key(deal.name, deal.company_name),
                    deal.deal_id,
                )
                > (cursor_name, cursor_id)
            ]

        page = deals[: limit + 1]
        items = page[:limit]
        next_cursor = (
            _encode_deal_search_cursor(
                _deal_search_cursor_name_key(items[-1].name, items[-1].company_name),
                items[-1].deal_id,
            )
            if len(page) > limit
            else None
        )
        return items, next_cursor

    async def get_all_deals(
        self,
        max_results: int = 500,
        statuses: list[CRMDealStatus] | None = None,
        pipeline_ids: list[UUID] | None = None,
        owner_ids: list[UUID] | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_close_date: datetime | None = None,
        max_close_date: datetime | None = None,
        min_expected_close_date: datetime | None = None,
        max_expected_close_date: datetime | None = None,
        q: str | None = None,
    ):
        """Get all deals (up to max_results) without pagination."""
        deals = list(self._deals.values())

        # Apply filters
        deals = self._apply_filters(
            deals,
            statuses,
            pipeline_ids,
            owner_ids,
            min_amount,
            max_amount,
            min_close_date,
            max_close_date,
            min_expected_close_date,
            max_expected_close_date,
            q,
        )

        # Sort by created_at desc
        deals.sort(key=lambda d: (d.created_at, str(d.deal_id)), reverse=True)

        return deals[:max_results]

    def _apply_filters(
        self,
        deals: list[FakeDeal],
        statuses: list[CRMDealStatus] | None,
        pipeline_ids: list[UUID] | None,
        owner_ids: list[UUID] | None,
        min_amount: float | None,
        max_amount: float | None,
        min_close_date: datetime | None,
        max_close_date: datetime | None,
        min_expected_close_date: datetime | None,
        max_expected_close_date: datetime | None,
        q: str | None,
    ) -> list[FakeDeal]:
        """Apply all filters to deals list."""
        if statuses:
            deals = [d for d in deals if d.deal_status in statuses]

        if pipeline_ids:
            deals = [d for d in deals if d.pipeline_id in pipeline_ids]

        if owner_ids:
            deals = [d for d in deals if d.owner_id in owner_ids]

        if min_amount is not None:
            deals = [d for d in deals if d.amount is not None and d.amount >= min_amount]

        if max_amount is not None:
            deals = [d for d in deals if d.amount is not None and d.amount <= max_amount]

        if min_close_date is not None:
            deals = [
                d for d in deals if d.close_date is not None and d.close_date >= min_close_date
            ]

        if max_close_date is not None:
            deals = [
                d for d in deals if d.close_date is not None and d.close_date <= max_close_date
            ]

        if min_expected_close_date is not None:
            deals = [
                d
                for d in deals
                if d.expected_close_date is not None
                and d.expected_close_date >= min_expected_close_date
            ]

        if max_expected_close_date is not None:
            deals = [
                d
                for d in deals
                if d.expected_close_date is not None
                and d.expected_close_date <= max_expected_close_date
            ]

        q_lower = q.strip().lower() if q else None
        if q_lower:
            deals = [
                d
                for d in deals
                if (d.name and q_lower in d.name.lower())
                or (d.company_name and q_lower in d.company_name.lower())
            ]

        return deals

    async def get_deal_by_id(self, deal_id: UUID) -> FakeDeal | None:
        """Get a single deal by ID."""
        return self._deals.get(deal_id)

    async def get_deals_stats(self) -> dict:
        """Calculate aggregate statistics for deals."""
        deals = list(self._deals.values())

        by_status: dict[str, int] = {}
        by_pipeline: dict[str, int] = {}
        total_amount = 0.0
        amount_count = 0

        for deal in deals:
            # Count by status
            status_key = (
                deal.deal_status.value
                if hasattr(deal.deal_status, "value")
                else str(deal.deal_status)
            )
            by_status[status_key] = by_status.get(status_key, 0) + 1

            # Count by pipeline
            pipeline_key = deal.pipeline_name or "unknown"
            by_pipeline[pipeline_key] = by_pipeline.get(pipeline_key, 0) + 1

            # Sum amounts
            if deal.amount is not None:
                total_amount += deal.amount
                amount_count += 1

        return {
            "total": len(deals),
            "by_status": by_status,
            "by_pipeline": by_pipeline,
            "total_amount": total_amount if amount_count > 0 else None,
            "average_amount": total_amount / amount_count if amount_count > 0 else None,
        }

    async def get_deal_team_members_batch(self, deal_ids: list[UUID]) -> dict[UUID, list[dict]]:
        """Get team members for multiple deals."""
        return {did: self._team_members.get(did, []) for did in deal_ids}

    async def list_deal_owners(
        self,
        q: str | None = None,
        limit: int = 200,
        owner_ids: list[UUID] | None = None,
    ) -> list[dict]:
        """Return distinct owners from seeded deals, with selected-owner hydration."""
        selected_ids = set(owner_ids or [])
        q_lower = q.strip().lower() if q else None
        owners: dict[UUID, dict] = {}
        for deal in self._deals.values():
            if not deal.owner_id:
                continue
            matches_query = (
                not q_lower
                or (deal.owner_name and q_lower in deal.owner_name.lower())
                or (deal.owner_email and q_lower in deal.owner_email.lower())
                or str(deal.owner_id) == q_lower
            )
            if not matches_query and deal.owner_id not in selected_ids:
                continue
            owners.setdefault(
                deal.owner_id,
                {
                    "owner_id": deal.owner_id,
                    "name": deal.owner_name,
                    "email": deal.owner_email,
                },
            )
        selected = [owner for owner in owners.values() if owner["owner_id"] in selected_ids]
        searched = [owner for owner in owners.values() if owner["owner_id"] not in selected_ids]
        searched.sort(key=lambda owner: (owner["name"] or "", str(owner["owner_id"])))
        return selected + searched[:limit]

    async def resolve_current_user_deal_owners(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[dict]:
        """Resolve current app user to seeded owner rows by ID or email."""
        email_lower = user_email.strip().lower() if user_email else None
        owners: dict[UUID, dict] = {}
        for deal in self._deals.values():
            if not deal.owner_id:
                continue
            if deal.owner_id != user_id and (
                not email_lower or not deal.owner_email or deal.owner_email.lower() != email_lower
            ):
                continue
            owners.setdefault(
                deal.owner_id,
                {
                    "owner_id": deal.owner_id,
                    "name": deal.owner_name,
                    "email": deal.owner_email,
                },
            )
        return list(owners.values())


class FakeDealJobService:
    """Fake DealJobService for testing."""

    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._jobs: dict[UUID, FakeDealJob] = {}  # job_id -> job
        self._deal_jobs: dict[UUID, list[FakeDealJob]] = {}  # deal_id -> jobs
        self._should_error = False
        self._error_message = "Service error"
        self._error_status = 500

    async def close(self):
        """Release fake job service resources after test scope ends."""
        pass

    def add_job(self, job: FakeDealJob):
        """Seed a job row and keep user and deal indexes consistent."""
        if job.user_id is None:
            job.user_id = self.user_id
        self._jobs[job.job_id] = job
        if job.deal_id not in self._deal_jobs:
            self._deal_jobs[job.deal_id] = []
        self._deal_jobs[job.deal_id].append(job)

    async def create_deal_summary_job(self, deal_id: UUID) -> FakeDealJob:
        """Create or get existing deal summary job."""
        from src.service.crm.jobs.deal_job_service import DealJobServiceError

        if self._should_error:
            raise DealJobServiceError(self._error_message, status_code=self._error_status)

        # Check for existing in-progress job
        existing_jobs = self._deal_jobs.get(deal_id, [])
        for job in existing_jobs:
            if job.job_type == DealJobType.DEAL_SUMMARY and job.status in (
                JobStatus.PENDING,
                JobStatus.IN_PROGRESS,
            ):
                return job

        # Reuse an active summary job if one exists; create a new row only when
        # no pending/in-progress job is available.
        job = FakeDealJob(
            job_id=uuid4(),
            deal_id=deal_id,
            organisation_id=self.organisation_id,
            job_type=DealJobType.DEAL_SUMMARY,
            status=JobStatus.PENDING,
        )
        self.add_job(job)
        return job

    async def get_active_deal_jobs_status(self, deal_id: UUID) -> dict | None:
        """Get status of active deal jobs."""
        jobs = self._deal_jobs.get(deal_id, [])
        active_jobs = [j for j in jobs if j.status in (JobStatus.PENDING, JobStatus.IN_PROGRESS)]

        if not active_jobs:
            return None

        result = {}
        for job in active_jobs:
            if job.job_type == DealJobType.DEAL_SUMMARY:
                result["deal_summary"] = {
                    "job_id": job.job_id,
                    "job_type": job.job_type.value,
                    "status": job.status.value if hasattr(job.status, "value") else str(job.status),
                }
        stakeholder_processing = [
            {
                "job_id": job.job_id,
                "job_type": job.job_type.value,
                "status": job.status.value if hasattr(job.status, "value") else str(job.status),
            }
            for job in active_jobs
            if job.job_type
            in {
                DealJobType.DEAL_EMAIL_REMATCH,
                DealJobType.DEAL_CALENDAR_REMATCH,
                DealJobType.STAKEHOLDER_SYNC,
            }
        ]
        if stakeholder_processing:
            result["stakeholder_processing"] = stakeholder_processing

        return result if result else None

    async def get_active_contact_research_job(
        self,
        contact_id: UUID,
    ) -> dict | None:
        """Get active contact research job for detail reconnect tests."""
        for jobs in self._deal_jobs.values():
            for job in jobs:
                if (
                    job.job_type == DealJobType.CONTACT_RESEARCH
                    and job.status in (JobStatus.PENDING, JobStatus.IN_PROGRESS)
                    and job.user_id == self.user_id
                    and (job.job_metadata or {}).get("contact_id") == str(contact_id)
                ):
                    return {
                        "job_id": job.job_id,
                        "job_type": job.job_type.value,
                        "status": job.status.value
                        if hasattr(job.status, "value")
                        else str(job.status),
                    }
        return None


# =============================================================================
# TEST DATA BUILDERS FOR DEALS
# =============================================================================


def create_deal(
    organisation_id: UUID,
    name: str | None = None,
    deal_status: CRMDealStatus = CRMDealStatus.OPEN,
    amount: float | None = 10000.0,
    **kwargs,
) -> FakeDeal:
    """Factory function to create a deal with sensible defaults."""
    return FakeDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        name=name or f"Test Deal {uuid4().hex[:4]}",
        deal_status=deal_status,
        amount=amount,
        **kwargs,
    )


@pytest.fixture(scope="function")
def test_deal_id():
    """Provide a consistent deal ID for tests."""
    return uuid4()


@pytest.fixture(scope="function")
def fake_stakeholders_service(test_user_ids):
    """Create a FakeStakeholdersService with test data."""
    service = FakeStakeholdersService(test_user_ids["organisation_id"])
    return service


@pytest.fixture(scope="function")
def fake_stakeholder_job_visibility_service(test_user_ids):
    """Create a FakeStakeholderJobVisibilityService for route contract tests."""
    return FakeStakeholderJobVisibilityService(test_user_ids["organisation_id"])


@pytest.fixture(scope="function")
def fake_insight_service(test_user_ids):
    """Create a FakeStakeholderInsightService with test data."""
    service = FakeStakeholderInsightService(
        test_user_ids["organisation_id"],
        test_user_ids["user_id"],
        "Test User",
    )
    return service


@pytest.fixture(scope="function")
def stakeholders_dependency_overrides(
    client,
    test_user_ids,
    fake_stakeholders_service,
    fake_stakeholder_job_visibility_service,
    fake_deal_job_service,
):
    """Override dependencies for stakeholders API tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        try:
            yield fake_stakeholders_service
        finally:
            await fake_stakeholders_service.close()

    async def _override_job_visibility_service():
        yield fake_stakeholder_job_visibility_service

    async def _override_deal_job_service():
        try:
            yield fake_deal_job_service
        finally:
            await fake_deal_job_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_stakeholders_service] = _override_service
    app.dependency_overrides[get_stakeholder_read_service] = _override_service
    app.dependency_overrides[get_stakeholder_write_service] = _override_service
    app.dependency_overrides[get_stakeholder_job_visibility_service] = (
        _override_job_visibility_service
    )
    app.dependency_overrides[get_stakeholder_deal_job_service] = _override_deal_job_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield
    app.dependency_overrides = {}


@pytest.fixture(scope="function")
def insights_dependency_overrides(client, test_user_ids, fake_insight_service):
    """Override dependencies for stakeholder insights API tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        try:
            yield fake_insight_service
        finally:
            await fake_insight_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_insight_service] = _override_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield
    app.dependency_overrides = {}


@pytest.fixture(scope="function")
def fake_deals_service(test_user_ids):
    """Create a FakeDealsAccessService with test data."""
    service = FakeDealsAccessService(test_user_ids["organisation_id"])
    return service


@pytest.fixture(scope="function")
def fake_deal_job_service(test_user_ids):
    """Create a FakeDealJobService with test data."""
    service = FakeDealJobService(
        test_user_ids["organisation_id"],
        test_user_ids["user_id"],
    )
    return service


@pytest.fixture(scope="function")
def deals_dependency_overrides(client, test_user_ids, fake_deals_service, fake_deal_job_service):
    """Override dependencies for deals API tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_deals_service():
        try:
            yield fake_deals_service
        finally:
            await fake_deals_service.close()

    async def _override_job_service():
        try:
            yield fake_deal_job_service
        finally:
            await fake_deal_job_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_deals_access_service] = _override_deals_service
    app.dependency_overrides[get_deal_job_service] = _override_job_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield {
        "deals_service": fake_deals_service,
        "job_service": fake_deal_job_service,
        "user_ids": test_user_ids,
    }
    app.dependency_overrides = {}


# =============================================================================
# FAKE MODELS FOR DEAL NOTES
# =============================================================================


class FakeNote:
    """Fake CRMNote model for testing."""

    def __init__(
        self,
        activity_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        body: str = "Test note content",
        subject: str | None = None,
        owner_name: str | None = None,
        owner_email: str | None = None,
        integration_id: UUID | None = None,  # None = user-created
        source_created_at: datetime | None = None,
        created_at: datetime | None = None,
        synced_at: datetime | None = None,
        is_deleted: bool = False,
        author_user_id: UUID | None = None,
        note_metadata: dict[str, Any] | None = None,
    ):
        self.activity_id = activity_id
        self.note_id = activity_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.body = body
        self.subject = subject
        self.title = subject
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.author_name = owner_name
        self.author_email = owner_email
        self.author_user_id = author_user_id
        self.integration_id = integration_id
        self.source_created_at = source_created_at or datetime.now(UTC)
        self.created_at = created_at or datetime.now(UTC)
        self.synced_at = synced_at
        self.source_modified_at = synced_at
        self.updated_at = synced_at or self.created_at
        self.is_deleted = is_deleted
        self.activity_type = "NOTE"
        self.note_metadata = note_metadata or {}


class FakeDealNotesService:
    """Fake DealNotesService for testing."""

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self._notes: dict[UUID, list[FakeNote]] = {}  # deal_id -> notes
        self._deals: set[UUID] = set()  # Valid deal IDs
        self._participants: dict[UUID, list] = {}  # deal_id -> participants
        self.session = FakeSession()
        self.read = self
        self.write = self
        self.dispatch = self

    async def close(self):
        """Release fake notes service resources for test cleanup."""
        pass

    def add_deal(self, deal_id: UUID):
        """Mark a deal as valid for testing."""
        self._deals.add(deal_id)

    def add_note(self, note: FakeNote):
        """Add a note for testing."""
        if note.deal_id not in self._notes:
            self._notes[note.deal_id] = []
        self._notes[note.deal_id].append(note)

    def add_participant(self, deal_id: UUID, email: str, name: str | None = None):
        """Add a participant for testing."""
        if deal_id not in self._participants:
            self._participants[deal_id] = []
        self._participants[deal_id].append(
            type("Participant", (), {"email": email, "name": name})()
        )

    async def get_deal_notes(
        self,
        deal_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        sort_order: str = "desc",
    ):
        """Get notes for a deal with pagination."""
        from src.service.crm.notes.contracts import NotesServiceError

        # Check deal exists
        if deal_id not in self._deals:
            raise NotesServiceError("Deal not found", status_code=404)

        notes = list(self._notes.get(deal_id, []))

        # Sort
        reverse = sort_order == "desc"
        notes.sort(key=lambda n: n.source_created_at or n.created_at, reverse=reverse)

        total_count = len(notes)

        # Apply cursor pagination (simplified)
        if cursor:
            # For testing, just skip some items
            try:
                import base64
                import json

                data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                cursor_ts = datetime.fromisoformat(data["ts"])
                if reverse:
                    notes = [n for n in notes if (n.source_created_at or n.created_at) < cursor_ts]
                else:
                    notes = [n for n in notes if (n.source_created_at or n.created_at) > cursor_ts]
            except Exception as err:
                raise NotesServiceError("Invalid cursor", status_code=400) from err

        has_more = len(notes) > limit
        items = notes[:limit]

        next_cursor = None
        if has_more and items:
            import base64
            import json

            last = items[-1]
            ts = last.source_created_at or last.created_at
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"ts": ts.isoformat(), "id": str(last.activity_id)}).encode()
            ).decode()

        participants = self._participants.get(deal_id, [])

        return [NoteReadRecord(note=note) for note in items], next_cursor, total_count, participants

    async def create_user_note(
        self,
        deal_id: UUID,
        content: str,
        subject: str | None,
        context,
    ):
        """Create a user note."""
        from src.service.crm.notes.contracts import CreateUserNoteResult, NotesServiceError

        if deal_id not in self._deals:
            raise NotesServiceError("Deal not found", status_code=404)

        existing = next(
            (
                n
                for n in self._notes.get(deal_id, [])
                if n.integration_id is None
                and not n.is_deleted
                and n.body == content
                and n.title == subject
            ),
            None,
        )
        if existing:
            return CreateUserNoteResult(note=existing, created_new=False)

        note = FakeNote(
            activity_id=uuid4(),
            deal_id=deal_id,
            organisation_id=self.organisation_id,
            body=content,
            subject=subject,
            owner_name=context.actor_name,
            owner_email=context.actor_email,
            integration_id=None,  # User-created
            note_metadata={
                "source": "user",
                "is_user_created": True,
                "created_via": context.created_via,
                "created_by_public_user_id": str(context.actor_user_id),
            },
        )
        self.add_note(note)
        return CreateUserNoteResult(note=note, created_new=True)

    def determine_note_source(self, note: FakeNote | NoteReadRecord) -> str:
        """Determine note source."""
        source_note = note.note if isinstance(note, NoteReadRecord) else note
        if source_note.integration_id is None:
            return "user"
        return "hubspot"  # Default CRM source

    async def trigger_deal_insight_pipeline(
        self,
        deal_id: UUID,
        user_id: UUID,
        trigger_source: str,
        trigger_source_id: UUID | None = None,
    ) -> dict[str, object]:
        """No-op pipeline trigger for API tests that mock note creation flow."""
        return {
            "triggered": True,
            "deal_id": str(deal_id),
            "user_id": str(user_id),
            "source": trigger_source,
        }


class FakeSession:
    """Fake async session for testing."""

    async def commit(self):
        """No-op commit to satisfy code paths needing an awaitable."""
        pass

    async def rollback(self):
        """No-op rollback to satisfy code paths needing an awaitable."""
        pass


# =============================================================================
# FAKE MODELS FOR MEETINGS
# =============================================================================


class FakeMeeting:
    """Fake meeting/calendar event model for testing."""

    def __init__(
        self,
        event_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        title: str = "Test Meeting",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        status: AssociationStatus = AssociationStatus.ACTIVE,
        attendees: list | None = None,
    ):
        self.event_id = event_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.title = title
        self.start_time = start_time or datetime.now(UTC)
        self.end_time = end_time or (datetime.now(UTC) + timedelta(hours=1))
        self.status = status
        self.attendees = attendees or []


class FakeMeetingJob:
    """Fake meeting job model for testing."""

    def __init__(
        self,
        job_id: UUID,
        deal_id: UUID,
        event_id: UUID,
        organisation_id: UUID,
        job_type: DealJobType,
        status: JobStatus = JobStatus.PENDING,
    ):
        self.job_id = job_id
        self.deal_id = deal_id
        self.event_id = event_id
        self.organisation_id = organisation_id
        self.job_type = job_type
        self.status = status


class FakeMeetingsService:
    """Fake MeetingsService for testing."""

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self._meetings: dict[UUID, list[FakeMeeting]] = {}  # deal_id -> meetings
        self._meeting_by_event: dict[UUID, FakeMeeting] = {}  # event_id -> meeting

    async def close(self):
        """Release fake meetings service resources for test cleanup."""
        pass

    def add_meeting(self, meeting: FakeMeeting):
        """Add a meeting for testing."""
        if meeting.deal_id not in self._meetings:
            self._meetings[meeting.deal_id] = []
        self._meetings[meeting.deal_id].append(meeting)
        self._meeting_by_event[meeting.event_id] = meeting

    async def list_meetings_by_deal(self, deal_id: UUID):
        """List meetings for a deal."""
        meetings = self._meetings.get(deal_id, [])
        return [
            {
                "event_id": m.event_id,
                "subject": m.title,
                "start_at": m.start_time,
                "status": m.status.value if hasattr(m.status, "value") else m.status,
                "attendees": m.attendees,
                "meeting_prep_status": "NOT_STARTED",
                "meeting_prep_data": None,
                "meeting_summary_status": "NOT_STARTED",
                "meeting_summary_data": None,
                "meeting_followup_status": "NOT_STARTED",
                "meeting_followup_data": None,
                "active_jobs": None,
            }
            for m in meetings
        ]

    async def update_meeting_status(self, deal_id: UUID, event_id: UUID, status: str):
        """Update meeting status."""
        from src.service.crm.meetings.meetings_service import MeetingsServiceError

        meeting = self._meeting_by_event.get(event_id)
        if not meeting or meeting.deal_id != deal_id:
            raise MeetingsServiceError("Meeting not found", status_code=404)

        # Validate status
        try:
            meeting.status = AssociationStatus(status)
        except ValueError as err:
            raise MeetingsServiceError(f"Invalid status: {status}", status_code=400) from err


class FakeDealMeetingJobService:
    """Fake DealMeetingJobService for testing."""

    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._jobs: dict[
            tuple[UUID, UUID, DealJobType], FakeMeetingJob
        ] = {}  # (deal_id, event_id, type) -> job
        self._valid_events: dict[UUID, UUID] = {}  # event_id -> deal_id

    async def close(self):
        """Release fake meeting-job service resources for test cleanup."""
        pass

    def add_valid_event(self, event_id: UUID, deal_id: UUID):
        """Mark an event as valid for testing."""
        self._valid_events[event_id] = deal_id

    async def create_meeting_job(
        self, deal_id: UUID, event_id: UUID, job_type: DealJobType
    ) -> FakeMeetingJob:
        """Create or get existing meeting job."""
        from src.service.crm.jobs.deal_meeting_job_service import DealMeetingJobServiceError

        # Check event exists and belongs to deal
        if event_id not in self._valid_events:
            raise DealMeetingJobServiceError("Event not found", status_code=404)
        if self._valid_events[event_id] != deal_id:
            raise DealMeetingJobServiceError("Event does not belong to deal", status_code=404)

        key = (deal_id, event_id, job_type)

        # Return existing job if in progress
        if key in self._jobs:
            existing = self._jobs[key]
            if existing.status in (JobStatus.PENDING, JobStatus.IN_PROGRESS):
                return existing

        # Keep one active job per (deal, event, type) key to avoid duplicate
        # work if the caller retries the same request.
        job = FakeMeetingJob(
            job_id=uuid4(),
            deal_id=deal_id,
            event_id=event_id,
            organisation_id=self.organisation_id,
            job_type=job_type,
            status=JobStatus.PENDING,
        )
        self._jobs[key] = job
        return job


# =============================================================================
# FAKE MODELS FOR RISKS/BLOCKERS
# =============================================================================


class FakeRiskBlocker:
    """Fake DealRiskBlocker model for testing."""

    def __init__(
        self,
        risk_blocker_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        item_type: RiskBlockerType = RiskBlockerType.RISK,
        status: RiskBlockerStatus = RiskBlockerStatus.OPEN,
        severity: RiskBlockerSeverity = RiskBlockerSeverity.MEDIUM,
        category: RiskBlockerCategory = RiskBlockerCategory.OTHER,
        title: str = "Test Risk",
        description: str | None = None,
        quote: str | None = None,
        source_type: RiskBlockerSource = RiskBlockerSource.MANUAL,
        source_record_id: str | None = None,
        source_context: str | None = None,
        is_ai_generated: bool = False,
        ai_confidence: float | None = None,
        ai_reasoning: str | None = None,
        owner_name: str | None = None,
        owner_email: str | None = None,
        assigned_team: str | None = None,
        resolution_notes: str | None = None,
        closed_at: datetime | None = None,
        closed_by_user_id: UUID | None = None,
        closed_by_user_name: str | None = None,
        created_by_user_id: UUID | None = None,
        created_by_user_name: str | None = None,
        identified_at: datetime | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        self.risk_blocker_id = risk_blocker_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.item_type = item_type
        self.status = status
        self.severity = severity
        self.category = category
        self.title = title
        self.description = description
        self.quote = quote
        self.source_type = source_type
        self.source_record_id = source_record_id
        self.source_context = source_context
        self.is_ai_generated = is_ai_generated
        self.ai_confidence = ai_confidence
        self.ai_reasoning = ai_reasoning
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.assigned_team = assigned_team
        self.resolution_notes = resolution_notes
        self.closed_at = closed_at
        self.closed_by_user_id = closed_by_user_id
        self.closed_by_user_name = closed_by_user_name
        self.created_by_user_id = created_by_user_id
        self.created_by_user_name = created_by_user_name
        self.identified_at = identified_at or datetime.now(UTC)
        self.created_at = created_at or datetime.now(UTC)
        self.updated_at = updated_at or datetime.now(UTC)
        self.last_modified_by_user_id: UUID | None = None
        self.last_modified_by_user_name: str | None = None
        self.extraction_run_id: UUID | None = None
        # Seed the initial open event to mirror the real service's audit trail.
        self.audit_trail: list[dict] = [
            {
                "action": "opened",
                "timestamp": self.created_at.isoformat(),
                "actor_type": "system" if is_ai_generated else "user",
                "actor_id": str(created_by_user_id) if created_by_user_id else None,
                "actor_name": created_by_user_name,
                "details": {
                    "source": "ai_extraction" if is_ai_generated else "manual",
                },
            }
        ]
        self.modification_count: int = 0
        self.unique_modifier_count: int = 1 if created_by_user_id else 0


class FakeRiskBlockerService:
    """Fake RiskBlockerService for testing."""

    def __init__(self, organisation_id: UUID, user_id: UUID, user_name: str | None = None):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_name = user_name
        self._items: dict[UUID, FakeRiskBlocker] = {}  # risk_blocker_id -> item
        self._deal_items: dict[UUID, list[UUID]] = {}  # deal_id -> item_ids
        self._valid_deals: set[UUID] = set()

    async def close(self):
        """Release fake risk/blocker service resources for test cleanup."""
        pass

    def add_deal(self, deal_id: UUID):
        """Mark a deal as valid."""
        self._valid_deals.add(deal_id)

    def add_item(self, item: FakeRiskBlocker):
        """Add a risk/blocker for testing."""
        self._items[item.risk_blocker_id] = item
        if item.deal_id not in self._deal_items:
            self._deal_items[item.deal_id] = []
        self._deal_items[item.deal_id].append(item.risk_blocker_id)

    async def list_for_deal(
        self,
        deal_id: UUID,
        limit: int = 20,
        page: int = 1,
        item_type: RiskBlockerType | None = None,
        statuses: list[RiskBlockerStatus] | None = None,
        severity: RiskBlockerSeverity | None = None,
        category: RiskBlockerCategory | None = None,
        include_resolved: bool = False,
        sort_order: str = "desc",
    ):
        """List risks/blockers for a deal."""
        item_ids = self._deal_items.get(deal_id, [])
        items = [self._items[item_id] for item_id in item_ids if item_id in self._items]

        # Filter by type
        if item_type:
            items = [i for i in items if i.item_type == item_type]

        # Filter by status
        if statuses:
            items = [i for i in items if i.status in statuses]
        elif not include_resolved:
            excluded = {RiskBlockerStatus.RESOLVED, RiskBlockerStatus.DISMISSED}
            items = [i for i in items if i.status not in excluded]

        # Filter by severity
        if severity:
            items = [i for i in items if i.severity == severity]

        # Filter by category
        if category:
            items = [i for i in items if i.category == category]

        # Sort
        reverse = sort_order == "desc"
        items.sort(key=lambda i: i.created_at, reverse=reverse)

        total_count = len(items)
        offset = max(page - 1, 0) * limit
        items = items[offset : offset + limit]

        # Build summary
        all_items = [
            self._items[item_id]
            for item_id in self._deal_items.get(deal_id, [])
            if item_id in self._items
        ]
        summary = {
            "total_count": len(all_items),
            "risks_count": sum(1 for i in all_items if i.item_type == RiskBlockerType.RISK),
            "blockers_count": sum(1 for i in all_items if i.item_type == RiskBlockerType.BLOCKER),
            "open_count": sum(1 for i in all_items if i.status == RiskBlockerStatus.OPEN),
            "in_progress_count": sum(
                1 for i in all_items if i.status == RiskBlockerStatus.IN_PROGRESS
            ),
            "resolved_count": sum(1 for i in all_items if i.status == RiskBlockerStatus.RESOLVED),
            "critical_count": sum(
                1 for i in all_items if i.severity == RiskBlockerSeverity.CRITICAL
            ),
            "high_count": sum(1 for i in all_items if i.severity == RiskBlockerSeverity.HIGH),
        }

        # Convert to list items format
        list_items = [
            {
                "risk_blocker_id": i.risk_blocker_id,
                "item_type": i.item_type,
                "status": i.status,
                "severity": i.severity,
                "title": i.title,
                "quote": i.quote,
                "source_type": i.source_type,
                "source_context": i.source_context,
                "owner_name": i.owner_name,
                "is_ai_generated": i.is_ai_generated,
            }
            for i in items
        ]

        return list_items, total_count, summary

    async def get_by_id(self, risk_blocker_id: UUID):
        """Get a risk/blocker by ID."""
        item = self._items.get(risk_blocker_id)
        if not item or item.organisation_id != self.organisation_id:
            return None

        return {
            "risk_blocker_id": item.risk_blocker_id,
            "deal_id": item.deal_id,
            "item_type": item.item_type,
            "status": item.status,
            "severity": item.severity,
            "category": item.category,
            "title": item.title,
            "description": item.description,
            "quote": item.quote,
            "source_type": item.source_type,
            "source_record_id": item.source_record_id,
            "source_context": item.source_context,
            "is_ai_generated": item.is_ai_generated,
            "ai_confidence": item.ai_confidence,
            "ai_reasoning": item.ai_reasoning,
            "extraction_run_id": item.extraction_run_id,
            "owner_name": item.owner_name,
            "owner_email": item.owner_email,
            "assigned_team": item.assigned_team,
            "resolution_notes": item.resolution_notes,
            "closed_at": item.closed_at,
            "closed_by_user_id": item.closed_by_user_id,
            "closed_by_user_name": item.closed_by_user_name,
            "created_by_user_id": item.created_by_user_id,
            "created_by_user_name": item.created_by_user_name,
            "last_modified_by_user_id": item.last_modified_by_user_id,
            "last_modified_by_user_name": item.last_modified_by_user_name,
            "identified_at": item.identified_at,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "audit_trail": item.audit_trail,
            "modification_count": item.modification_count,
            "unique_modifier_count": item.unique_modifier_count,
        }

    async def create(
        self,
        deal_id: UUID,
        item_type: RiskBlockerType,
        title: str,
        description: str | None = None,
        category: RiskBlockerCategory = RiskBlockerCategory.OTHER,
        severity: RiskBlockerSeverity = RiskBlockerSeverity.MEDIUM,
        owner_name: str | None = None,
        owner_email: str | None = None,
        assigned_team: str | None = None,
    ):
        """Create a risk/blocker."""
        item = FakeRiskBlocker(
            risk_blocker_id=uuid4(),
            deal_id=deal_id,
            organisation_id=self.organisation_id,
            item_type=item_type,
            status=RiskBlockerStatus.OPEN,  # Manual items start as OPEN
            title=title,
            description=description,
            category=category,
            severity=severity,
            owner_name=owner_name,
            owner_email=owner_email,
            assigned_team=assigned_team,
            created_by_user_id=self.user_id,
            created_by_user_name=self.user_name,
            source_type=RiskBlockerSource.MANUAL,
        )
        self.add_item(item)
        return await self.get_by_id(item.risk_blocker_id)

    async def update(self, risk_blocker_id: UUID, updates: dict):
        """Update a risk/blocker."""
        item = self._items.get(risk_blocker_id)
        if not item or item.organisation_id != self.organisation_id:
            return None

        for key, value in updates.items():
            if hasattr(item, key):
                setattr(item, key, value)
        item.updated_at = datetime.now(UTC)
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name

        return await self.get_by_id(risk_blocker_id)

    def _append_audit(self, item: FakeRiskBlocker, action: str, details: dict) -> None:
        """Append a fake audit entry — mirrors RiskBlockerService._add_audit_entry."""
        item.audit_trail.append(
            {
                "action": action,
                "timestamp": datetime.now(UTC).isoformat(),
                "actor_type": "user",
                "actor_id": str(self.user_id),
                "actor_name": self.user_name,
                "details": details,
            }
        )
        item.modification_count = len(
            [e for e in item.audit_trail if e.get("action") not in ("opened", "created")]
        )

    async def dismiss(self, risk_blocker_id: UUID):
        """Dismiss a risk/blocker."""
        from src.service.crm.insights.risk_blocker_service import RiskBlockerNotFoundError

        item = self._items.get(risk_blocker_id)
        if not item or item.organisation_id != self.organisation_id:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        previous_status = item.status.value
        now = datetime.now(UTC)
        item.status = RiskBlockerStatus.DISMISSED
        item.closed_at = now
        item.closed_by_user_id = self.user_id
        item.closed_by_user_name = self.user_name
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = now
        self._append_audit(item, "dismissed", {"previous_status": previous_status})

        return await self.get_by_id(risk_blocker_id)

    async def resolve(self, risk_blocker_id: UUID, resolution_notes: str):
        """Resolve a risk/blocker."""
        from src.service.crm.insights.risk_blocker_service import RiskBlockerNotFoundError

        item = self._items.get(risk_blocker_id)
        if not item or item.organisation_id != self.organisation_id:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        previous_status = item.status.value
        now = datetime.now(UTC)
        item.status = RiskBlockerStatus.RESOLVED
        item.resolution_notes = resolution_notes
        item.closed_at = now
        item.closed_by_user_id = self.user_id
        item.closed_by_user_name = self.user_name
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = now
        self._append_audit(
            item,
            "resolved",
            {"previous_status": previous_status, "resolution_notes": resolution_notes},
        )

        return await self.get_by_id(risk_blocker_id)

    async def reopen(self, risk_blocker_id: UUID):
        """Reopen a risk/blocker."""
        from src.service.crm.insights.risk_blocker_service import (
            InvalidStatusTransitionError,
            RiskBlockerNotFoundError,
        )

        item = self._items.get(risk_blocker_id)
        if not item or item.organisation_id != self.organisation_id:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        # Can only reopen from RESOLVED or DISMISSED
        if item.status not in (RiskBlockerStatus.RESOLVED, RiskBlockerStatus.DISMISSED):
            raise InvalidStatusTransitionError(item.status.value, "OPEN")

        previous_status = item.status.value
        item.status = RiskBlockerStatus.OPEN
        item.resolution_notes = None
        item.closed_at = None
        item.closed_by_user_id = None
        item.closed_by_user_name = None
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = datetime.now(UTC)
        self._append_audit(item, "reopened", {"previous_status": previous_status})

        return await self.get_by_id(risk_blocker_id)

    async def delete(self, risk_blocker_id: UUID):
        """Delete a risk/blocker."""
        from src.service.crm.insights.risk_blocker_service import RiskBlockerNotFoundError

        item = self._items.get(risk_blocker_id)
        if not item or item.organisation_id != self.organisation_id:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        del self._items[risk_blocker_id]
        if item.deal_id in self._deal_items:
            self._deal_items[item.deal_id] = [
                item_id for item_id in self._deal_items[item.deal_id] if item_id != risk_blocker_id
            ]


# =============================================================================
# FIXTURES FOR NOTES, MEETINGS, RISKS/BLOCKERS
# =============================================================================


@pytest.fixture(scope="function")
def fake_notes_service(test_user_ids):
    """Create a FakeDealNotesService."""
    return FakeDealNotesService(test_user_ids["organisation_id"])


@pytest.fixture(scope="function")
def fake_meetings_service(test_user_ids):
    """Create a FakeMeetingsService."""
    return FakeMeetingsService(test_user_ids["organisation_id"])


@pytest.fixture(scope="function")
def fake_meeting_job_service(test_user_ids):
    """Create a FakeDealMeetingJobService."""
    return FakeDealMeetingJobService(
        test_user_ids["organisation_id"],
        test_user_ids["user_id"],
    )


@pytest.fixture(scope="function")
def fake_risk_blocker_service(test_user_ids):
    """Create a FakeRiskBlockerService."""
    return FakeRiskBlockerService(
        test_user_ids["organisation_id"],
        test_user_ids["user_id"],
        "Test User",
    )


@pytest.fixture(scope="function")
def notes_dependency_overrides(client, test_user_ids, fake_notes_service):
    """Override dependencies for deal notes tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        try:
            yield fake_notes_service
        finally:
            await fake_notes_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_deal_notes_service] = _override_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield {
        "service": fake_notes_service,
        "user_ids": test_user_ids,
    }
    app.dependency_overrides = {}


@pytest.fixture(scope="function")
def meetings_dependency_overrides(
    client, test_user_ids, fake_meetings_service, fake_meeting_job_service
):
    """Override dependencies for meetings tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_meetings_service():
        try:
            yield fake_meetings_service
        finally:
            await fake_meetings_service.close()

    async def _override_job_service():
        try:
            yield fake_meeting_job_service
        finally:
            await fake_meeting_job_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_meetings_service] = _override_meetings_service
    app.dependency_overrides[get_deal_meeting_job_service] = _override_job_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield {
        "meetings_service": fake_meetings_service,
        "job_service": fake_meeting_job_service,
        "user_ids": test_user_ids,
    }
    app.dependency_overrides = {}


@pytest.fixture(scope="function")
def risks_dependency_overrides(client, test_user_ids, fake_risk_blocker_service):
    """Override dependencies for risks/blockers tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        try:
            yield fake_risk_blocker_service
        finally:
            await fake_risk_blocker_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_risk_blocker_service] = _override_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield {
        "service": fake_risk_blocker_service,
        "user_ids": test_user_ids,
    }
    app.dependency_overrides = {}


# =============================================================================
# FACTORY FUNCTIONS FOR TEST DATA
# =============================================================================


def create_note(
    deal_id: UUID,
    organisation_id: UUID,
    content: str = "Test note content",
    subject: str | None = None,
    is_user_created: bool = True,
    **kwargs,
) -> FakeNote:
    """Factory function to create a note."""
    return FakeNote(
        activity_id=uuid4(),
        deal_id=deal_id,
        organisation_id=organisation_id,
        body=content,
        subject=subject,
        integration_id=None if is_user_created else uuid4(),
        **kwargs,
    )


def create_meeting(
    deal_id: UUID,
    organisation_id: UUID,
    title: str = "Test Meeting",
    **kwargs,
) -> FakeMeeting:
    """Factory function to create a meeting."""
    return FakeMeeting(
        event_id=uuid4(),
        deal_id=deal_id,
        organisation_id=organisation_id,
        title=title,
        **kwargs,
    )


def create_risk_blocker(
    deal_id: UUID,
    organisation_id: UUID,
    title: str = "Test Risk",
    item_type: RiskBlockerType = RiskBlockerType.RISK,
    status: RiskBlockerStatus = RiskBlockerStatus.OPEN,
    **kwargs,
) -> FakeRiskBlocker:
    """Factory function to create a risk/blocker."""
    return FakeRiskBlocker(
        risk_blocker_id=uuid4(),
        deal_id=deal_id,
        organisation_id=organisation_id,
        title=title,
        item_type=item_type,
        status=status,
        **kwargs,
    )


# =============================================================================
# FAKE MODELS - Tasks
# =============================================================================


class FakeTask:
    """Fake CRMTask model for testing."""

    def __init__(
        self,
        activity_id: UUID,
        deal_id: UUID,
        organisation_id: UUID,
        subject: str | None = "Test Task",
        body: str | None = None,
        status: str = "NOT_STARTED",
        activity_type: str = "TASK",
        priority: str = "MEDIUM",
        due_date: datetime | None = None,
        due_at: datetime | None = None,
        is_all_day: bool = False,
        completed_date: datetime | None = None,
        reminder_date: datetime | None = None,
        owner_id: UUID | None = None,
        owner_name: str | None = None,
        owner_email: str | None = None,
        external_assignee_email: str | None = None,
        external_assignee_name: str | None = None,
        created_by_user_id: UUID | None = None,
        related_name: str | None = None,
        is_deleted: bool = False,
        is_deleted_remote: bool = False,
        integration_id: UUID | None = None,
        properties: dict | None = None,
        metrics: dict | None = None,
        source_created_at: datetime | None = None,
        source_modified_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        **kwargs,
    ):
        self.activity_id = activity_id
        self.task_id = activity_id
        self.deal_id = deal_id
        self.organisation_id = organisation_id
        self.subject = subject
        self.title = subject
        self.body = body
        self.description = body
        self.status = status
        self.activity_type = activity_type
        self.task_type = activity_type
        self.priority = priority
        self.due_at = due_at if due_at is not None else None if is_all_day else due_date
        self.due_date = due_date.date() if due_date else None
        self.is_all_day = is_all_day
        self.completed_date = completed_date
        self.completed_at = completed_date
        self.reminder_date = reminder_date
        self.reminder_at = reminder_date
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.assigned_to_user_id = owner_id
        self.assigned_to_name = owner_name
        self.assigned_to_email = owner_email
        self.owner_user_id = owner_id
        self.external_assignee_email = external_assignee_email
        self.external_assignee_name = external_assignee_name
        self.created_by_user_id = created_by_user_id
        self.related_name = related_name
        self.is_deleted = is_deleted
        self.is_deleted_remote = is_deleted_remote
        self.integration_id = integration_id
        self.properties = properties or {}
        self.task_metadata = dict(self.properties)
        self.task_metadata.setdefault("task_type", activity_type)
        if external_assignee_email or external_assignee_name:
            self.task_metadata["assignee_kind"] = "external"
            self.assigned_to_email = external_assignee_email
            self.assigned_to_name = external_assignee_name
        self.metrics = metrics
        self.source_created_at = source_created_at
        self.source_modified_at = source_modified_at
        self.last_seen_at = last_seen_at
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.synced_at = datetime.now(UTC)


class FakeTasksAccessService:
    """Fake TasksAccessService for testing.

    Simulates the real service behavior including filtering, sorting, and pagination.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.session = FakeAsyncSession()
        self._tasks: dict[UUID, list[FakeTask]] = {}  # deal_id -> tasks
        self.list_tasks_calls: list[dict] = []

    def add_task(self, task: FakeTask):
        """Add a task to the fake store."""
        if task.deal_id not in self._tasks:
            self._tasks[task.deal_id] = []
        self._tasks[task.deal_id].append(task)

    def clear(self):
        """Clear all tasks."""
        self._tasks.clear()

    async def close(self):
        """No-op for test cleanup."""
        pass

    async def list_tasks_by_deal(
        self,
        deal_id: UUID,
        limit: int = 50,
        page: int = 1,
        statuses: list[str] | None = None,
        priorities: list[str] | None = None,
        task_types: list[str] | None = None,
    ) -> tuple[list[TaskReadRecord], int]:
        """List tasks for a deal with filtering and pagination."""
        tasks = [t for t in self._tasks.get(deal_id, []) if not t.is_deleted]

        # Filter by task type - only TASK/TODO allowed
        allowed_types = {"TASK", "TODO"}
        if task_types:
            requested = {t for t in task_types if t} & allowed_types
            if not requested:
                return [], 0
            tasks = [t for t in tasks if t.activity_type in requested]
        else:
            tasks = [t for t in tasks if t.activity_type in allowed_types]

        # Apply status filter
        if statuses:
            tasks = [t for t in tasks if t.status in statuses]

        # Apply priority filter
        if priorities:
            tasks = [t for t in tasks if t.priority in priorities]

        # Sort by created_at DESC, activity_id DESC
        tasks.sort(key=lambda t: (t.created_at, t.activity_id), reverse=True)

        total_count = len(tasks)
        offset = max(page - 1, 0) * limit
        return [
            TaskReadRecord(task=task, deal_name=task.related_name)
            for task in tasks[offset : offset + limit]
        ], total_count

    async def list_tasks(
        self,
        **kwargs,
    ) -> tuple[list[TaskReadRecord], int]:
        self.list_tasks_calls.append(kwargs)
        if kwargs.get("scope") == "assigned_to_user" and not kwargs.get("assigned_to_key"):
            raise TasksAccessServiceError(
                "assigned_to_key is required when scope=assigned_to_user",
                status_code=400,
            )
        tasks = [
            task
            for tasks_list in self._tasks.values()
            for task in tasks_list
            if not task.is_deleted and task.activity_type in {"TASK", "TODO"}
        ]
        total_count = len(tasks)
        return [
            TaskReadRecord(task=task, deal_name=task.related_name)
            for task in tasks[: kwargs.get("limit", 50)]
        ], total_count

    async def resolve_current_user_task_assignees(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[dict]:
        return [
            {
                "kind": "internal_user",
                "filter_key": f"email:{user_email.lower()}",
                "name": "Test User",
                "email": user_email.lower() if user_email else None,
                "public_user_id": user_id,
                "crm_user_id": user_id,
                "task_count": 3,
            }
        ]

    async def get_task_by_id(self, task_id: UUID) -> TaskReadRecord | None:
        """Get a task by ID."""
        for tasks_list in self._tasks.values():
            for task in tasks_list:
                if (
                    task.activity_id == task_id
                    and not task.is_deleted
                    and task.activity_type in {"TASK", "TODO"}
                ):
                    return TaskReadRecord(task=task, deal_name=task.related_name)
        return None


# =============================================================================
# FIXTURES - Tasks
# =============================================================================


@pytest.fixture(scope="function")
def fake_tasks_service(test_user_ids):
    """Create a fresh FakeTasksAccessService for each test."""
    service = FakeTasksAccessService(test_user_ids["organisation_id"])
    yield service
    service.clear()


@pytest.fixture(scope="function")
def tasks_dependency_overrides(client, test_user_ids, fake_tasks_service):
    """Override dependencies for tasks tests."""

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        try:
            yield fake_tasks_service
        finally:
            await fake_tasks_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_tasks_access_service] = _override_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield {
        "service": fake_tasks_service,
        "user_ids": test_user_ids,
    }
    app.dependency_overrides = {}


# =============================================================================
# FACTORY FUNCTIONS - Tasks
# =============================================================================


def create_task(
    deal_id: UUID,
    organisation_id: UUID,
    subject: str = "Test Task",
    status: str = "NOT_STARTED",
    priority: str = "MEDIUM",
    activity_type: str = "TASK",
    due_date: datetime | None = None,
    due_at: datetime | None = None,
    is_all_day: bool = False,
    **kwargs,
) -> FakeTask:
    """Factory function to create a task."""
    return FakeTask(
        activity_id=uuid4(),
        deal_id=deal_id,
        organisation_id=organisation_id,
        subject=subject,
        status=status,
        priority=priority,
        activity_type=activity_type,
        due_date=due_date,
        due_at=due_at,
        is_all_day=is_all_day,
        **kwargs,
    )
