# AI-Native Trigger Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dev-only trigger backend with one registry-driven, typed, replay-safe automation pipeline for match events and direct manual, schedule, and webhook invocations.

**Architecture:** A single `AutomationRegistry` owns producer contracts and user-facing trigger definitions. Every durable source writes a validated `AutomationEventEnvelope` to the existing transactional dispatch table; one source-agnostic activation service applies principal scope, causation policy, watches, and typed filters before inserting an idempotent `AgentRun` that carries immutable trigger, agent, and event snapshots. Personal and organisation-service execution are separate identities with fail-closed save-time and runtime capability policy.

**Tech Stack:** Python 3.11, Pydantic 2, FastAPI, SQLModel/SQLAlchemy, PostgreSQL JSONB/ARRAY/RLS, Celery, Redis, pytest, Alembic.

**Execution note:** The user requested phased inline implementation. Do not create commits or publish the branch unless separately asked. Preserve unrelated untracked files.

---

## File map

New focused files:

- `src/service/automation/events/contracts.py`: strict immutable envelope and nested route/source/actor/subject/change contracts.
- `src/service/automation/triggers/registry.py`: backend-owned event/trigger definitions, field types, operator catalogs, producer availability, and preset compilation metadata.
- `src/service/automation/triggers/matcher.py`: pure path resolution, watch evaluation, typed predicate validation/evaluation, and fixed-plus-user filter composition.
- `src/service/automation/triggers/principal_policy.py`: valid principal/scope/audience combinations and organisation-service capability allowlists.
- `src/service/automation/triggers/schedule.py`: recurrence validation and DST-correct next-occurrence calculation.
- `src/service/automation/triggers/direct.py`: subject-isolated manual/schedule direct-envelope construction.
- `src/service/automation/triggers/webhook.py`: endpoint token/secret handling, signature and replay verification, typed payload mapping, and direct dispatch.
- `src/service/automation/producers/crm.py`: mutation-ledger-to-envelope adapter shared by CRM writers.
- `src/service/automation/producers/email.py`: safe email snapshot adapter with body/attachment references only.
- `src/service/automation/producers/meeting.py`: durable meeting/transcript/summary snapshot adapter.
- `src/service/automation/producers/signals.py`: persisted risk/blocker and buyer/stakeholder signal transition adapter.
- `src/service/automation/producers/agent.py`: run lifecycle event adapter, with causation metadata.
- `migrations/org_migrations/versions/3c7f79637712_087_ai_native_automation.py`: clean dev-data reset and replacement storage projections.

Existing files changed in place:

- `src/domain/models/organisation_tables/agent_models.py`: replacement enums, trigger/dispatch/run projections, indexes, constraints, and audit fields.
- `src/api/schemas/automation_trigger_models.py`: client-decision request format plus generic registry/catalog response format.
- `src/api/schemas/agent_run_models.py`: event/principal/audience projections instead of the old CRM source tuple.
- `src/service/automation/triggers/catalog.py`: serialize the registry and merge organisation-specific CRM fields/options.
- `src/service/automation/triggers/validator.py`: validate/normalize a binding against the registry, matcher, schedule, principal policy, and current options.
- `src/service/automation/triggers/access.py`: creator-visible CRUD and indexed event candidate lookup.
- `src/service/automation/triggers/service.py`: derive every server-owned projection, pin target, hash the full runtime contract, and manage direct secrets/schedules.
- `src/service/automation/triggers/serializers.py`: serialize only the new public contract.
- `src/service/automation/triggers/activation_access.py`: load candidates/subjects/principals and insert idempotent runs without source branches.
- `src/service/automation/triggers/activation.py`: generic match/direct activation, skip/pause telemetry, import and causation guards.
- `src/service/automation/triggers/runtime.py`: build and validate the immutable run execution envelope.
- `src/service/automation/events/access.py`: idempotent dispatch insert/load/update by organisation and envelope idempotency key.
- `src/service/automation/events/service.py`: validate known events and persist the canonical envelope.
- `src/service/automation/events/delivery.py`: retain staged post-commit publication using the generalized dispatch identity.
- `src/service/automation/runs/*`: principal-aware claim/read/lifecycle/runtime checks and run-outcome emission.
- `src/api/routers/automation/triggers_handler.py`: pass authenticated role, expose invoke/webhook-rotation endpoints, and retain transaction ownership.
- `src/api/routers/automation/runs_handler.py`: apply audience-aware read policy.
- `src/agents/context.py`: carry immutable execution principal type/id separately from optional human credential user.
- `src/agents/tools/registry.py`: expose one code-owned organisation-service allowlist.
- `src/agents/workflows/user_configured/agent.py`: reject capabilities outside the runtime principal policy before tool construction.
- `src/service/automation/runs/runtime_executor.py`: compare principal, subject, target, and effective tools to the claimed envelope.
- `src/workers/chat_worker/services/agent_run_execution_service.py`: revalidate organisation/principal/subject/target and construct personal or service context.
- `src/workers/chat_worker/services/automation_event_activation_service.py`: validate stored envelope and commit dispatch/run outcomes atomically.
- `src/workers/chat_worker/tasks/automation_event_tasks.py`: add per-org schedule processing and all-org fanout tasks.
- `src/workers/chat_worker/config.py` and `celery_app.py`: register/route/beat the schedule dispatcher.
- CRM write/mutation services, email delta sync, meeting summary boundaries, AI insight writers, and run lifecycle services: enqueue only after their durable row change and before commit.

Tests stay close to these seams under `tests/service/automation`, `tests/api/routers/automation`, `tests/workers/chat_worker`, `tests/migrations`, and the affected domain service test directories.

---

## Phase 1: Canonical contracts, registry, matcher, and storage

### Task 1: Strict automation event envelope

**Files:**

- Create: `src/service/automation/events/contracts.py`
- Create: `tests/service/automation/events/test_contracts.py`

