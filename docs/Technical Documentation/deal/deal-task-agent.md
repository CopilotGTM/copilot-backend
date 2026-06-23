# Deal Task Agent

## Purpose

`DealTaskAgent` runs after meeting-driven deal insight processing. It creates
internal CRM tasks for concrete follow-up items found in meeting summaries and
completes existing internal tasks when the summary explicitly says the work is
done.

This workflow is deal-scoped and organisation-scoped. The agent receives
`organisation_id` and `deal_id` through `AgentContext`; tools never accept a
model-controlled organisation or deal identifier.

## Pipeline Placement

The deal insight pipeline now runs in this order:

```text
deal_memory.process_job
  -> group(
       risk_blocker.extract_deal_risks_blockers_task,
       stakeholder.analyze_deal_stakeholders_task
     )
  -> deal_tasks.sync_from_summary
  -> deal_update.process_job  # only when ENABLE_DEAL_UPDATE_AGENT_PIPELINE=true
```

Task sync runs after the insight group so it can use updated deal memory,
risk/blocker, and stakeholder context. Deal update is a feature-flagged final
step when enabled.

## Components

### Automation Service

`src/service/crm/internal/deal_task_automation_service.py`

This service is the deterministic write boundary. It owns:

- listing `CRMTask` rows for one org/deal,
- creating internal `AI_MEETING` tasks,
- resolving assignee hints against org users,
- creating tasks even when no assignee is known,
- idempotent duplicate detection,
- completing internal same-deal tasks,
- refusing CRM-synced task completion.

### Agent Tools

`src/agents/tools/tasks/`

- `list_deal_tasks`: returns current deal tasks and marks CRM-synced tasks as
  read-only.
- `create_deal_task`: creates an internal task for the current context deal.
- `complete_deal_task`: completes an internal same-deal task after UUID and
  reason validation.

The chat `create_task` tool remains separate. It uses Slack/Teams preview
confirmation and is not exposed to `DealTaskAgent`.

### Agent Workflow

`src/agents/workflows/deal_tasks/`

The prompt requires `list_deal_tasks` before writes. The output schema reports
created, completed, skipped, and failed task items with counts.

### Worker Task

`src/workers/deal_worker/tasks/deal_insight_tasks/deal_task_sync_task.py`

The Celery task name is `deal_tasks.sync_from_summary`. It runs on `deal_jobs`,
marks the pre-created `DealJobs` record in progress, calls `DealTaskAgent`, saves
the structured result, persists token usage when available, and marks the job
complete or failed.

## Idempotency

Created tasks store an `idempotency_key` in `CRMTask.properties`. The key is
built from:

- organisation id,
- deal id,
- source association id, source event id, or `"deal"`,
- normalized task subject.

When the same summary/source is reprocessed, the service returns the existing
task instead of creating a duplicate.

## Completion Rules

Automation may complete a task only when all conditions are true:

- task belongs to the current organisation,
- task belongs to the current deal,
- task is task-like (`TASK` or `TODO`),
- task is not soft-deleted,
- `integration_id is None`.

CRM-synced tasks are read-only because the external CRM remains source of truth.

## Provenance

AI-created tasks include:

- `source = AI_MEETING`,
- `created_by_user_id = None`,
- `properties.created_via = "deal_task_agent"`,
- source event, association, and summary job ids when available,
- assignee resolution status.

## Notification Deferral

Teams, Slack, and WhatsApp notifications are intentionally not implemented in
this slice. The current implementation records task write results and job result
data so a later notifier can decide whether to notify all owners, all assignees,
or a channel-specific recipient group.

## Focused Verification

```bash
uv run pytest tests/service/crm/test_deal_task_automation_service.py tests/agents/tools/tasks/test_deal_task_tools.py tests/agents/tools/test_registry_deal_task_agent.py tests/agents/workflows/deal_tasks/test_deal_task_agent.py tests/workers/deal_worker/test_deal_task_sync_task.py tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py tests/workers/deal_worker/test_deal_task_agent_pipeline_integration.py -v
uv run ruff check src/agents/tools/tasks src/agents/workflows/deal_tasks src/service/crm/internal/deal_task_automation_service.py src/workers/deal_worker/tasks/deal_insight_tasks src/workers/deal_worker/utils/pipeline_dispatch.py tests/service/crm tests/agents tests/workers/deal_worker
git diff --check
```
