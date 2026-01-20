"""Import and instantiation tests for deal insight modules.

These tests verify that:
1. All modules can be imported without errors
2. Services and agents accept the new signatures (no session parameter)

Run with: uv run pytest tests/workers/deal_worker/test_deal_insight_imports.py -v

Or run standalone verification:
    uv run python tests/workers/deal_worker/test_deal_insight_imports.py
"""

from uuid import uuid4

import pytest

# Skip markers for tests that need specific fixtures
pytestmark = pytest.mark.unit


class TestDealInsightTaskImports:
    """Test that all deal insight tasks can be imported."""

    def test_import_deal_insight_tasks_init(self):
        """Test importing from deal_insight_tasks __init__."""
        from src.workers.deal_worker.tasks.deal_insight_tasks import (
            analyze_deal_stakeholders_task,
            analyze_org_stakeholders_task,
            extract_deal_risks_blockers_task,
            process_deal_summary_job,
            trigger_extraction_all_orgs,
            trigger_stakeholder_analysis_all_orgs,
        )

        # Verify they are callable (Celery tasks)
        assert callable(analyze_deal_stakeholders_task)
        assert callable(analyze_org_stakeholders_task)
        assert callable(extract_deal_risks_blockers_task)
        assert callable(process_deal_summary_job)
        assert callable(trigger_extraction_all_orgs)
        assert callable(trigger_stakeholder_analysis_all_orgs)

    def test_import_from_main_tasks_init(self):
        """Test importing deal insight tasks from main tasks __init__."""
        from src.workers.deal_worker.tasks import (
            analyze_deal_stakeholders_task,
            extract_deal_risks_blockers_task,
            process_deal_summary_job,
        )

        assert callable(analyze_deal_stakeholders_task)
        assert callable(extract_deal_risks_blockers_task)
        assert callable(process_deal_summary_job)


class TestDealInsightServiceImports:
    """Test that all deal insight services can be imported."""

    def test_import_deal_insight_services_init(self):
        """Test importing from deal_insight_services __init__."""
        from src.workers.deal_worker.services.deal_insight_services import (
            DealNotFoundError,
            DealSummaryError,
            DealSummaryService,
            NoContentError,
            NoMeetingsError,
            RiskBlockerExtractionError,
            RiskBlockerExtractionService,
            StakeholderAnalysisService,
        )

        # Verify they are classes
        assert isinstance(DealSummaryService, type)
        assert isinstance(StakeholderAnalysisService, type)
        assert isinstance(RiskBlockerExtractionService, type)

        # Verify exceptions
        assert issubclass(DealNotFoundError, Exception)
        assert issubclass(DealSummaryError, Exception)
        assert issubclass(NoMeetingsError, Exception)
        assert issubclass(NoContentError, Exception)
        assert issubclass(RiskBlockerExtractionError, Exception)


class TestServiceInstantiation:
    """Test that services can be instantiated with new signatures."""

    def test_stakeholder_analysis_service_instantiation(self):
        """StakeholderAnalysisService takes only organisation_id (no session)."""
        from src.workers.deal_worker.services.deal_insight_services import (
            StakeholderAnalysisService,
        )

        org_id = uuid4()

        # Should instantiate with just organisation_id
        service = StakeholderAnalysisService(organisation_id=org_id)

        assert service.organisation_id == org_id

    def test_risk_blocker_extraction_service_instantiation(self):
        """RiskBlockerExtractionService takes organisation_id and optional publisher."""
        from src.workers.deal_worker.services.deal_insight_services import (
            RiskBlockerExtractionService,
        )

        org_id = uuid4()

        # Should instantiate with just organisation_id
        service = RiskBlockerExtractionService(organisation_id=org_id)

        assert service.organisation_id == org_id
        assert service.publisher is None

    def test_deal_summary_service_instantiation(self):
        """DealSummaryService takes organisation_id and optional publisher."""
        from src.workers.deal_worker.services.deal_insight_services import (
            DealSummaryService,
        )

        org_id = uuid4()

        # Should instantiate with just organisation_id
        service = DealSummaryService(organisation_id=org_id)

        assert service.organisation_id == org_id


class TestAgentInstantiation:
    """Test that agents can be instantiated with new signatures."""

    def test_risk_blocker_extraction_agent_instantiation(self):
        """RiskBlockerExtractionAgent takes organisation_id and deal_id (no session)."""
        from src.agents.workflows.risk_blocker_extraction import (
            RiskBlockerExtractionAgent,
        )

        org_id = uuid4()
        deal_id = uuid4()

        # Should instantiate with organisation_id and deal_id (no session)
        agent = RiskBlockerExtractionAgent(
            organisation_id=org_id,
            deal_id=deal_id,
        )

        assert agent.organisation_id == org_id
        assert agent.deal_id == deal_id
        assert agent.context.organisation_id == org_id
        assert agent.context.deal_id == deal_id

    def test_stakeholder_analysis_agent_instantiation(self):
        """StakeholderAnalysisAgent has no special context (simple agent)."""
        from src.agents.workflows.stakeholder_analysis import StakeholderAnalysisAgent

        # Simple agent with no parameters
        agent = StakeholderAnalysisAgent()

        assert agent.agent is not None

    def test_deal_summary_agent_instantiation(self):
        """DealSummaryAgent takes organisation_id and deal_id (no session)."""
        from src.agents.workflows.deal_summary import DealSummaryAgent

        org_id = uuid4()
        deal_id = uuid4()

        # Should instantiate with organisation_id and deal_id (no session)
        agent = DealSummaryAgent(
            organisation_id=org_id,
            deal_id=deal_id,
        )

        assert agent.organisation_id == org_id
        assert agent.deal_id == deal_id
        assert agent.context.organisation_id == org_id
        assert agent.context.deal_id == deal_id

    def test_briefing_agent_instantiation(self):
        """BriefingAgent takes organisation_id and deal_id (no session)."""
        from src.agents.workflows.briefing import BriefingAgent

        org_id = uuid4()
        deal_id = uuid4()

        # Should instantiate with organisation_id and deal_id (no session)
        agent = BriefingAgent(
            organisation_id=org_id,
            deal_id=deal_id,
        )

        assert agent.organisation_id == org_id
        assert agent.deal_id == deal_id
        assert agent.context.organisation_id == org_id
        assert agent.context.deal_id == deal_id

    def test_summary_agent_instantiation(self):
        """SummaryAgent (meeting summary) takes organisation_id and deal_id (no session)."""
        from src.agents.workflows.summary import SummaryAgent

        org_id = uuid4()
        deal_id = uuid4()

        # Should instantiate with organisation_id and deal_id (no session)
        agent = SummaryAgent(
            organisation_id=org_id,
            deal_id=deal_id,
        )

        assert agent.organisation_id == org_id
        assert agent.deal_id == deal_id
        assert agent.context.organisation_id == org_id
        assert agent.context.deal_id == deal_id