- [ ] **Step 1: Write failing contract tests**

Cover strict JSON, aware UTC timestamps, route invariants, paired subject identity, paired change keys, non-negative depth, extra-field rejection, and the 64 KiB serialized ceiling.

```python
def test_match_route_forbids_direct_identity(valid_envelope_data):
    valid_envelope_data["route"] = {
        "mode": "match",
        "trigger_id": str(uuid4()),
        "config_hash": None,
    }
    with pytest.raises(ValidationError):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_envelope_rejects_non_json_and_oversize_values(valid_envelope_data):
    valid_envelope_data["record"] = {"bad": Decimal("1")}
    with pytest.raises(ValidationError):
        AutomationEventEnvelope.model_validate(valid_envelope_data)
    valid_envelope_data["record"] = {"large": "x" * 65_536}
    with pytest.raises(ValidationError, match="64 KiB"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest tests/service/automation/events/test_contracts.py -q`

Expected: collection fails because `events.contracts` does not exist.

- [ ] **Step 3: Implement the minimal strict models**

Use frozen Pydantic models with `extra="forbid"`, strict scalar validation, `allow_inf_nan=False`, and one model-level byte-size check over `model_dump(mode="json")`.

```python
class AutomationEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    event_id: UUID
    organisation_id: UUID
    event_key: str
    route: AutomationEventRoute
    occurred_at: AwareDatetime
    source: AutomationEventSource
    actor: AutomationEventActor
    subject: AutomationEventSubject
    record: dict[str, JsonValue] = Field(default_factory=dict)
    changes: dict[str, AutomationEventChange] = Field(default_factory=dict)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    references: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_id: str | None = None
    caused_by_run_id: UUID | None = None
    causation_depth: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=512)
```

- [ ] **Step 4: Run the focused test and static checks**

Run: `uv run pytest tests/service/automation/events/test_contracts.py -q`

Expected: all tests pass.

Run: `uv run mypy src/service/automation/events/contracts.py`

Expected: no errors.

### Task 2: One typed registry for events and trigger presets

**Files:**

- Create: `src/service/automation/triggers/registry.py`
- Replace: `src/service/automation/triggers/catalog.py`
- Replace: `tests/service/automation/triggers/test_catalog.py`
- Create: `tests/service/automation/triggers/test_registry.py`

- [ ] **Step 1: Write failing registry invariants and catalog tests**

Assert unique event/trigger keys, every trigger references existing event definitions, unavailable producers are hidden, no direct family uses match routing, the deal preset table compiles to `crm.deal.updated`, and catalog fields expose stable path IDs/types/operators rather than bespoke forms.

```python
def test_amount_crossed_preset_compiles_to_one_update_event():
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.amount_crossed")
    compiled = definition.compile({"direction": "up", "threshold": "100000.00"})
    assert definition.match_event_keys == ("crm.deal.updated",)
    assert compiled.watch.paths == ("amount",)
    assert compiled.filter == {
        "all": [
            {"path": "changes.amount.before", "operator": "lt", "value": "100000.00"},
            {"path": "changes.amount.after", "operator": "gte", "value": "100000.00"},
        ]
    }
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_registry.py tests/service/automation/triggers/test_catalog.py -q`

Expected: missing registry definitions or old catalog-shape assertions fail.

- [ ] **Step 3: Implement immutable registry data and the live catalog subset**

Define `FieldType`, `FieldDefinition`, `EventDefinition`, `TriggerDefinition`, `CompiledMatcher`, and `AutomationRegistry`. Register the complete destination taxonomy, but mark entries `producer_available=False` until Task 12 wires a durable producer. Initially live: manual, schedule, webhook, deal created/updated/deleted, contact/account created/updated/deleted, email received/sent, transcript ready, summary ready, persisted risk/blocker and stakeholder signals, and agent-run terminal events where a durable boundary exists.

```python
class AutomationRegistry:
    def __init__(self, events: Iterable[EventDefinition], triggers: Iterable[TriggerDefinition]):
        self.events = _unique_map("event_key", events)
        self.triggers = _unique_map("trigger_key", triggers)
        self._validate_references()

    def catalog_triggers(self) -> tuple[TriggerDefinition, ...]:
        return tuple(
            item for item in self.triggers.values()
            if item.producer_available
            and all(self.events[key].producer_available for key in item.match_event_keys)
        )
```

Use declarative field builders only where they remove repetition; do not add a plugin API or runtime registration.

- [ ] **Step 4: Run registry/catalog tests**

Run: `uv run pytest tests/service/automation/triggers/test_registry.py tests/service/automation/triggers/test_catalog.py -q`

Expected: all pass.

### Task 3: Generic typed matcher

**Files:**

- Create: `src/service/automation/triggers/matcher.py`
- Replace: `tests/service/automation/triggers/test_activation_matcher.py`
- Create: `tests/service/automation/triggers/test_matcher_validation.py`

- [ ] **Step 1: Write exhaustive failing table-driven tests**

Cover text normalization/exact text, enum/relation, integer, decimal string, boolean, date, datetime, list operators, inclusive `between`, missing versus explicit null, create/update watch behavior, any/all nesting up to two group levels, 20-leaf cap, invalid path/operator/value, and fixed/user `AND` composition.

```python
@pytest.mark.parametrize(
    ("field_type", "operator", "actual", "expected", "matches"),
    [
        (FieldType.DECIMAL, "gte", "125000.00", "100000.00", True),
        (FieldType.TEXT, "contains", "Straße", "STRASSE", True),
        (FieldType.DATE, "before", "2026-08-04", "2026-08-05", True),
        (FieldType.LIST, "contains_all", ["a", "b"], ["b", "a"], True),
    ],
)
def test_typed_operators(field_type, operator, actual, expected, matches):
    assert evaluate_operator(field_type, operator, actual, expected) is matches
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_activation_matcher.py tests/service/automation/triggers/test_matcher_validation.py -q`

