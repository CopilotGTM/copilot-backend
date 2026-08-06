# Trigger Correctness Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the nine verified CRM, schedule, direct-activation, schema-drift, and webhook correctness findings without changing unrelated worktree content.

**Architecture:** Keep the immutable CRM mutation ledger broader than the automation catalog, but let the CRM adapter emit only fields that the typed registry can match. Preserve queued CRM facts by replay-validating them against the stable base registry while validating each saved binding against the current tenant schema. Direct sources remain fail-closed through trigger/event association and authority checks; webhook fallback identity includes the signed timestamp so it cannot suppress legitimate later deliveries.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy/SQLModel, PostgreSQL outbox, pytest.

---

### Task 1: Bound CRM automation changes and preserve delete-time state

**Files:**
- Modify: `tests/service/automation/producers/test_crm.py`
- Modify: `tests/service/crm/test_deal_delete_mutation_events.py`
- Modify: `src/service/automation/producers/crm.py`
- Modify: `src/service/crm/deals/write_service.py`
- Modify: `src/service/crm/deals/mutation_event_service.py`

- [ ] **Step 1: Write failing adapter tests**

Add regressions proving an update containing a supported field plus `description` emits only the supported field, while a contact update containing only `job_title` produces no automation envelope.

```python
envelope = CRMEventAdapter(source.organisation_id).from_mutation(source)
assert set(envelope.changes) == {"amount"}

source.entity_type = "contact"
source.changed_fields = {"job_title": {"old": "CFO", "new": "COO"}}
assert CRMEventAdapter(source.organisation_id).from_mutation(source) is None
```

- [ ] **Step 2: Run the adapter regressions and verify RED**

Run: `python -m pytest -q tests/service/automation/producers/test_crm.py`

Expected: the adapter still forwards `description` and still creates an envelope for the audit-only contact change.

- [ ] **Step 3: Filter update changes through the base registry**

Use `AUTOMATION_REGISTRY.event(event_key).watch_fields` as the standard-field allowlist, retain deal `custom_properties.*` changes for tenant-schema validation, and return `None` when an update has no automation-visible changes.

```python
definition = AUTOMATION_REGISTRY.event(event_key)
changes = {
    key: {"before": value["old"], "after": value["new"]}
    for key, value in event.changed_fields.items()
    if key in definition.watch_fields
    or (event.entity_type == "deal" and key.startswith("custom_properties."))
}
if lifecycle == "updated" and not changes:
    return None
```

- [ ] **Step 4: Write the pre-delete snapshot regression and verify RED**

Extend the deal deletion test to assert that the record passed to the mutation event still has `deal_status == OPEN`, then run that test and observe the missing `record` argument.

- [ ] **Step 5: Capture and pass the pre-delete record**

Capture `DealMutationPolicy.automation_record(deal)` before changing the row, thread it through `_record_mutation_event`, and let `DealMutationEventService.record_event` use the supplied record instead of rebuilding it from the mutated deal.

- [ ] **Step 6: Run CRM regressions GREEN**

Run: `python -m pytest -q tests/service/automation/producers/test_crm.py tests/service/crm/test_deal_delete_mutation_events.py tests/service/crm/test_crm_mutation_event_service.py`

Expected: all selected tests pass.

### Task 2: Keep direct trigger routing executable and correctly authorized

**Files:**
- Modify: `tests/service/automation/triggers/test_schedule.py`
- Modify: `tests/service/automation/triggers/test_activation_service.py`
- Modify: `tests/service/automation/triggers/test_direct.py`
- Modify: `src/service/automation/triggers/schedule.py`
- Modify: `src/service/automation/triggers/activation.py`
- Modify: `src/service/automation/triggers/direct.py`
- Modify: `src/service/automation/triggers/service.py`

- [ ] **Step 1: Add failing direct-source regressions**

Cover three contracts: a one-time schedule clears `next_trigger_at` but remains loadable for queued fanout, a schedule envelope cannot target a manual binding, and an organization admin can invoke another admin's organization-service manual binding.

- [ ] **Step 2: Run the three tests and verify RED**

Run the individual pytest node IDs and confirm the current results are respectively paused, matched, and creator-rejected.

- [ ] **Step 3: Implement the minimum direct-source fixes**

Leave a completed one-time schedule `ACTIVE` with `next_trigger_at=None`, return `direct_event_mismatch` when a direct envelope key is absent from the binding projection, and pass `is_admin` into manual invocation so only authorized admins bypass creator equality for organization-service bindings.

```python
if envelope.route.mode == AutomationRouteMode.DIRECT and envelope.event_key not in trigger.match_event_keys:
    return "direct_event_mismatch"
```

