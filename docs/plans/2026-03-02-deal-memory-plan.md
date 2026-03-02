# Deal Memory Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a persistent deal memory system — a markdown document per deal that accumulates knowledge from meetings, emails, and notes, serving as pre-built context for all downstream agents.

**Architecture:** New `deal_memories` table stores a markdown TEXT document per deal. A tool-calling `DealMemoryAgent` reuses existing tools (list_deal_meetings, list_deal_stakeholders, etc.) to discover deal data — no custom data loading. A thin `DealMemoryService` orchestrates: load existing memory, call agent, save result. A scheduled Celery task backfills deals without memory; event triggers handle incremental updates.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy, Alembic (org migrations), Celery, Azure OpenAI (via Agno), PostgreSQL TEXT column.

**Design Doc:** `docs/plans/2026-03-02-deal-memory-design.md`

---

## Task 1: Database Model + Migration

**Files:**
- Create: `src/domain/models/organisation_tables/sync/deal_memory_models.py`
- Modify: `src/domain/models/organisation_tables/job_models.py`
- Create: `migrations/org_migrations/versions/3c7f79637767_033_deal_memories.py`

**Step 1: Add `DEAL_MEMORY_BUILD` to `DealJobType` enum**

In `src/domain/models/organisation_tables/job_models.py`, add after `DEAL_IMPORT`:

```python
# Deal memory
DEAL_MEMORY_BUILD = "deal_memory_build"  # Build or update deal memory document
```

**Step 2: Create the `DealMemory` model**

Create `src/domain/models/organisation_tables/sync/deal_memory_models.py`:

```python
"""Deal Memory model — persistent markdown memory document per deal."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import Column, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from src.domain.models.organisation_tables.base_mixins import TimestampMixin


class DealMemory(TimestampMixin, table=True):
    """One markdown document per deal, incrementally updated."""

    __tablename__: ClassVar[str] = "deal_memories"
    __table_args__: ClassVar[tuple] = (
        UniqueConstraint("organisation_id", "deal_id", name="uq_deal_memories_org_deal"),
        Index("ix_deal_memories_org", "organisation_id"),
    )

    memory_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
    )
    content: str = Field(
        sa_column=Column(Text, nullable=False),
    )
    version: int = Field(
        sa_column=Column(Integer, nullable=False, server_default="1"),
        default=1,
    )
    token_count: int | None = Field(default=None)


DealMemory.update_forward_refs()
```

**Step 3: Create the migration**

Create `migrations/org_migrations/versions/3c7f79637767_033_deal_memories.py`:

```python
"""033 add deal_memories table.

Revision ID: 3c7f79637767
Revises: 3c7f79637768
Create Date: 2026-03-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637767"
down_revision: str | None = "3c7f79637768"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_memories",
        sa.Column("memory_id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id", sa.UUID(), nullable=False, index=True),
        sa.Column("deal_id", sa.UUID(), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"], name="fk_deal_memories_deal"),
    )
    op.create_unique_constraint("uq_deal_memories_org_deal", "deal_memories", ["organisation_id", "deal_id"])
    op.create_index("ix_deal_memories_org", "deal_memories", ["organisation_id"])


def downgrade() -> None:
    op.drop_index("ix_deal_memories_org", table_name="deal_memories")
    op.drop_constraint("uq_deal_memories_org_deal", "deal_memories", type_="unique")
    op.drop_table("deal_memories")
```

**Step 4: Run migration**

```bash
uv run python -m scripts.run_org_migrations
```

---

## Task 2: DealMemory Agent

The agent reuses existing tools (same ones DealSummaryAgent uses) to gather deal data. No custom data loading code.

**Files:**
- Create: `src/agents/workflows/deal_memory/__init__.py`
- Create: `src/agents/workflows/deal_memory/agent.py`
- Create: `src/agents/workflows/deal_memory/prompts.py`
- Modify: `src/agents/tools/registry.py` (register DealMemoryAgent tools)
- Modify: `src/infra/config/agent_config.py` (add config entry)

**Step 1: Create `src/agents/workflows/deal_memory/__init__.py`**