Expected: matcher module is missing.

- [ ] **Step 3: Implement pure validation and evaluation**

Resolve paths through the registry's exact path map; never split client-supplied arbitrary paths. Internally, each `FieldDefinition` owns an explicit accessor tuple such as `("changes", "amount", "after")`.

```python
MISSING = object()


def matches_binding(
    envelope: AutomationEventEnvelope,
    event: EventDefinition,
    watch: WatchConfig | None,
    filter_tree: FilterGroup | FilterLeaf | None,
) -> bool:
    if watch and envelope.source.operation == "update":
        present = [path in envelope.changes for path in watch.paths]
        if (watch.mode == "all" and not all(present)) or (
            watch.mode == "any" and not any(present)
        ):
            return False
    return filter_tree is None or _evaluate_node(envelope, event, filter_tree)
```

Use `Decimal`, `date.fromisoformat`, and aware UTC datetime parsing; reject floats for decimal fields and non-finite numbers globally.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/service/automation/triggers/test_activation_matcher.py tests/service/automation/triggers/test_matcher_validation.py -q`

Expected: all pass.

### Task 4: Replace storage projections with a clean reset migration

**Files:**

- Modify: `src/domain/models/organisation_tables/agent_models.py`
- Create: `migrations/org_migrations/versions/3c7f79637712_087_ai_native_automation.py`
- Replace: `tests/test_agent_automation_schema.py`
- Create: `tests/migrations/org/test_087_ai_native_automation.py`
- Modify: `tests/migrations/test_org_migration_head_contract.py`

- [ ] **Step 1: Write failing schema and migration-contract tests**

Assert exact new columns/enums/checks/indexes, no legacy `trigger_type/source_domain/event_type/owner_user_id` trigger columns, unique dispatch idempotency per org, unique run `(organisation_id, trigger_id, event_id)`, and migration head/down revision.

```python
def test_trigger_has_registry_owned_projections_only():
    columns = AutomationTrigger.__table__.columns
    assert {"trigger_key", "family", "match_event_keys", "execution_principal_type"} <= set(columns)
    assert {"trigger_type", "source_domain", "event_type", "owner_user_id"}.isdisjoint(columns)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_agent_automation_schema.py tests/migrations/org/test_087_ai_native_automation.py tests/migrations/test_org_migration_head_contract.py -q`

Expected: old model and missing migration fail.

- [ ] **Step 3: Implement model replacement and reset migration**

The upgrade explicitly deletes disposable dev rows in FK-safe order, drops legacy constraints/indexes/columns, adds the new projections, and rebuilds constraints/indexes. Keep `user_agents` data. Use PostgreSQL `ARRAY(Text)` for `match_event_keys` and a partial GIN index for active match candidates.

```python
class AutomationPrincipalType(str, Enum):
    USER = "user"
    ORGANISATION_SERVICE = "organisation_service"


class AutomationTriggerFamily(str, Enum):
    EVENT = "event"
    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
```

Dispatch stores `event_key`, `route_mode`, nullable `direct_trigger_id`, `idempotency_key`, and `envelope`. Run stores `created_by_user_id`, principal type/id, audience, `event_id`, `event_key`, and all immutable snapshots. Add activation telemetry JSON and skip/pause reason fields only where the design requires durable visibility.

- [ ] **Step 4: Run schema/migration tests and render SQL**

Run: `uv run pytest tests/test_agent_automation_schema.py tests/migrations/org/test_087_ai_native_automation.py tests/migrations/test_org_migration_head_contract.py -q`

Expected: all pass.

Run: `uv run alembic -c migrations/org_migrations/alembic.ini upgrade head --sql`

Expected: SQL renders without exceptions and ends at revision `3c7f79637712`.

---

## Phase 2: Binding API and security policy

### Task 5: New request/response contract and principal policy

**Files:**

- Replace: `src/api/schemas/automation_trigger_models.py`
- Create: `src/service/automation/triggers/principal_policy.py`
- Modify: `src/agents/tools/registry.py`
- Replace: `tests/api/routers/automation/test_trigger_contracts.py`
- Create: `tests/service/automation/triggers/test_principal_policy.py`

- [ ] **Step 1: Write failing contract/security tests**

Assert the client cannot submit derived event keys, principal IDs, target snapshots, config hash, fixed matcher content, or webhook secrets. Assert personal bindings require personal scope/audience. Assert organisation bindings require an admin and allow only none/tenant-validated explicit users/subject participants. Assert the service-principal tool and skill sets fail closed.

```python
def test_organisation_binding_rejects_user_credential_tool():
    with pytest.raises(AutomationTriggerValidationError, match="service principal"):
        validate_principal_capabilities(
            principal_type=AutomationPrincipalType.ORGANISATION_SERVICE,
            allowed_tools=("send_email",),
            allowed_skills=(),
        )
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/api/routers/automation/test_trigger_contracts.py tests/service/automation/triggers/test_principal_policy.py -q`

Expected: old schema shape/policy fails.

- [ ] **Step 3: Implement the decision-only schemas and policy**

```python
class AutomationTriggerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    target_id: UUID
    trigger_key: str
    status: AutomationTriggerStatus = AutomationTriggerStatus.ACTIVE
    config: dict[str, JsonValue] = Field(default_factory=dict)
    watch: WatchRequest | None = None
    filter: FilterRequest | None = None
    principal_mode: Literal["personal", "organisation"]
    scope: ScopeRequest
    audience: AudienceRequest
    task: TaskRequest
    delivery: DeliveryRequest = Field(default_factory=DeliveryRequest)