- [ ] **Step 4: Run direct-source regressions GREEN**

Run: `python -m pytest -q tests/service/automation/triggers/test_schedule.py tests/service/automation/triggers/test_activation_service.py tests/service/automation/triggers/test_direct.py tests/service/automation/triggers/test_access.py tests/service/automation/triggers/test_service.py`

Expected: all selected tests pass.

### Task 3: Isolate queued CRM schema drift per candidate

**Files:**
- Modify: `tests/service/automation/triggers/test_activation_service.py`
- Modify: `src/service/automation/triggers/activation.py`

- [ ] **Step 1: Add failing historical-schema tests**

Create a queued deal update containing a removed custom field and prove an unrelated binding still creates a run while the affected binding receives `binding_contract_invalid`. Add a type-drift case proving matcher evaluation failure pauses only that candidate.

- [ ] **Step 2: Run the new tests and verify RED**

Expected: activation raises before candidate lookup or aborts the whole dispatch on `MatcherEvaluationError`.

- [ ] **Step 3: Separate event replay validation from binding validation**

Validate CRM envelopes against `AUTOMATION_REGISTRY`, which is stable and permits historical custom paths, then use the current tenant registry for binding compilation and matching. Treat `MatcherEvaluationError` as a candidate-level invalid binding alongside matcher validation errors.

```python
validation_registry = AUTOMATION_REGISTRY if envelope.event_key.startswith("crm.") else registry
validation_registry.event(envelope.event_key).validate_envelope(envelope)
event_definition = registry.event(envelope.event_key)
```

- [ ] **Step 4: Run activation and worker tests GREEN**

Run: `python -m pytest -q tests/service/automation/triggers/test_activation_service.py tests/workers/chat_worker/services/test_automation_event_activation_service.py`

Expected: all selected tests pass and unrelated candidates retain outcomes.

### Task 4: Bound webhook payloads and expire fallback identities

**Files:**
- Modify: `tests/service/automation/triggers/test_webhook.py`
- Modify: `tests/api/routers/automation/test_webhook_ingress.py`
- Modify: `src/service/automation/triggers/webhook.py`
- Modify: `docs/API Documentation/automation/v1-agent-triggers-api.md`

- [ ] **Step 1: Add failing webhook regressions**

Prove an exact unsigned-delivery retry with the same timestamp is idempotent, the same body with a fresh timestamp after six minutes creates a new event, and a valid JSON body at the public raw limit fits inside the canonical envelope.

- [ ] **Step 2: Run webhook tests and verify RED**

Expected: the post-window delivery reuses the permanent payload hash and the near-limit envelope raises Pydantic validation.

- [ ] **Step 3: Implement timestamp-scoped fallback identity and payload headroom**

When no external delivery ID exists, append the canonical signed timestamp to the payload hash. Reserve 4 KiB of the envelope budget for route, actor, source, and idempotency metadata by setting the public body cap to 60 KiB.

```python
WEBHOOK_MAX_BODY_BYTES = 60 * 1024
return f"{hashlib.sha256(canonical).hexdigest()}:{timestamp}"
```

- [ ] **Step 4: Update the frontend-facing API limit and run webhook tests GREEN**

Document the 61,440-byte public limit and run both webhook service and handler suites.

### Task 5: Restore affected writer fixtures and verify the complete slice

**Files:**
- Modify: `tests/service/crm/test_contact_mutation_events.py`
- Modify: `tests/service/crm/test_account_mutation_events.py`

- [ ] **Step 1: Inject a fake automation enqueuer into writer tests**

Keep mutation-ledger assertions focused by replacing each service's `_automation_events` dependency with `AsyncMock()` in shared test setup instead of teaching fake SQL sessions the production outbox protocol.

- [ ] **Step 2: Run contact/account writer suites**

Run: `python -m pytest -q tests/service/crm/test_contact_mutation_events.py tests/service/crm/test_account_mutation_events.py`

Expected: all tests pass.

- [ ] **Step 3: Run focused automation and CRM verification**

Run the producer, dispatch, registry, activation, direct, schedule, webhook, manual API, webhook API, and CRM writer suites changed by this plan.

- [ ] **Step 4: Run static and worktree checks**

Run Ruff on changed Python files, the repository Python comment-smell checker, staged and unstaged `git diff --check`, and compare `git status --short` against the captured baseline plus this plan's intended files.

- [ ] **Step 5: Audit all nine findings**

Confirm each reported failure now has a regression that failed before its implementation and passes afterward. Do not claim the broader repository is green unless a broader test command was actually run and passed.
