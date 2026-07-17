# WhatsApp Response Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove duplicated WhatsApp answers and reduce avoidable cold-path, queue, typing-indicator, and telemetry latency without changing GeneralAgent prompt composition.

**Architecture:** Keep the durable WhatsApp job boundary intact. Fix replay handling at the shared stream buffer, record bounded phase metrics around the existing intake/worker/delivery phases, preload process-local dependencies before workers accept messages, and dedicate worker deployments to interactive versus maintenance queues. Reduce OTLP metric frequency through the existing configuration seam.

**Tech Stack:** Python, Agno streaming, Celery, OpenTelemetry, FastAPI, Helm, pytest.

---

### Task 1: Suppress completed-run answer replay

**Files:**
- Modify: `src/agents/common/stream_buffer.py`
- Test: `tests/agents/test_stream_buffer_run_completed.py`

- [ ] Add a failing stream test where `RunCompleted.content` repeats the answer.
- [ ] Run the focused test and confirm the repeated answer is emitted.
- [ ] Exclude `RunCompleted` from answer-content processing while still yielding it for token metrics.
- [ ] Run the focused stream and GeneralAgent tests.

### Task 2: Measure WhatsApp latency phases and preload cold dependencies

**Files:**
- Modify: `src/infra/observability/metrics.py`
- Modify: `src/workers/chat_worker/services/bot_message_processing_service.py`
- Modify: `src/workers/chat_worker/celery_app.py`
- Test: `tests/infra/observability/test_metrics_runtime.py`
- Test: `tests/workers/chat_worker/test_celery_app.py`

- [ ] Add failing tests for bounded phase histograms and worker child warmup.
- [ ] Record intake, preparation, agent, persistence, delivery, and total durations without tenant identifiers.
- [ ] Import the agent graph and preload WhatsApp credentials in each worker child before it accepts tasks.
- [ ] Run metrics, worker startup, and bot-processing tests.

### Task 3: Isolate interactive chat messages from maintenance work

**Files:**
- Modify: `charts/application/Chart.yaml`
- Modify: `charts/application/values.yaml`
- Modify: `charts/application/values-dev.yaml`
- Modify: `charts/application/values-prod.yaml`
- Modify: `tests/charts/test_worker_cronjobs.py`

- [ ] Add failing Helm contract assertions for a dedicated `chat-message-worker` consuming only interactive queues.
- [ ] Keep `chat-worker` on attachment and maintenance queues and configure independent KEDA scaling.
- [ ] Increase `maxTasksPerChild` to reduce repeated process cold starts.
- [ ] Render the chart and run chart contract tests.

### Task 4: Surface typing earlier and warm API credentials

**Files:**
- Modify: `src/service/bots/whatsapp_bot_service.py`
- Modify: `src/api/fastapi_app.py`
- Test: `tests/service/bots/test_whatsapp_bot_service.py`
- Test: `tests/api/test_fastapi_lifespan.py`

- [ ] Add a failing ordering test requiring the typing request after durable commit but before queue publication.
- [ ] Add a failing lifespan test requiring best-effort WhatsApp credential preload.
- [ ] Move the UX-only typing call to the post-commit/pre-publish boundary and preload API credentials at startup.
- [ ] Run webhook and lifespan tests.

### Task 5: Reduce OTLP metric-export pressure

**Files:**
- Modify: `src/infra/config/configs.py`
- Modify: `charts/application/values-dev.yaml`
- Modify: `charts/application/values-prod.yaml`
- Test: `tests/infra/test_otel_runtime.py`

- [ ] Add a failing configuration contract for a five-minute metric export cadence.
- [ ] Set the existing configurable interval to 300000 milliseconds in deployed environments.
- [ ] Run OTel configuration and runtime tests.

### Task 6: Verification and completion audit

- [ ] Run all focused tests changed above.
- [ ] Run Ruff check and format checks on changed Python files.
- [ ] Run Helm dependency build, template rendering, and chart tests.
- [ ] Run the Python comment-smell scanner on changed backend files.
- [ ] Confirm `git diff --check` and review the diff without staging unrelated files.
