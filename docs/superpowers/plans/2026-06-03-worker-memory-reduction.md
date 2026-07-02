# Worker Memory Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce baseline and task-growth memory for Celery workers without weakening task registration or org-isolation behavior.

**Architecture:** Treat memory as two separate problems: worker startup imports and per-task retained data. First add repeatable measurement, then remove eager imports and broad package re-exports, then stream large Salesforce/CRM batches instead of retaining whole sync payloads, and finally add recycling limits as a safety net.

**Tech Stack:** Python 3.11, Celery prefork workers, FastAPI backend modules, Kubernetes prod evidence from `copilot-gtm-aks-prod`.

---

## Evidence Snapshot

Live prod context: `copilot-gtm-aks-prod`, namespace `copilotgtm`.

Current pod memory from `kubectl top pod -n copilotgtm --containers`:

- `copilot-backend-integrations-worker`: about `2914Mi`, concurrency `8`, memory limit `6Gi`
- `copilot-backend-knowledge-upload-worker`: about `1000Mi`, concurrency `5`, memory limit `1536Mi`
- `copilot-backend-deal-worker`: about `734Mi`, concurrency `2`, memory limit `3Gi`
- `copilot-backend-chat-worker`: about `722Mi`, concurrency `3`, memory limit `2560Mi`

Per-process `/proc/*/smaps_rollup` showed integrations private memory concentrated in children, not only shared RSS:

- integrations children reached private memory around `691Mi`, `630Mi`, `470Mi`, `420Mi`, and `356Mi`
- knowledge children were around `83Mi` to `171Mi` private
- chat children were around `114Mi` to `210Mi` private
- deal children were around `197Mi` to `232Mi` private

Local import probe after `uv sync --all-extras`:

- `src.workers.deal_worker.tasks`: `+226MiB`
- `src.workers.knowledge_worker.tasks`: `+162MiB`
- `src.workers.chat_worker.tasks`: `+138MiB`
- `src.workers.integrations_worker.tasks`: `+116MiB`
- `src.workers.integrations_worker.tasks.crm.salesforce.salesforce_cdc_subscriber_task`: `+117MiB`

Runtime logs show integrations memory growth is also data-volume related:

- `integrations.sync_salesforce_companies` was paging Salesforce accounts beyond `records_so_far=47000`
- `SalesforceAccountsFetcher.fetch_all()` accumulates all pages in one `records` list before returning
- calendar batch failures log a bounded event sample, but `exc_info=True` still emits SQLAlchemy statement parameters, producing huge logs and memory pressure

---

## File Map

- Modify: `src/workers/base/celery_base.py` to accept explicit task module lists and keep worker registration measurable.
- Modify: `src/workers/*/celery_app.py` to remove duplicate explicit imports where Celery include already handles registration.
- Modify: `src/workers/*/tasks/__init__.py` package surfaces to stop broad re-export imports where they are not needed for Celery registration.
- Modify: `src/workers/chat_worker/services/__init__.py` and task imports that currently pull unrelated services through package-level imports.
- Modify: `src/workers/integrations_worker/providers/crm/salesforce/*_fetcher.py` to add page/batch streaming APIs.
- Modify: `src/service/integrations/crm/records/salesforce/*/*_sync_service.py` to persist streamed pages/chunks.
- Modify: `src/service/integrations/calendar/writer.py` or DB engine creation to prevent SQLAlchemy parameter dumps on calendar bulk-write failures.
- Create: `scripts/test_local/worker_import_memory_probe.py` for repeatable import-memory measurement.
- Test: focused worker import/registration tests under `tests/workers/`.

---

### Task 1: Add Repeatable Memory Measurement

**Files:**
- Create: `scripts/test_local/worker_import_memory_probe.py`
- Test manually with: `uv run python -m scripts.test_local.worker_import_memory_probe`

- [ ] **Step 1: Add the probe script**

Create a script that launches a fresh subprocess per module so imported state cannot contaminate later measurements.