```

Define one conservative organisation-service allowlist from registered tool enum values. Exclude all provider-credential, communication-send, calendar mutation, reminder, personal mailbox, and personal task tools. Permit no skills initially unless a code-owned service-safe set is explicitly declared.

- [ ] **Step 4: Run contract/security tests**

Run: `uv run pytest tests/api/routers/automation/test_trigger_contracts.py tests/service/automation/triggers/test_principal_policy.py -q`

Expected: all pass.

### Task 6: Registry-driven validation, persistence, serialization, and API

**Files:**

- Replace: `src/service/automation/triggers/validator.py`
- Replace: `src/service/automation/triggers/access.py`
- Replace: `src/service/automation/triggers/service.py`
- Replace: `src/service/automation/triggers/serializers.py`
- Modify: `src/api/routers/automation/triggers_handler.py`
- Replace: `tests/service/automation/triggers/test_validator.py`
- Replace: `tests/service/automation/triggers/test_access.py`
- Replace: `tests/service/automation/triggers/test_service.py`

- [ ] **Step 1: Write failing binding tests**

Cover server-derived event keys/family/schema/principal ID/preset matcher/target snapshot/hash, editable user watch only on supported definitions, option validation, full hash sensitivity, duplicate detection, archive semantics, admin gate, and paused-on-schema-drift behavior.

```python
async def test_create_derives_amount_crossing_matcher_and_principal(service, body):
    created = await service.create(body)
    assert created.trigger_key == "crm.deal.amount_crossed"
    assert created.match_event_keys == ["crm.deal.updated"]
    assert created.watch_config == {"mode": "any", "paths": ["amount"]}
    assert created.execution_principal_id == service.user_id
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_validator.py tests/service/automation/triggers/test_access.py tests/service/automation/triggers/test_service.py -q`

Expected: imports/signatures/old assertions fail.

- [ ] **Step 3: Implement the binding service**

Normalize through the registry, merge fixed and user filters with `AND`, validate current option IDs, resolve explicit audience users tenant-locally, validate pinned agent capabilities for the principal, and hash canonical JSON containing every run-affecting projection.

```python
canonical = {
    "trigger_key": definition.trigger_key,
    "config": normalized.config,
    "watch": normalized.watch,
    "filter": normalized.filter,
    "principal": normalized.principal,
    "scope": normalized.scope,
    "audience": normalized.audience,
    "task": normalized.task,
    "delivery": normalized.delivery,
    "target_revision": agent.revision,
}
config_hash = sha256(canonical_json(canonical).encode()).hexdigest()
```

Use `created_by_user_id` for CRUD visibility. Organisation bindings are visible to organisation admins; personal bindings remain creator-owned. Return webhook endpoint metadata but never the stored secret digest.

- [ ] **Step 4: Run service and router tests**

Run: `uv run pytest tests/service/automation/triggers tests/api/routers/automation/test_trigger_contracts.py -q`

Expected: all binding/catalog tests pass.

---

## Phase 3: Generalized dispatch and source-agnostic activation

### Task 7: Envelope dispatch service and replay-safe delivery

**Files:**

- Replace: `src/service/automation/events/access.py`
- Replace: `src/service/automation/events/service.py`
- Modify: `src/service/automation/events/delivery.py`
- Replace: `tests/service/automation/events/test_event_dispatch_access.py`
- Replace: `tests/service/automation/events/test_event_dispatch_service.py`
- Modify: `tests/service/automation/events/test_direct_delivery.py`

- [ ] **Step 1: Write failing dispatch tests**

Assert registry validation before insert, org/event/idempotency projection from the envelope, conflict returns the same dispatch, conflicting payload for the same idempotency key fails, staged publish occurs only after commit, and broker failure leaves the durable row replayable.

```python
async def test_enqueue_replay_returns_same_dispatch(service, envelope):
    first = await service.enqueue(envelope)
    second = await service.enqueue(envelope)
    assert second.dispatch_id == first.dispatch_id
    assert service.session.add.call_count == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/events -q`

Expected: old service signature/storage assertions fail.

- [ ] **Step 3: Implement one enqueue boundary**

```python
async def enqueue(self, envelope: AutomationEventEnvelope) -> AutomationEventDispatch:
    definition = self.registry.event(envelope.event_key)
    definition.validate_envelope(envelope)
    return await self.access.insert_or_get(envelope)
```

Compare canonical envelope JSON on idempotency conflicts. Do not accept separate tenant/source/event arguments that can drift from the envelope.

- [ ] **Step 4: Run event tests**

Run: `uv run pytest tests/service/automation/events -q`

Expected: all pass.

### Task 8: Generic activation and idempotent run creation

**Files:**

- Replace: `src/service/automation/triggers/activation_access.py`
- Replace: `src/service/automation/triggers/activation.py`
- Replace: `tests/service/automation/triggers/test_activation_access.py`
- Replace: `tests/service/automation/triggers/test_activation_service.py`
- Modify: `src/workers/chat_worker/services/automation_event_activation_service.py`
- Replace: `tests/workers/chat_worker/services/test_automation_event_activation_service.py`

- [ ] **Step 1: Write failing activation matrix**

Prove exact once for multi-field matches, create/update watch semantics, delete pre-snapshot, wrong tenant, personal out-of-scope, organisation scope, unavailable/paused/archived, schema drift pause, direct config-hash mismatch, initial sync exclusion/capped opt-in, causation depth/duplicate-chain guards, and one broken binding not poisoning siblings.

```python
async def test_multifield_update_creates_one_run(activation, dispatch):
    dispatch.envelope["changes"] = {
        "stage_id": {"before": "a", "after": "b"},
        "amount": {"before": "90000.00", "after": "125000.00"},
    }
    runs = await activation.activate(dispatch)
    assert len(runs) == 1
    assert runs[0].idempotency_key == f"{runs[0].trigger_id}:{dispatch.dispatch_id}"
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_activation_access.py tests/service/automation/triggers/test_activation_service.py tests/workers/chat_worker/services/test_automation_event_activation_service.py -q`

Expected: CRM-specific activation fails new cases.

- [ ] **Step 3: Implement source-agnostic activation**

Candidate access uses `match_event_keys @> ARRAY[event_key]` for match routes and exact trigger ID for direct. Scope compares event-time `record.owner_id`/`record.created_by_user_id`; no live CRM entity load occurs in matching. Persist one activation outcome per candidate in dispatch telemetry, including `matched`, skip/pause reason, and run ID.

```python
if envelope.route.mode == RouteMode.MATCH:
    candidates = await access.list_active_candidates(envelope.event_key)