```python
"""Deal Memory agent — builds and updates persistent deal memory documents."""

from src.agents.workflows.deal_memory.agent import DealMemoryAgent

__all__ = ["DealMemoryAgent"]
```

**Step 2: Create `src/agents/workflows/deal_memory/prompts.py`**

```python
"""Prompts for the DealMemoryAgent."""

from __future__ import annotations

DEAL_MEMORY_SYSTEM_PROMPT = """\
You are a Deal Intelligence Agent maintaining a persistent memory document
for a B2B enterprise deal. You produce structured markdown that serves as
the single source of truth for this deal.

# Your Task

{mode_instruction}

# Required Workflow

You MUST call these tools to gather data before writing the memory:
1. **list_deal_stakeholders** — get all stakeholders
2. **list_deal_meetings** — get meeting history
3. **list_deal_emails** — get email threads
4. **list_deal_notes** — get CRM notes
5. **get_existing_risks_blockers** — get known risks

For important meetings, call **get_meeting_details** or **get_meeting_transcript**
to get the full content. Use your judgement on how deep to go.

# Template Structure (MANDATORY — never omit a section)

The document MUST contain these sections in order:

1. Executive Snapshot — 5-8 sentences on current state and momentum
2. Commercial State — deal size, stage, close date, stage duration
3. Deal Business Aspect — why this deal exists, business outcome
4. Technical Aspects — technical landscape, requirements, integrations
5. Pain Points — detailed list of client business problems
6. Buying Dynamics — stakeholder map, economic buyer, champion, multi-threading
7. Engagement Trajectory — meeting activity, response behavior, participation
8. Contextual Signals — urgency, decision clarity, champion strength, budget, competition
9. Execution Clarity — next steps, scheduled meetings, timeline
10. Active Risks — structured risk entries with severity, evidence, mitigation
11. Confidence Assessment — overall close confidence with explanation
12. Change Log — bullet list of changes since last update

# Classification Rules

## Economic Buyer
Individual with formal authority to approve budget.
Do NOT assume based solely on title. Requires evidence: states budget authority,
identified as budget owner in CRM, or described as final approver.

## Champion
Internal stakeholder who actively advocates for our solution.
Requires evidence: ownership language, coordinates internal meetings,
shares internal objections. Frequent contact ≠ champion.

## Decision Clarity
- Yes: criteria, approval process, timeline, stakeholders all articulated
- Partial: some articulated
- No: vague or undefined

## Budget Certainty
- Confirmed: explicit confirmation
- Probable: discussed positively, not confirmed
- Unclear: no clear confirmation
- At Risk: hedging or freeze language

## Multi-Threading
- Single: one active stakeholder
- Partial: two stakeholders, limited role diversity
- Multi: three+ stakeholders across functional roles

## Urgency Level
- High: explicit deadlines or mandatory timeline
- Medium: implied priority, flexible timing
- Low: no time-bound urgency

# Rules
- If evidence is ambiguous, default to lower-confidence classification
  and state "Insufficient evidence."
- Never fabricate information. Only include facts from the data.
- For risks, always include severity, evidence, and mitigation suggestion.
"""

INITIAL_BUILD_INSTRUCTION = """\
You are creating the INITIAL deal memory from scratch. Use your tools to
gather ALL available data about this deal. Read meetings, emails, notes,
stakeholders, and risks. Then build a comprehensive memory document.

Fill every section with available evidence. Use "Insufficient evidence"
where data doesn't provide clear signals. Never leave sections blank.

Output the COMPLETE markdown document."""

INCREMENTAL_UPDATE_INSTRUCTION = """\
You are UPDATING an existing deal memory. A new {trigger_type} has arrived.

The existing memory is provided below. Your tasks:
1. Read the new source data using tools (the trigger source ID: {trigger_source_id})
2. Update ONLY sections affected by the new information
3. Preserve all unaffected sections UNCHANGED
4. Add an entry to the Change Log

Output the COMPLETE updated markdown document."""


def get_deal_memory_system_prompt(
    is_initial: bool,
    trigger_type: str | None = None,
    trigger_source_id: str | None = None,
) -> str:
    """Build the system prompt for the DealMemoryAgent."""
    if is_initial:
        mode = INITIAL_BUILD_INSTRUCTION
    else:
        mode = INCREMENTAL_UPDATE_INSTRUCTION.format(
            trigger_type=trigger_type or "update",
            trigger_source_id=trigger_source_id or "unknown",
        )
    return DEAL_MEMORY_SYSTEM_PROMPT.format(mode_instruction=mode)
```

