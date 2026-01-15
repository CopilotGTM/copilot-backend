"""Comprehensive tests for Stakeholder Insights API endpoints.

Tests cover:
- Response structure validation (all fields present with correct types)
- Business logic (filtering, sorting by priority, status transitions)
- Edge cases (empty data, boundaries, all priority/status combinations)
- Error handling (invalid IDs, not found scenarios)
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
)

from tests.api.routers.crm.conftest import FakeInsight, create_insight


class TestListStakeholderInsights:
    """Tests for GET /{deal_id}/stakeholder-insights endpoint."""

    def test_returns_empty_list_for_deal_with_no_insights(
        self,
        client: TestClient,
        insights_dependency_overrides,
        test_deal_id,
    ):
        """Verify empty response structure when deal has no insights."""
        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["data"]["insights"] == []
        assert body["data"]["total_count"] == 0
        assert "summary" in body["data"]
        assert body["data"]["summary"]["critical_count"] == 0
        assert body["data"]["summary"]["high_count"] == 0
        assert body["data"]["summary"]["medium_count"] == 0
        assert body["data"]["summary"]["low_count"] == 0

    def test_returns_all_insight_fields_correctly(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify all insight fields are present and correctly typed in response."""
        stakeholder_id = uuid4()
        related_risk_id = uuid4()

        insight = FakeInsight(
            insight_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            insight_type=StakeholderInsightType.ENGAGEMENT_GAP,
            priority=StakeholderInsightPriority.HIGH,
            status=StakeholderInsightStatus.ACTIVE,
            title="Critical Engagement Gap Detected",
            description="Key stakeholder has not engaged in 30 days",
            ai_reasoning="Based on historical patterns, this stakeholder typically responds within 7 days.",
            ai_confidence=0.92,
            stakeholder_id=stakeholder_id,
            stakeholder_name="John Decision Maker",
            stakeholder_title="VP of Engineering",
            stakeholder_email="john@company.com",
            actions=[
                {"action_type": "SCHEDULE_MEETING", "label": "Schedule Check-in", "parameters": {}},
                {
                    "action_type": "SEND_EMAIL",
                    "label": "Send Follow-up",
                    "parameters": {"template": "follow_up"},
                },
            ],
            metric_value=30.0,
            metric_unit="days",
            threshold_value=14.0,
            trend_direction="increasing",
            source_type="engagement_analysis",
            related_risk_blocker_id=related_risk_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]["insights"]) == 1

        data = body["data"]["insights"][0]
        # Core fields
        assert data["insight_id"] == str(insight.insight_id)
        assert data["insight_type"] == "ENGAGEMENT_GAP"
        assert data["priority"] == "HIGH"
        assert data["status"] == "ACTIVE"
        assert data["title"] == "Critical Engagement Gap Detected"
        assert data["description"] == "Key stakeholder has not engaged in 30 days"
        assert (
            data["ai_reasoning"]
            == "Based on historical patterns, this stakeholder typically responds within 7 days."
        )
        assert data["ai_confidence"] == 0.92

        # Stakeholder context
        assert data["stakeholder_id"] == str(stakeholder_id)
        assert data["stakeholder_name"] == "John Decision Maker"
        assert data["stakeholder_title"] == "VP of Engineering"
        assert data["stakeholder_email"] == "john@company.com"

        # Actions
        assert len(data["actions"]) == 2
        assert data["actions"][0]["action_type"] == "SCHEDULE_MEETING"
        assert data["actions"][1]["action_type"] == "SEND_EMAIL"

        # Metrics
        assert data["metric_value"] == 30.0
        assert data["metric_unit"] == "days"
        assert data["threshold_value"] == 14.0
        assert data["trend_direction"] == "increasing"

        # Source
        assert data["source_type"] == "engagement_analysis"
        assert data["related_risk_blocker_id"] == str(related_risk_id)

        # Timestamps
        assert "identified_at" in data
        assert "expires_at" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_sorts_insights_by_priority_critical_first(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify insights are sorted by priority: CRITICAL > HIGH > MEDIUM > LOW."""
        # Add insights in reverse priority order
        priorities = [
            (StakeholderInsightPriority.LOW, "Low Priority"),
            (StakeholderInsightPriority.HIGH, "High Priority"),
            (StakeholderInsightPriority.CRITICAL, "Critical Priority"),
            (StakeholderInsightPriority.MEDIUM, "Medium Priority"),
        ]

        for priority, title in priorities:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                priority=priority,
                title=title,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        body = resp.json()
        insights = body["data"]["insights"]
        assert len(insights) == 4

        # Verify order: CRITICAL, HIGH, MEDIUM, LOW
        assert insights[0]["priority"] == "CRITICAL"
        assert insights[1]["priority"] == "HIGH"
        assert insights[2]["priority"] == "MEDIUM"
        assert insights[3]["priority"] == "LOW"

    def test_secondary_sort_by_identified_at_within_priority(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify insights with same priority are sorted by identified_at (oldest first)."""
        base_time = datetime.now(UTC)

        # Add HIGH priority insights with different times
        for _i, delta in enumerate([3, 1, 2]):  # Add in random order
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                priority=StakeholderInsightPriority.HIGH,
                title=f"Insight {delta}",
                identified_at=base_time - timedelta(hours=delta),
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]

        # Should be sorted by identified_at (oldest first)
        assert insights[0]["title"] == "Insight 3"  # 3 hours ago
        assert insights[1]["title"] == "Insight 2"  # 2 hours ago
        assert insights[2]["title"] == "Insight 1"  # 1 hour ago

    def test_excludes_resolved_and_dismissed_by_default(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify RESOLVED and DISMISSED insights are excluded by default."""
        statuses = [
            (StakeholderInsightStatus.ACTIVE, "Active"),
            (StakeholderInsightStatus.ACKNOWLEDGED, "Acknowledged"),
            (StakeholderInsightStatus.RESOLVED, "Resolved"),
            (StakeholderInsightStatus.DISMISSED, "Dismissed"),
        ]

        for status, title in statuses:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                status=status,
                title=title,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]

        # Only ACTIVE and ACKNOWLEDGED should be returned
        assert len(insights) == 2
        titles = {i["title"] for i in insights}
        assert titles == {"Active", "Acknowledged"}

    def test_includes_resolved_when_include_expired_true(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify include_expired=true includes resolved/dismissed insights."""
        for status in StakeholderInsightStatus:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                status=status,
                title=f"Status {status.value}",
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights",
            params={"include_expired": True},
        )

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]

        # All statuses should be included
        assert len(insights) >= 4  # ACTIVE, ACKNOWLEDGED, RESOLVED, DISMISSED

    def test_filters_by_single_status(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify filtering by a single status value."""
        for status in [StakeholderInsightStatus.ACTIVE, StakeholderInsightStatus.ACKNOWLEDGED]:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                status=status,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights",
            params={"status": "ACTIVE"},
        )

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]
        assert len(insights) == 1
        assert insights[0]["status"] == "ACTIVE"

    def test_filters_by_multiple_statuses(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify filtering by comma-separated status values."""
        for status in StakeholderInsightStatus:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                status=status,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights",
            params={"status": "ACTIVE,ACKNOWLEDGED"},
        )

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]
        statuses = {i["status"] for i in insights}
        assert statuses == {"ACTIVE", "ACKNOWLEDGED"}

    def test_filters_by_priority(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify filtering by priority level."""
        for priority in StakeholderInsightPriority:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                priority=priority,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights",
            params={"priority": "CRITICAL"},
        )

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]
        assert len(insights) == 1
        assert insights[0]["priority"] == "CRITICAL"

    def test_filters_by_insight_type(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify filtering by insight type."""
        types = [
            StakeholderInsightType.ENGAGEMENT_GAP,
            StakeholderInsightType.SENTIMENT_DECLINING,
            StakeholderInsightType.CHAMPION_OPPORTUNITY,
        ]

        for insight_type in types:
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                insight_type=insight_type,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights",
            params={"insight_type": "ENGAGEMENT_GAP"},
        )

        assert resp.status_code == 200
        insights = resp.json()["data"]["insights"]
        assert len(insights) == 1
        assert insights[0]["insight_type"] == "ENGAGEMENT_GAP"

    def test_respects_limit_parameter(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify limit parameter restricts number of results."""
        # Add 10 insights
        for i in range(10):
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                title=f"Insight {i}",
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights",
            params={"limit": 3},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]["insights"]) == 3
        assert body["data"]["total_count"] == 10  # Total count reflects all matching

    def test_returns_correct_summary_counts(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify summary counts are accurate for active insights only."""
        # Add insights with different priorities (all ACTIVE)
        priority_counts = {
            StakeholderInsightPriority.CRITICAL: 2,
            StakeholderInsightPriority.HIGH: 3,
            StakeholderInsightPriority.MEDIUM: 1,
            StakeholderInsightPriority.LOW: 4,
        }

        for priority, count in priority_counts.items():
            for _ in range(count):
                insight = create_insight(
                    deal_id=test_deal_id,
                    organisation_id=test_user_ids["organisation_id"],
                    priority=priority,
                    status=StakeholderInsightStatus.ACTIVE,
                )
                fake_insight_service.add_insight(insight)

        # Add some non-ACTIVE insights (should not be in summary)
        for _ in range(5):
            insight = create_insight(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                priority=StakeholderInsightPriority.CRITICAL,
                status=StakeholderInsightStatus.RESOLVED,
            )
            fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        summary = resp.json()["data"]["summary"]
        assert summary["critical_count"] == 2  # Only ACTIVE critical
        assert summary["high_count"] == 3
        assert summary["medium_count"] == 1
        assert summary["low_count"] == 4

    def test_handles_insight_with_no_stakeholder_context(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify insight without stakeholder context is returned correctly."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            title="Deal-level Insight",
            stakeholder_id=None,
            stakeholder_name=None,
            stakeholder_email=None,
        )
        fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        data = resp.json()["data"]["insights"][0]
        assert data["stakeholder_id"] is None
        assert data["stakeholder_name"] is None
        assert data["stakeholder_email"] is None

    def test_returns_422_for_invalid_deal_id(
        self,
        client: TestClient,
        insights_dependency_overrides,
    ):
        """Verify 422 error for malformed deal ID."""
        resp = client.get("/users/crm/deals/not-a-uuid/stakeholder-insights")
        assert resp.status_code == 422


class TestAcknowledgeInsight:
    """Tests for POST /{deal_id}/stakeholder-insights/{insight_id}/acknowledge endpoint."""

    def test_successfully_acknowledges_active_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify active insight can be acknowledged."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
            title="Test Insight",
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/acknowledge"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Insight acknowledged successfully"
        assert body["data"]["status"] == "ACKNOWLEDGED"
        assert body["data"]["insight_id"] == str(insight.insight_id)

    def test_returns_404_for_nonexistent_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        test_deal_id,
    ):
        """Verify 404 error when insight doesn't exist."""
        unknown_id = uuid4()

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{unknown_id}/acknowledge"
        )

        assert resp.status_code == 404

    def test_returns_422_for_invalid_insight_id(
        self,
        client: TestClient,
        insights_dependency_overrides,
        test_deal_id,
    ):
        """Verify 422 error for malformed insight ID."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/not-a-uuid/acknowledge"
        )

        assert resp.status_code == 422


class TestDismissInsight:
    """Tests for POST /{deal_id}/stakeholder-insights/{insight_id}/dismiss endpoint."""

    def test_successfully_dismisses_active_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify active insight can be dismissed."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
            title="Dismissable Insight",
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/dismiss"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Insight dismissed successfully"
        assert body["data"]["status"] == "DISMISSED"

    def test_successfully_dismisses_acknowledged_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify acknowledged insight can also be dismissed."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACKNOWLEDGED,
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/dismiss"
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "DISMISSED"

    def test_returns_404_for_nonexistent_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        test_deal_id,
    ):
        """Verify 404 error when insight doesn't exist."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{uuid4()}/dismiss"
        )

        assert resp.status_code == 404