else:
    candidates = [await access.get_direct_candidate(envelope.route.trigger_id)]

for trigger in candidates:
    outcome = await self._evaluate(trigger, envelope)
    if outcome.matches:
        runs.append(await access.insert_run_if_absent(trigger, envelope))
```

The worker validates the stored envelope first, inserts runs and marks the dispatch processed in the same transaction, commits, then publishes only newly inserted run IDs.

- [ ] **Step 4: Run activation tests**

Run: `uv run pytest tests/service/automation/triggers/test_activation_access.py tests/service/automation/triggers/test_activation_service.py tests/workers/chat_worker/services/test_automation_event_activation_service.py -q`

Expected: all pass.

### Task 9: Principal-aware run runtime and read policy

**Files:**

- Modify: `src/agents/context.py`
- Modify: `src/agents/tools/registry.py`
- Modify: `src/agents/workflows/user_configured/agent.py`
- Replace: `src/service/automation/triggers/runtime.py`
- Modify: `src/service/automation/runs/runtime_executor.py`
- Modify: `src/service/automation/runs/claim_service.py`
- Modify: `src/service/automation/runs/read_access.py`
- Modify: `src/service/automation/runs/read_service.py`
- Modify: `src/workers/chat_worker/services/agent_run_execution_service.py`
- Update: `tests/service/automation/runs/test_runtime_executor.py`
- Update: `tests/service/automation/triggers/test_runtime_mapper.py`
- Update: `tests/workers/chat_worker/services/test_agent_run_execution_service.py`
- Update: `tests/service/automation/runs/test_read_service.py`

- [ ] **Step 1: Write failing runtime security tests**

Assert organisation/principal/subject/target/capability revalidation; user principal must still be active and entitled to the subject; service principal has no borrowed human user ID and cannot instantiate a forbidden tool; audience grants read/delivery only and never runtime data scope.

```python
async def test_service_principal_does_not_borrow_creator_credentials(loader, envelope):
    context = await loader(envelope_for_organisation_service(envelope))
    assert context.user_id is None
    assert context.execution_principal_type == "organisation_service"
    assert context.execution_principal_id == envelope.organisation_id
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/runs/test_runtime_executor.py tests/service/automation/triggers/test_runtime_mapper.py tests/workers/chat_worker/services/test_agent_run_execution_service.py tests/service/automation/runs/test_read_service.py -q`

Expected: old owner-only runtime fails.

- [ ] **Step 3: Implement the second trust boundary**

```python
@dataclass
class AgentContext:
    organisation_id: UUID
    execution_principal_type: str = "user"
    execution_principal_id: UUID | None = None
    user_id: UUID | None = None
    # existing immutable subject/deal fields remain
```

At load time query current organisation/principal/subject access and current pinned target identity/revision. Re-run `validate_principal_capabilities` on the immutable agent snapshot before tool construction. Personal runs set both principal ID and `user_id`; service runs set the organisation principal ID and `user_id=None`.

- [ ] **Step 4: Run runtime/read tests**

Run: `uv run pytest tests/service/automation/runs tests/service/automation/triggers/test_runtime_mapper.py tests/workers/chat_worker/services/test_agent_run_execution_service.py -q`

Expected: all pass.

---

## Phase 4: Durable producers

### Task 10: CRM mutation adapter and lifecycle producers

**Files:**

- Create: `src/service/automation/producers/__init__.py`
- Create: `src/service/automation/producers/crm.py`
- Modify: `src/service/crm/mutations/event_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Modify: `src/service/crm/contacts/write_service.py`
- Modify: `src/service/crm/accounts/write_service.py`
- Modify: `src/service/crm/leads/write_service.py`
- Modify: `src/service/crm/tasks/write_service.py`
- Modify: `src/service/crm/notes/write_service.py`
- Replace/add focused tests beside each touched service and `tests/service/automation/producers/test_crm.py`.

- [ ] **Step 1: Write failing adapter and writer tests**

Cover create/update/delete snapshots, full exact changed field set, no-op suppression, canonical decimal/date/datetime/UUID/list/custom values, every existing deal semantic mutation normalizing to one `crm.deal.updated`, actor/source mode/causation propagation, and outbox insert before source commit.

```python
def test_existing_deal_semantic_operations_emit_one_update(mutation):
    envelope = build_crm_envelope(mutation, record=deal_snapshot())
    assert envelope.event_key == "crm.deal.updated"
    assert envelope.changes == mutation.changed_fields
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/producers/test_crm.py tests/service/crm/test_deal_mutation_event_service.py tests/service/crm/test_deal_lifecycle_mutation_events.py tests/service/crm/test_deal_delete_mutation_events.py tests/service/crm/test_contact_mutation_events.py tests/service/crm/test_account_mutation_events.py tests/service/crm/contacts/test_write_service.py tests/service/crm/test_account_write_service.py tests/service/crm/test_task_service_boundaries.py tests/service/crm/test_notes_service_boundaries.py -q`

Expected: adapter missing and old semantic fanout assertions fail.

- [ ] **Step 3: Implement the shared adapter and wire actual durable boundaries**

Reuse the mutation ledger's operation/changed fields and existing JSON normalizer. The automation call happens once per mutation event, while `semantic_event_types` remains only for verified last-activity/audit callers.