**Step 3: Create `src/agents/workflows/deal_memory/agent.py`**

```python
"""DealMemoryAgent — tool-calling agent that builds deal memory documents.

Reuses existing tools (list_deal_meetings, list_deal_stakeholders, etc.)
to gather data. Same pattern as DealSummaryAgent.
"""

from __future__ import annotations

from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.deal_memory.prompts import get_deal_memory_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class DealMemoryAgent:
    """Agent that builds or updates a deal memory document using tools."""

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID,
        publisher=None,
        org_name: str | None = None,
    ):
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.publisher = publisher

        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=org_name or "Our Organization",
        )

        tools = get_tools_for_agent("DealMemoryAgent", self.context, lambda: None)
        config = agent_config.get_config("deal_memory_agent")

        self.agent = Agent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            description="Deal Memory Agent",
            tools=tools,
            markdown=True,
            debug_mode=config.debug_mode,
        )

    async def generate(
        self,
        *,
        is_initial_build: bool,
        trigger_type: str | None = None,
        trigger_source_id: str | None = None,
        existing_memory: str = "",
    ) -> str:
        """Generate or update a deal memory document.

        Returns the complete markdown string.
        """
        system_prompt = get_deal_memory_system_prompt(
            is_initial=is_initial_build,
            trigger_type=trigger_type,
            trigger_source_id=trigger_source_id,
        )

        if is_initial_build:
            user_prompt = (
                "Build the initial deal memory for this deal. "
                "Use your tools to gather all available data, then produce "
                "the complete markdown document."
            )
        else:
            user_prompt = (
                f"Update this deal memory with the new {trigger_type or 'data'}.\n\n"
                f"## Existing Memory\n\n```markdown\n{existing_memory}\n```\n\n"
                "Use your tools to read the new data, then produce "
                "the complete updated markdown document."
            )

        logger.info(
            "DealMemoryAgent %s for deal %s",
            "initial build" if is_initial_build else "incremental update",
            self.deal_id,
        )

        response = await self.agent.arun(user_prompt, system_prompt=system_prompt)

        content = response.content
        if isinstance(content, str):
            return content
        return str(content)
```

**Step 4: Register tools in `src/agents/tools/registry.py`**

Add to `AGENT_TOOLS`:

```python
"DealMemoryAgent": {
    ToolName.LIST_DEAL_STAKEHOLDERS,
    ToolName.GET_STAKEHOLDER_DETAILS,
    ToolName.LIST_DEAL_MEETINGS,
    ToolName.GET_MEETING_DETAILS,
    ToolName.GET_MEETING_TRANSCRIPT,
    ToolName.GET_MEETING_SUMMARY,
    ToolName.LIST_DEAL_EMAILS,
    ToolName.GET_EMAIL_DETAILS,
    ToolName.LIST_DEAL_NOTES,
    ToolName.GET_EXISTING_RISKS_BLOCKERS,
    ToolName.GET_DEAL_CHANGE_HISTORY,
    ToolName.GET_PIPELINE_DETAILS,
},
```

**Step 5: Add agent config in `src/infra/config/agent_config.py`**

Add after Risk Blocker config:

```python
# Deal Memory Agent - builds/updates persistent deal memory documents
DEAL_MEMORY_MODEL: str = os.getenv("AGENT_DEAL_MEMORY_MODEL", "gpt-5.2")
DEAL_MEMORY_DEBUG: bool = os.getenv("AGENT_DEAL_MEMORY_DEBUG", "false").lower() == "true"
DEAL_MEMORY_TIMEOUT: int = int(os.getenv("AGENT_DEAL_MEMORY_TIMEOUT", "300"))
```