```python
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Sequence

MODULES = (
    "src.workers.chat_worker.tasks",
    "src.workers.deal_worker.tasks",
    "src.workers.knowledge_worker.tasks",
    "src.workers.integrations_worker.tasks",
    "src.workers.integrations_worker.tasks.crm.salesforce.salesforce_cdc_subscriber_task",
)


def _measure(module: str) -> str:
    code = (
        "import importlib, os, psutil; "
        "p=psutil.Process(os.getpid()); "
        "before=p.memory_info().rss//1024//1024; "
        f"importlib.import_module({module!r}); "
        "after=p.memory_info().rss//1024//1024; "
        f"print({module!r}, after-before, after)"
    )
    return subprocess.check_output([sys.executable, "-c", code], text=True)


def main(argv: Sequence[str] | None = None) -> int:
    modules = tuple(argv or MODULES)
    for module in modules:
        print(_measure(module).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 2: Run the baseline**

Run:

```bash
uv run python -m scripts.test_local.worker_import_memory_probe
```

Expected baseline should be close to the evidence above. Record the output in the PR description after each optimization pass.

---

### Task 2: Remove Eager Registration Cascades

**Files:**
- Modify: `src/workers/base/celery_base.py`
- Modify: `src/workers/chat_worker/celery_app.py`
- Modify: `src/workers/knowledge_worker/celery_app.py`
- Modify: `src/workers/integrations_worker/celery_app.py`
- Modify: `src/workers/*/tasks/__init__.py`

- [ ] **Step 1: Change the Celery factory to accept a string or explicit module list**

Keep current behavior compatible, but allow workers to include specific task modules without importing a broad package `__init__`.

```python
def create_celery_app(worker_name, config, tasks_module):
    include_modules = [tasks_module] if isinstance(tasks_module, str) else list(tasks_module)
    celery_app = Celery(
        worker_name,
        broker=config.broker_url,
        backend=config.backend_url,
        include=include_modules,
    )
```

- [ ] **Step 2: Define explicit task module lists per worker**

For each worker, pass only modules that actually define `@app.task` functions. Example shape for integrations:

```python
task_modules = [
    "src.workers.integrations_worker.tasks.calendar.integration_task",
    "src.workers.integrations_worker.tasks.email.integration_task",
    "src.workers.integrations_worker.tasks.token_refresh",
]
app = create_celery_app("integrations_worker", config, task_modules)
```

Then add the CRM, messaging, and subscription modules explicitly. Do the same for chat, knowledge, and deal.

- [ ] **Step 3: Remove bottom explicit imports from worker `celery_app.py` files**

Remove imports like:

```python
from .tasks import calendar, crm, email, messaging, token_refresh
```

Registration should happen through Celery `include`, not through both `include` and manual imports.

- [ ] **Step 4: Make `tasks/__init__.py` lightweight**

Replace package-level re-export imports with only `__all__` names or leave the files empty with a module docstring. Do not import every task module from package `__init__`.

- [ ] **Step 5: Verify registration still works**

Run:

```bash
uv run python -m scripts.test_local.worker_import_memory_probe
uv run python -c "from src.workers.integrations_worker.celery_app import app; print(sorted(app.tasks.keys()))"
```

Expected: task names still include the queues' existing task names, and import deltas drop where package `__init__` cascades were removed.

---

### Task 3: Move Heavy Dependencies Behind Task Execution

**Files:**
- Modify: `src/workers/chat_worker/tasks/*.py`
- Modify: `src/workers/chat_worker/services/__init__.py`
- Modify: `src/workers/knowledge_worker/tasks/*.py`
- Modify: `src/workers/deal_worker/tasks/**/*.py`
- Modify: selected `src/agents/workflows/**/agent.py` imports only if task modules import them eagerly

- [ ] **Step 1: Replace service package imports with direct module imports**

Avoid imports like:

```python
from src.workers.chat_worker.services import BotMessageProcessingService
```

Use:

```python
from src.workers.chat_worker.services.bot_message_processing_service import (
    BotMessageProcessingService,
)
```

This prevents `services/__init__.py` from loading unrelated services such as post-upload thumbnail generation.

- [ ] **Step 2: Move task-only service imports into task functions**

For task modules that register lightweight wrappers but import heavy agents/services at module import time, move the heavy import into the task body.

```python
@app.task(name="deal.process_summary", bind=True)
def process_deal_summary_job(self, organisation_id: str, deal_id: str) -> dict[str, Any]:
    from src.workers.deal_worker.services.deal_insight_services.deal_summary_service import (
        DealSummaryService,
    )

    return run_async(DealSummaryService().run(...))
```

- [ ] **Step 3: Preserve type checking without runtime imports**

Use `TYPE_CHECKING` for annotations that would otherwise import heavy modules.

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.workflows.deal_summary.agent import DealSummaryAgent
```

- [ ] **Step 4: Re-run the import probe**

Run:

```bash
uv run python -m scripts.test_local.worker_import_memory_probe
```

Expected: biggest reductions should appear in deal and chat startup imports.

---

### Task 4: Stream Salesforce Record Sync Instead of Retaining Whole Objects

**Files:**
- Modify: `src/workers/integrations_worker/providers/crm/salesforce/accounts_fetcher.py`
- Modify: `src/workers/integrations_worker/providers/crm/salesforce/contacts_fetcher.py`
- Modify: `src/workers/integrations_worker/providers/crm/salesforce/opportunities_fetcher.py`
- Modify: `src/workers/integrations_worker/providers/crm/salesforce/tasks_fetcher.py`
- Modify: `src/workers/integrations_worker/providers/crm/salesforce/users_fetcher.py`
- Modify: matching sync services under `src/service/integrations/crm/records/salesforce/`

- [ ] **Step 1: Add async page iterators to fetchers**

Keep existing `fetch_all()` for tests/backward compatibility initially, but add streaming APIs:

```python
async def iter_pages(self) -> AsyncIterator[list[dict[str, Any]]]:
    field_names = await self._fetch_field_names()
    soql = f"SELECT {', '.join(field_names)} FROM Account ORDER BY Name"
    url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
    next_url: str | None = None
    page_count = 0

    while page_count < _MAX_PAGES:
        payload = await self._get_json(next_url or url, params=None if next_url else {"q": soql})
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Salesforce account query API returned non-list records")
        yield [row for row in raw_records if isinstance(row, dict)]
        page_count += 1
        next_records_url = payload.get("nextRecordsUrl")
        if not isinstance(next_records_url, str) or not next_records_url:
            break
        next_url = next_records_url if next_records_url.startswith("http") else f"{self.instance_url}{next_records_url}"
```

- [ ] **Step 2: Persist per page or bounded chunk in sync services**

Replace:

```python
records = await fetcher.fetch_all()
normalized = normalize_companies(records)
await writer.apply(normalized)
```

With:

```python
total_seen = 0
for page in fetcher.iter_pages():
    normalized = normalize_companies(page)
    await writer.apply(normalized)
    total_seen += len(page)
```

Use `async for`, not `for`, in the actual implementation.

- [ ] **Step 3: Add tests for streaming behavior**

Create tests that simulate three Salesforce pages and assert the writer is called per page rather than after all pages are retained.

Run:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce -q
```

Expected: existing result counts remain correct, writer calls are chunked.

---

### Task 5: Stop Huge SQL Parameter Dumps on Calendar Bulk Failures

**Files:**
- Modify: `src/service/integrations/calendar/writer.py`
- Consider: DB engine creation if SQLAlchemy `hide_parameters=True` can be applied safely globally

- [ ] **Step 1: Prevent giant parameter rendering**

The current handler logs a bounded sample, but `exc_info=True` still lets SQLAlchemy render a huge statement and parameter list. Change the failure path to log a compact exception summary and bounded samples.

```python
except Exception as exc:
    logger.error(
        "Calendar batch event upsert failed",
        extra={
            "integration_id": str(integration.integration_id),
            "sample_count": min(len(latest_by_identity), SAMPLE_LOG_LIMIT),
            "samples": [
                item.event.debug_sample
                for item in list(latest_by_identity.values())[:SAMPLE_LOG_LIMIT]
            ],
            "error_type": type(exc).__name__,
            "error_message": str(exc).splitlines()[0][:500],
        },
    )
    raise
```

- [ ] **Step 2: Test bounded logging**

Add a test that forces `session.execute()` to raise with a large fake SQLAlchemy-style message and asserts the log payload is bounded.

Run:

```bash
uv run pytest tests/service/integrations/calendar -q
```

Expected: failure still re-raises, log output does not include all event payloads or SQL parameters.

---

### Task 6: Add Worker Recycling Guardrails After Root Fixes

**Files:**
- Modify: `src/workers/integrations_worker/celery_app.py`
- Modify: `src/workers/chat_worker/celery_app.py`
- Optionally modify: worker Helm values in infra repo if runtime flags are managed outside backend

- [ ] **Step 1: Add max tasks per child where absent**

Deal and knowledge already set `worker_max_tasks_per_child=50`; add equivalent values to integrations and chat.

```python
app.conf.update(
    worker_max_tasks_per_child=25,
)
```

- [ ] **Step 2: Consider max memory per child for integrations only**

After streaming fixes, add a conservative limit if normal tasks stay below it.

```python
app.conf.update(
    worker_max_memory_per_child=900 * 1024,
)
```

Do not set this until the streaming tests prove large Salesforce syncs no longer need multi-GiB memory.

- [ ] **Step 3: Verify worker config imports**

Run:

```bash
uv run python -c "from src.workers.integrations_worker.celery_app import app; print(app.conf.worker_max_tasks_per_child)"
uv run python -c "from src.workers.chat_worker.celery_app import app; print(app.conf.worker_max_tasks_per_child)"
```

Expected: both print configured integer values.

---

### Task 7: Final Verification

**Files:**
- No new files unless tests reveal gaps.

- [ ] **Step 1: Run focused worker tests**

Run:

```bash
uv run pytest tests/workers -q
```

- [ ] **Step 2: Run focused integration CRM/calendar tests**

Run:

```bash
uv run pytest tests/service/integrations tests/workers/integrations_worker -q
```

- [ ] **Step 3: Run linter only at PR boundary**

Run:

```bash
uv run ruff check src/ tests/ scripts/test_local/worker_import_memory_probe.py
```

- [ ] **Step 4: Deploy-readiness check**

Before merging, capture and compare:

```bash
uv run python -m scripts.test_local.worker_import_memory_probe
kubectl top pod -n copilotgtm --containers
```

Expected: local startup deltas are lower, and prod memory after deployment should show lower warm-start memory plus integrations children no longer climbing into multi-GiB during large Salesforce page fetches.