```python
envelope = CRMEventAdapter(self.organisation_id).from_mutation(
    mutation=mutation,
    record=safe_record_snapshot,
    source_mode=source_mode,
    actor=actor,
    causation=causation,
)
await AutomationEventService(self.session, self.organisation_id).enqueue(envelope)
```

Only mark registry entries producer-available after the writer tests demonstrate the same-transaction enqueue.

- [ ] **Step 4: Run CRM and automation tests**

Run: `uv run pytest tests/service/automation tests/service/crm -q`

Expected: all selected tests pass.

### Task 11: Email and meeting producers through the same envelope

**Files:**

- Create: `src/service/automation/producers/email.py`
- Create: `src/service/automation/producers/meeting.py`
- Modify: `src/service/integrations/email_delta_sync_service.py`
- Modify: `src/service/meeting_bot/summary_trigger_service.py`
- Modify the durable meeting-summary completion boundary in `src/workers/deal_worker/tasks/meeting_insight_tasks/meeting_summary_task.py`.
- Add: `tests/service/automation/producers/test_email.py`
- Add: `tests/service/automation/producers/test_meeting.py`
- Update focused email/meeting service tests.

- [ ] **Step 1: Write failing privacy/transaction tests**

Assert changed inbound rows emit `email.message.received` in the folder session before commit, outbound rows emit sent when known, replay keys use durable email IDs, bodies/snippets/attachment bytes never enter envelopes, and transcript/summary events emit only after durable state with content references. Prove activation uses the same matcher and contains no source-domain branch.

```python
def test_email_envelope_references_content_without_copying_body(email):
    envelope = EmailEventAdapter().received(email)
    assert "body" not in envelope.record
    assert "body" not in envelope.attributes
    assert envelope.references["email_id"] == str(email.email_id)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/producers/test_email.py tests/service/automation/producers/test_meeting.py tests/service/integrations/test_email_delta_sync_service.py tests/service/meeting_bot/test_meeting_summary_trigger_service.py tests/workers/deal_worker/tasks/meeting_insight_tasks/test_meeting_summary_task.py -q`

Expected: producer modules/events are missing.

- [ ] **Step 3: Implement adapters and same-transaction wiring**

Load changed emails in the existing folder session, classify direction from durable fields, enqueue before that service commits, and stage delivery after commit. At transcript ready, use meeting/calendar IDs and transcript metadata/reference only. At summary completion, enqueue in the transaction that marks the summary durable.

- [ ] **Step 4: Run focused and cross-domain matcher tests**

Run: `uv run pytest tests/service/automation/producers tests/service/automation/triggers/test_activation_service.py tests/service/integrations/test_email_delta_sync_service.py tests/service/meeting_bot/test_meeting_summary_trigger_service.py tests/workers/deal_worker/tasks/meeting_insight_tasks/test_meeting_summary_task.py -q`

Expected: all pass and one generic activation path handles all three domains.

### Task 12: Persisted AI signals and agent lifecycle events

**Files:**

- Create: `src/service/automation/producers/signals.py`
- Create: `src/service/automation/producers/agent.py`
- Modify: `src/service/crm/risks/write_service.py`
- Modify: `src/service/crm/risks/lifecycle_service.py`
- Modify: `src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py`
- Modify: `src/service/crm/insights/stakeholder_insight_service.py`
- Modify: `src/workers/deal_worker/services/deal_insight_services/stakeholder_analysis_service.py`
- Modify: `src/service/automation/runs/lifecycle_service.py`
- Add: `tests/service/automation/producers/test_signals.py`
- Add: `tests/service/automation/producers/test_agent.py`
- Update focused risk/blocker, stakeholder-insight, and run lifecycle tests.

- [ ] **Step 1: Write failing durable-transition tests**

Assert detected/changed/resolved only after inference/evidence/confidence/model identity is durable; unchanged writes emit nothing; run succeeded/failed/cancelled emits one event after terminal persistence; child events carry `caused_by_run_id`, correlation, and incremented depth; a triggering chain records visited trigger IDs.

```python
def test_signal_transition_requires_durable_inference_identity(signal):
    signal.model_name = None
    with pytest.raises(AutomationProducerError, match="model identity"):
        AISignalEventAdapter().detected(signal)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/producers/test_signals.py tests/service/automation/producers/test_agent.py tests/service/crm/test_risk_blocker_service_boundaries.py tests/workers/deal_worker/test_risk_blocker_extraction_service.py tests/api/routers/crm/test_stakeholder_insights_handler.py tests/workers/deal_worker/test_stakeholder_analysis_service.py tests/service/automation/runs/test_lifecycle_service.py -q`

Expected: adapters/events missing.

- [ ] **Step 3: Implement only facts supported by current durable rows**

Map risks/blockers to `ai.deal_risk.*`/`ai.deal_blocker.*` and stakeholder insights to supported buyer signal types. Include evidence references, confidence, model/prompt identity, status, and subject IDs; never include source text. Map terminal `AgentRun` rows to agent events after their terminal update. Leave approval and unsupported AI taxonomy entries unavailable unless caller tracing finds a durable request/grant/reject/expire row.

- [ ] **Step 4: Run producer/lifecycle tests**

Run: `uv run pytest tests/service/automation/producers tests/service/crm/test_risk_blocker_service_boundaries.py tests/workers/deal_worker/test_risk_blocker_extraction_service.py tests/api/routers/crm/test_stakeholder_insights_handler.py tests/workers/deal_worker/test_stakeholder_analysis_service.py tests/service/automation/runs/test_lifecycle_service.py -q`

Expected: all pass; unsupported catalog entries remain hidden.

---

## Phase 5: Direct manual, schedule, and webhook paths

### Task 13: Manual invocation with subject-isolated fanout

**Files:**