Add to `_CONFIG_MAP`:

```python
"deal_memory_agent": ("DEAL_MEMORY_MODEL", "DEAL_MEMORY_DEBUG", "DEAL_MEMORY_TIMEOUT"),
```

---

## Task 3: DealMemoryService (Thin Orchestrator)

The service is simple: load existing memory, call agent, save result. No data loading — the agent uses tools.

**Files:**
- Create: `src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py`
- Modify: `src/workers/deal_worker/services/deal_insight_services/__init__.py`

**Step 1: Create the service**

Create `src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py`:

```python
"""Thin orchestrator for deal memory build/update.

The service does NOT load deal data — the agent uses tools for that.
Service responsibility: load existing memory, call agent, save result.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync.deal_memory_models import DealMemory
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import get_deal, get_organisation_name

logger = get_logger(__name__)


class DealMemoryError(Exception):
    def __init__(self, message: str, error_code: str = "memory_error", retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class DealMemoryService:
    def __init__(
        self,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        self.organisation_id = organisation_id
        self.publisher = publisher

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def build_or_update(
        self,
        deal_id: UUID,
        *,
        trigger_type: str | None = None,
        trigger_source_id: UUID | None = None,
    ) -> str:
        """Build or update the deal memory document.

        Returns the updated markdown content.
        """
        from src.agents.workflows.deal_memory import DealMemoryAgent

        # Phase 1: Load existing memory + basic deal validation (fresh session)
        self._publish_progress("Loading deal context...", "context", 0.1)

        existing_memory = ""
        existing_version = 0
        is_initial = True
        org_name: str | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, deal_id)
            if not deal:
                raise DealMemoryError(
                    f"Deal {deal_id} not found",
                    error_code="deal_not_found",
                    retryable=False,
                )

            org_name = await get_organisation_name(session, self.organisation_id)

            memory_row = (
                await session.execute(
                    select(DealMemory).where(
                        col(DealMemory.deal_id) == deal_id,
                        col(DealMemory.organisation_id) == self.organisation_id,
                    )
                )
            ).scalars().first()

            if memory_row:
                existing_memory = memory_row.content
                existing_version = memory_row.version
                is_initial = False
        # Session released

        # Phase 2: Call agent (agent uses tools to gather data, no session held here)
        self._publish_progress("AI is building deal memory...", "generation", 0.3)

        agent = DealMemoryAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            publisher=self.publisher,
            org_name=org_name,
        )

        try:
            content = await agent.generate(
                is_initial_build=is_initial,
                trigger_type=trigger_type,
                trigger_source_id=str(trigger_source_id) if trigger_source_id else None,
                existing_memory=existing_memory,
            )
        except Exception as exc:
            raise DealMemoryError(
                f"AI generation failed: {exc}",
                error_code="ai_generation_failed",
                retryable=True,
            ) from exc

        # Phase 3: Save result (fresh session)
        self._publish_progress("Saving deal memory...", "saving", 0.9)

        token_count = len(content.split()) * 2  # rough estimate

        async for session in acquire_org_async_session(self.organisation_id):
            memory_row = (
                await session.execute(
                    select(DealMemory).where(
                        col(DealMemory.deal_id) == deal_id,
                        col(DealMemory.organisation_id) == self.organisation_id,
                    )
                )
            ).scalars().first()

            if memory_row:
                memory_row.content = content
                memory_row.version = memory_row.version + 1
                memory_row.token_count = token_count
            else:
                session.add(
                    DealMemory(
                        organisation_id=self.organisation_id,
                        deal_id=deal_id,
                        content=content,
                        version=1,
                        token_count=token_count,
                    )
                )
            await session.commit()

        logger.info(
            "Deal memory %s for deal %s (v%d, ~%d tokens)",
            "created" if is_initial else "updated",
            deal_id,
            existing_version + 1,
            token_count,
        )
        return content
```

**Step 2: Update `src/workers/deal_worker/services/deal_insight_services/__init__.py`**

Add imports:

```python
from src.workers.deal_worker.services.deal_insight_services.deal_memory_service import (
    DealMemoryError,
    DealMemoryService,
)
```