class TestResolveInsight:
    """Tests for POST /{deal_id}/stakeholder-insights/{insight_id}/resolve endpoint."""

    def test_successfully_resolves_insight_without_notes(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify insight can be resolved without providing notes."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/resolve"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Insight resolved successfully"
        assert body["data"]["status"] == "RESOLVED"

    def test_successfully_resolves_insight_with_notes(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify insight can be resolved with resolution notes."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
        )
        fake_insight_service.add_insight(insight)

        resolution_notes = "Contacted stakeholder via phone, scheduled follow-up meeting."

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/resolve",
            json={"resolution_notes": resolution_notes},
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "RESOLVED"

    def test_successfully_resolves_acknowledged_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify acknowledged insight can be resolved."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACKNOWLEDGED,
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/resolve"
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "RESOLVED"

    def test_returns_404_for_nonexistent_insight(
        self,
        client: TestClient,
        insights_dependency_overrides,
        test_deal_id,
    ):
        """Verify 404 error when insight doesn't exist."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{uuid4()}/resolve"
        )

        assert resp.status_code == 404

    def test_returns_422_for_invalid_deal_id(
        self,
        client: TestClient,
        insights_dependency_overrides,
    ):
        """Verify 422 error for malformed deal ID."""
        resp = client.post(f"/users/crm/deals/not-a-uuid/stakeholder-insights/{uuid4()}/resolve")

        assert resp.status_code == 422


class TestInsightStatusTransitions:
    """Tests for insight status transition flows."""

    def test_full_lifecycle_active_to_acknowledged_to_resolved(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify complete lifecycle: ACTIVE -> ACKNOWLEDGED -> RESOLVED."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
        )
        fake_insight_service.add_insight(insight)

        # Step 1: Acknowledge
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/acknowledge"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "ACKNOWLEDGED"

        # Step 2: Resolve
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/resolve",
            json={"resolution_notes": "Issue addressed"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "RESOLVED"

    def test_direct_active_to_resolved(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify ACTIVE can be directly resolved (skip acknowledge)."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/resolve"
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "RESOLVED"

    def test_direct_active_to_dismissed(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify ACTIVE can be directly dismissed."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            status=StakeholderInsightStatus.ACTIVE,
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/dismiss"
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "DISMISSED"


class TestInsightResponseStructure:
    """Tests validating the complete response structure matches API schema."""

    def test_list_response_structure(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify list response has all required top-level fields."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
        )
        fake_insight_service.add_insight(insight)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholder-insights")

        assert resp.status_code == 200
        body = resp.json()

        # Top-level response structure
        assert "status" in body
        assert "message" in body
        assert "data" in body

        # Data structure
        assert "insights" in body["data"]
        assert "total_count" in body["data"]
        assert "summary" in body["data"]

        # Summary structure
        summary = body["data"]["summary"]
        assert "critical_count" in summary
        assert "high_count" in summary
        assert "medium_count" in summary
        assert "low_count" in summary

    def test_single_insight_response_structure(
        self,
        client: TestClient,
        insights_dependency_overrides,
        fake_insight_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify single insight response (from acknowledge/dismiss/resolve) has correct structure."""
        insight = create_insight(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
        )
        fake_insight_service.add_insight(insight)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholder-insights/{insight.insight_id}/acknowledge"
        )

        assert resp.status_code == 200
        body = resp.json()

        # Top-level response structure
        assert body["status"] == "SUCCESS"
        assert "message" in body
        assert "data" in body

        # Insight data structure
        data = body["data"]
        required_fields = [
            "insight_id",
            "insight_type",
            "priority",
            "status",
            "title",
            "ai_reasoning",
            "ai_confidence",
            "identified_at",
            "created_at",
            "updated_at",
        ]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