- Create: `src/service/automation/triggers/direct.py`
- Modify: `src/service/automation/triggers/service.py`
- Modify: `src/api/routers/automation/triggers_handler.py`
- Add: `tests/service/automation/triggers/test_direct.py`
- Add/update: `tests/api/routers/automation/test_manual_invocation.py`

- [ ] **Step 1: Write failing invocation tests**

Cover global/single/multi-select, tenant-validated subject IDs, cap, stable authenticated invocation ID, one envelope per subject, idempotent replay, parent correlation ID, stale config rejection, and no client-selected tenant/event ID.

```python
async def test_multi_subject_manual_invocation_fans_out(service, trigger, subjects):
    dispatches = await service.invoke(trigger.trigger_id, invocation_id="ui-123", subjects=subjects)
    assert [item.envelope.subject.id for item in dispatches] == [str(item) for item in subjects]
    assert len({item.envelope.event_id for item in dispatches}) == len(subjects)
    assert len({item.envelope.correlation_id for item in dispatches}) == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_direct.py tests/api/routers/automation/test_manual_invocation.py -q`

Expected: endpoint/service missing.

- [ ] **Step 3: Implement validated direct fanout**

Build `system.manual.invoked` envelopes with route trigger ID/current hash and deterministic keys `trigger:invocation:subject-or-global`. Resolve subject record snapshots through the registry resource adapter, cap fanout, enqueue all in the route transaction, then stage publish.

- [ ] **Step 4: Run manual tests**

Run: `uv run pytest tests/service/automation/triggers/test_direct.py tests/api/routers/automation/test_manual_invocation.py -q`

Expected: all pass.

### Task 14: Due schedule dispatcher and DST-correct recurrence

**Files:**

- Create: `src/service/automation/triggers/schedule.py`
- Modify: `src/service/automation/triggers/access.py`
- Modify: `src/workers/chat_worker/tasks/automation_event_tasks.py`
- Modify: `src/workers/chat_worker/config.py`
- Modify: `src/workers/chat_worker/celery_app.py`
- Add: `tests/service/automation/triggers/test_schedule.py`
- Update: `tests/workers/chat_worker/tasks/test_automation_event_tasks.py`
- Update: `tests/workers/chat_worker/test_chat_worker_config.py`

- [ ] **Step 1: Write failing schedule and worker-tier tests**

Cover one-time/daily/weekdays/weekly/biweekly/monthly and explicitly supported custom cadence, IANA timezone rejection, spring-forward/fall-back wall-clock behavior, row locks with `skip_locked`, exact UTC occurrence identity, subject fanout cap, next-occurrence update in the same transaction, all-org tier only fanning out, and per-org retries.

```python
def test_daily_schedule_preserves_local_wall_clock_across_dst():
    next_at = next_occurrence(
        config=daily_at("09:00", "America/New_York"),
        previous_utc=datetime(2026, 3, 7, 14, tzinfo=UTC),
    )
    assert next_at == datetime(2026, 3, 8, 13, tzinfo=UTC)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_schedule.py tests/workers/chat_worker/tasks/test_automation_event_tasks.py tests/workers/chat_worker/test_chat_worker_config.py -q`

Expected: schedule module/tasks missing.

- [ ] **Step 3: Implement the three-tier scheduler**

Beat calls `chat.dispatch_due_automation_schedules_all_orgs`; it reads approved org IDs and publishes per-org tasks without retries. Each per-org task locks a bounded due batch, resolves capped subjects, writes one `time.schedule.due` direct envelope per subject/occurrence, advances `next_trigger_at` locally then stores UTC, commits, and publishes dispatches. Deterministic idempotency is `trigger:occurrence-utc:subject-or-global:config-hash`.

- [ ] **Step 4: Run schedule/worker tests**

Run: `uv run pytest tests/service/automation/triggers/test_schedule.py tests/workers/chat_worker/tasks/test_automation_event_tasks.py tests/workers/chat_worker/test_chat_worker_config.py -q`

Expected: all pass.

### Task 15: Authenticated replay-safe webhooks

**Files:**

- Create: `src/service/automation/triggers/webhook.py`
- Modify: `src/service/automation/triggers/service.py`
- Modify: `src/api/routers/automation/triggers_handler.py`
- Add: `tests/service/automation/triggers/test_webhook.py`
- Add: `tests/api/routers/automation/test_webhook_ingress.py`

- [ ] **Step 1: Write failing security tests**

Cover unguessable endpoint token, server-derived tenant, HMAC signature over timestamp/raw body, constant-time compare, five-minute replay window, secret digest/rotation, old-secret grace window, request/schema/field/64 KiB limits, rate-limit hook, external dedupe key or canonical body hash, and stale trigger hash rejection.

```python
async def test_webhook_body_cannot_choose_tenant(client, signed_headers, endpoint):
    response = await client.post(
        endpoint,
        headers=signed_headers,
        json={"organisation_id": str(uuid4()), "risk": "high"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation/triggers/test_webhook.py tests/api/routers/automation/test_webhook_ingress.py -q`

Expected: webhook implementation/routes missing.

- [ ] **Step 3: Implement endpoint/secret lifecycle and direct dispatch**

Persist only endpoint token hash and secret hashes/rotation timestamps in trigger config. Return the plaintext secret once on create/rotate. Validate the trigger-owned JSON schema and map registered payload fields into `record`/`attributes`, then create `integration.webhook.received` with external actor and route trigger/current hash.

- [ ] **Step 4: Run webhook tests**

Run: `uv run pytest tests/service/automation/triggers/test_webhook.py tests/api/routers/automation/test_webhook_ingress.py -q`

Expected: all pass.

---

## Phase 6: Contract audit, recovery, and full verification

### Task 16: Recovery, observability, and schema-drift reconciliation

**Files:**