Add to `__all__`: `"DealMemoryError"`, `"DealMemoryService"`

---

## Task 4: Celery Tasks — Job Processing + Backfill

**Files:**
- Create: `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py`
- Modify: `src/workers/deal_worker/tasks/deal_insight_tasks/__init__.py`
- Modify: `src/workers/deal_worker/config.py`

**Step 1: Create `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py`**

```python
"""Celery tasks for deal memory build and update."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import func, select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import CalendarEventDealAssociation
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.domain.models.organisation_tables.sync.deal_memory_models import DealMemory
from src.infra.database.factory import get_async_provider
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services import (
    DealMemoryError,
    DealMemoryService,
)
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


@app.task(
    bind=True,
    name="deal_memory.process_job",
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def process_deal_memory_job(
    self: Task, organisation_id: str, job_id: str
) -> dict[str, Any]:
    """Process a deal memory build/update job."""
    logger.info("Processing deal memory job org=%s job=%s", organisation_id, job_id)

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Load job context
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            deal_id = job.deal_id
            job_type_str = str(job.job_type)
            metadata = job.job_metadata or {}
            trigger_type = metadata.get("trigger_type")
            trigger_source_id_str = metadata.get("trigger_source_id")
            trigger_source_id = UUID(trigger_source_id_str) if trigger_source_id_str else None

            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        if not deal_id:
            raise RuntimeError(f"Deal job {job_id} has no deal_id")

        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        try:
            service = DealMemoryService(org_uuid, publisher)
            content = await service.build_or_update(
                deal_id,
                trigger_type=trigger_type,
                trigger_source_id=trigger_source_id,
            )
        except DealMemoryError as e:
            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = e.message
                    await session.commit()
            publisher.publish_error(e.message, e.error_code)
            publisher.publish_finalize("error")
            if e.retryable:
                raise
            return {"status": "failed", "job_id": job_id, "error": e.message}

        # Mark completed
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = {"token_count": len(content.split()) * 2}
                await session.commit()

        publisher.publish_finalize("done")
        return {"status": "completed", "job_id": job_id}

    try:
        return run_async(_run())
    except DealMemoryError as e:
        if not e.retryable:
            return {"status": "failed", "job_id": job_id, "error": e.message}
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
    except Exception as e:
        logger.error("Deal memory task failed %s: %s", job_id, e, exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e


@app.task(
    bind=True,
    name="deal_memory.backfill_org",
    acks_late=True,
    max_retries=2,
    soft_time_limit=1800,
    time_limit=1860,
)
def build_deal_memory_org(self: Task, organisation_id: str) -> dict[str, Any]:
    """Backfill: find open deals without memory that have data, dispatch builds."""
    lock_key = f"task_lock:deal_memory_backfill:{organisation_id}"
    if not acquire_task_lock(lock_key, ttl=1800):
        return {"status": "skipped", "reason": "already_running"}

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        dispatched = 0
        skipped = 0

        async for session in acquire_org_async_session(org_uuid):
            # Open deals without a memory
            existing_ids = select(col(DealMemory.deal_id)).where(
                col(DealMemory.organisation_id) == org_uuid
            )
            deals_result = await session.execute(
                select(col(CRMDeal.deal_id))
                .where(
                    col(CRMDeal.organisation_id) == org_uuid,
                    col(CRMDeal.is_deleted).is_(False),
                    col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                    col(CRMDeal.deal_id).notin_(existing_ids),
                )
                .limit(50)
            )
            deal_ids = [row[0] for row in deals_result.all()]

        for deal_id in deal_ids:
            # Check if deal has any processed meetings
            has_data = False
            async for session in acquire_org_async_session(org_uuid):
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(CalendarEventDealAssociation)
                        .where(col(CalendarEventDealAssociation.deal_id) == deal_id)
                    )
                ).scalar() or 0
                has_data = count > 0

            if not has_data:
                skipped += 1
                continue

            # Create job and dispatch
            async for session in acquire_org_async_session(org_uuid):
                job = DealJobs(
                    organisation_id=org_uuid,
                    user_id=AUTOMATED_PIPELINE_USER_ID,
                    job_type=DealJobType.DEAL_MEMORY_BUILD,
                    deal_id=deal_id,
                    status=JobStatus.PENDING,
                    job_metadata={"source": "automated_pipeline", "is_initial_build": True},
                )
                session.add(job)
                await session.commit()
                job_id = job.job_id

            process_deal_memory_job.delay(
                organisation_id=organisation_id,
                job_id=str(job_id),
            )
            dispatched += 1

        return {"status": "completed", "dispatched": dispatched, "skipped": skipped}

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error("Backfill failed for org %s: %s", organisation_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=120) from exc
    finally:
        release_task_lock(lock_key)


@app.task(
    name="deal_memory.trigger_backfill_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def trigger_deal_memory_backfill_all_orgs() -> dict[str, Any]:
    """Tier 1: Dispatch backfill for all approved organisations."""

    async def _dispatch() -> dict[str, Any]:
        provider = get_async_provider()
        async with provider.master_session() as master_session:
            result = await master_session.execute(
                select(Organisation).where(
                    col(Organisation.status) == OrganisationStatus.APPROVED
                )
            )
            orgs = result.scalars().all()

        dispatched = 0
        for org in orgs:
            try:
                build_deal_memory_org.delay(organisation_id=str(org.organisation_id))
                dispatched += 1
            except Exception as e:
                logger.warning("Failed to dispatch for org %s: %s", org.organisation_id, e)

        return {"status": "completed", "orgs_dispatched": dispatched}

    return run_async(_dispatch())
```