- Modify: `src/workers/chat_worker/tasks/automation_event_tasks.py`
- Modify: `src/service/automation/triggers/service.py`
- Modify: `src/service/crm/internal/custom_field_write_service.py`
- Update: `tests/service/crm/test_deal_schema_service.py`
- Add/update focused recovery, telemetry, and schema-drift tests.

- [ ] **Step 1: Write failing recovery/audit tests**

Assert pending dispatch recovery republishes without duplicating runs, activation records candidate/match/skip/pause/run/latency/correlation, custom-field deletion/type change pauses affected bindings, label-only change does not, and schedule/import caps expose stable skip/error codes.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/service/automation tests/workers/chat_worker/tasks/test_automation_event_tasks.py -q`

Expected: new reconciliation/recovery assertions fail.

- [ ] **Step 3: Implement the smallest reconciliation and telemetry path**

Reuse registry matcher validation against each active binding; update only affected bindings and persist a sanitized `last_error`. Keep recovery at-least-once and rely on dispatch/run uniqueness rather than distributed exactly-once claims.

- [ ] **Step 4: Run focused automation suite**

Run: `uv run pytest tests/service/automation tests/api/routers/automation tests/workers/chat_worker/services/test_automation_event_activation_service.py tests/workers/chat_worker/tasks/test_automation_event_tasks.py -q`

Expected: all pass.

### Task 17: Repeated format/output review and broad verification

**Files:**

- Review every file changed by Tasks 1–16.
- Modify only files needed to fix concrete findings.

- [ ] **Step 1: Audit every persisted and public format against the design**

Create a temporary review matrix (do not commit it) covering:

```text
Registry -> catalog -> create/update request -> normalized trigger row
Producer -> envelope -> dispatch row -> activation telemetry -> run snapshots
Run claim -> runtime envelope -> AgentContext -> tool list -> output/read audience
Manual/schedule/webhook ingress -> direct hash/idempotency -> generic activation
```

For each arrow, compare field names, types, null/missing semantics, tenant/principal source, secret/content exposure, size caps, and replay identity. Fix every mismatch and rerun the nearest test immediately.

- [ ] **Step 2: Scan for forbidden legacy/source branching and unsafe content**

Run:

```powershell
rg -n "source_domain|semantic_event_types.*enqueue|if .*event_key.*crm|trigger_type|AutomationScopeType\.OWNER" src/service/automation src/api/routers/automation src/workers/chat_worker
rg -n "email.*body|full_transcript|access_token|refresh_token|client_secret" src/service/automation
```

Expected: no legacy trigger contract or source-specific activation branch; sensitive values appear only in explicit rejection/reference code or tests.

- [ ] **Step 3: Run focused behavior suites twice after the final fix**

Run:

```powershell
uv run pytest tests/service/automation tests/api/routers/automation tests/workers/chat_worker/services/test_automation_event_activation_service.py tests/workers/chat_worker/services/test_agent_run_execution_service.py tests/workers/chat_worker/tasks/test_automation_event_tasks.py -q
uv run pytest tests/service/automation tests/api/routers/automation tests/workers/chat_worker/services/test_automation_event_activation_service.py tests/workers/chat_worker/services/test_agent_run_execution_service.py tests/workers/chat_worker/tasks/test_automation_event_tasks.py -q
```

Expected: both fresh runs pass with identical counts.

- [ ] **Step 4: Run model, migration, comment, type, lint, and full-suite checks**

Run:

```powershell
uv run pytest tests/test_agent_automation_schema.py tests/migrations -q
uv run python .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/service/automation src/domain/models/organisation_tables/agent_models.py src/workers/chat_worker/services/agent_run_execution_service.py
uv run mypy src/service/automation src/api/schemas/automation_trigger_models.py src/domain/models/organisation_tables/agent_models.py
uv run ruff check src/service/automation src/api/routers/automation src/api/schemas/automation_trigger_models.py src/domain/models/organisation_tables/agent_models.py src/workers/chat_worker
uv run pytest -q
```

Expected: every command exits 0. If the repository has unrelated pre-existing full-suite failures, prove them independently on `origin/main`; otherwise fix all failures caused by this branch.

- [ ] **Step 5: Review the final diff and storage migration one last time**

Run:

```powershell
git status --short
git diff --check
git diff --stat
git diff -- src/service/automation src/api/routers/automation src/api/schemas/automation_trigger_models.py src/domain/models/organisation_tables/agent_models.py migrations/org_migrations/versions/3c7f79637712_087_ai_native_automation.py
```

Expected: no whitespace errors; only requested implementation, plan/spec, and preserved unrelated untracked files are present.

---

## Self-review record

- Spec coverage: Every normative envelope field/invariant, base lifecycle rule, deal specific-value example, typed operator family, watch/filter limit, registry role, binding projection, principal/scope/audience combination, matching/direct behavior, idempotency identity, subject fanout, import/causation guard, schedule/webhook safeguard, producer availability rule, and walking-skeleton gate maps to a task above.
- Explicit non-goals: No compatibility adapter, data backfill, plugin framework, workflow engine, matcher-time LLM, event-stream replacement, or historical trigger-version subsystem is planned.
- Durable-fact gate: The full taxonomy is registered for stable naming, but only producer-backed definitions become catalog-visible. Unsupported conversation-message and approval facts remain unavailable rather than fabricated.
- Type consistency: `event_key`, `trigger_key`, `family`, `execution_principal_type/id`, `scope_config`, `audience_config`, `watch_config`, `filter_config`, `config_hash`, `event_id`, and `AutomationEventEnvelope` names are used consistently from API through storage/runtime.
- Placeholder scan: The plan contains no deferred implementation placeholders. “Current” service references are intentionally resolved by caller tracing immediately before each edit so the shared writer is changed once.
- Repository fit: The plan reuses the existing outbox delivery, tenant sessions/RLS, pinned agent snapshots, Celery recovery, CRM mutation ledger, email folder transaction, and meeting transcript/summary durability boundaries.