**Step 2: Update `src/workers/deal_worker/tasks/deal_insight_tasks/__init__.py`**

Add imports and `__all__` entries for `process_deal_memory_job`, `build_deal_memory_org`, `trigger_deal_memory_backfill_all_orgs`.

**Step 3: Update `src/workers/deal_worker/config.py`**

Add `"deal_memory.process_job"` to `deal_jobs_queue_tasks`.

Add `"deal_memory.backfill_org"` and `"deal_memory.trigger_backfill_all_orgs"` to `deal_queue_tasks`.

Add to `beat_schedule`:

```python
"deal-memory-backfill": {
    "task": "deal_memory.trigger_backfill_all_orgs",
    "schedule": crontab(minute=10),
    "options": {"queue": "deal", "routing_key": "deal"},
},
```

---

## Task 5: Wire Event Triggers + Lint/Verify

**Step 1:** After meeting summary completes successfully, dispatch a deal memory update job. Same pattern as existing trigger points — create a `DealJobs` row with `trigger_type="meeting"` and `trigger_source_id`, then call `process_deal_memory_job.delay(...)`.

Exact insertion point depends on the meeting summary flow — the implementer should find where `JobStatus.COMPLETED` is set in the meeting summary path and add the trigger there.

**Step 2: Run lint and type check**

```bash
uv run ruff check src/domain/models/organisation_tables/sync/deal_memory_models.py src/agents/workflows/deal_memory/ src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py --fix
uv run mypy src/domain/models/organisation_tables/sync/deal_memory_models.py src/agents/workflows/deal_memory/ src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py
```

---

## Summary

| Action | File |
|--------|------|
| Create | `src/domain/models/organisation_tables/sync/deal_memory_models.py` |
| Modify | `src/domain/models/organisation_tables/job_models.py` |
| Create | `migrations/org_migrations/versions/3c7f79637767_033_deal_memories.py` |
| Create | `src/agents/workflows/deal_memory/__init__.py` |
| Create | `src/agents/workflows/deal_memory/agent.py` |
| Create | `src/agents/workflows/deal_memory/prompts.py` |
| Modify | `src/agents/tools/registry.py` |
| Modify | `src/infra/config/agent_config.py` |
| Create | `src/workers/deal_worker/services/deal_insight_services/deal_memory_service.py` |
| Modify | `src/workers/deal_worker/services/deal_insight_services/__init__.py` |
| Create | `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py` |
| Modify | `src/workers/deal_worker/tasks/deal_insight_tasks/__init__.py` |
| Modify | `src/workers/deal_worker/config.py` |
