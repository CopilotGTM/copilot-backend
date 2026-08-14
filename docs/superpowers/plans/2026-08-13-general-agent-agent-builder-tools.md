# GeneralAgent Agent Builder Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated user discover templates and existing template copies, create a private agent, and attach a personal trigger through GeneralAgent without creating a second agent or trigger platform.

**Architecture:** Add two web-only GeneralAgent tools: one read-only `inspect_agent_builder` tool and one mutating `change_agent_builder` tool. Each accepts a Pydantic discriminated request union, delegates to the existing owner-scoped `AgentDefinitionService` and `AutomationTriggerService`, and derives tenant, user, scope, audience, and principal identity from `AgentContext`. Add a progressively loaded `agent-builder` skill that drives the tool sequence and always reads the live capability/trigger catalogs instead of duplicating trigger definitions in prompt text.

**Tech Stack:** Python 3.11, Agno 2.5.9, Pydantic 2, FastAPI service contracts, SQLAlchemy/SQLModel, pytest

---

## 1. Decision summary

### Build this

- `inspect_agent_builder`
  - `overview`: page through the current user's agents, return the capability catalog, and annotate every code-owned template with non-archived copies owned by that same user.
  - `trigger_catalog`: return the live trigger catalog with personal-compatible definitions retained and chat support called out per family.
  - `trigger_options`: search tenant-owned labels/values with `principal_mode="personal"` fixed by code.
- `change_agent_builder`
  - `create_agent`: create one normal private user-authored agent.
  - `use_template`: materialise one exact template version; if the current user already has a copy, return `needs_confirmation` unless `confirm_duplicate=true`.
  - `attach_trigger`: attach one personal trigger to an owned agent; principal, scope, and audience are code-derived.
- `agent-builder` skill
  - routes create/build/template/trigger requests;
  - keeps dependent writes sequential;
  - loads live catalogs before selecting capability keys, trigger keys, option values, or filter paths;
  - explains partial completion honestly if agent creation succeeds and trigger creation fails.

### Do not build this in V1

- No new database tables or migrations.
- No new HTTP endpoints; tools call the same services as the existing `/users/v1/*` endpoints.
- No MCP server or second agent-management service.
- No template create/update/publish API, organisation-visible agents, organisation-service triggers, or admin controls.
- No agent update/archive, trigger update/archive/invoke, run-history, or output-delivery chat actions.
- No chat webhook creation. Webhook definitions remain visible as known capabilities, but creation stays in the Agents UI until one-time secret delivery has a non-loggable secure surface.
- No management tools or `agent-builder` skill inside user-configured automation agents; that would permit recursive control-plane mutation.

### Why this shape

- The repository already has the hard parts: owner-scoped definitions, exact-version template materialisation, catalog-driven triggers, trigger option search, optimistic concurrency, immutable run snapshots, and personal/organisation authority validation.
- OpenAI recommends obvious schemas, code-derived known arguments, invalid states made unrepresentable, and fewer initially available functions. Two discriminated tools add two choices rather than six separate functions to the current GeneralAgent surface. See [OpenAI function-calling guidance](https://developers.openai.com/api/docs/guides/function-calling).
- Agno derives tool schemas from typed function signatures and supports explicit confirmation/user-input controls; this plan uses typed request unions now and preserves a clean seam for platform confirmation later. See [Agno tool guidance](https://docs.agno.com/tools/overview).
- OWASP recommends separate tool sets by trust level, least privilege, server-side authorization, structured validation, and human oversight for persistent/high-impact actions. See [OWASP AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html).
- OpenAI recommends human intervention for sensitive or high-risk actions. Here, a direct user request authorizes private-agent creation; duplicate template copies, ambiguous targets, and active automation require an explicit conversational confirmation. See [OpenAI's practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/).

## 2. Existing contracts to reuse

| Need | Existing source of truth |
| --- | --- |
| Create/list/get private agents | `src/service/agents/definition_service.py` |
| Enforce tenant and owner predicates | `src/service/agents/access.py` |
| Validate tool/skill keys and JSON Schema | `src/service/agents/validator.py` |
| List selectable tools and skills | `src/service/agents/capability_catalog.py` |
| List/materialise exact template versions | `src/service/agents/template_metadata.py`, `AgentDefinitionService.use_template()` |
| Build live trigger definitions | `AutomationTriggerService.get_catalog()` |
| Search tenant-owned trigger options | `AutomationTriggerService.search_trigger_options()` |
| Validate and persist a trigger | `AutomationTriggerService.create_trigger()` |
| Personal trigger authority | `principal_mode=personal`, scope `owned_or_created_by_principal`, audience `principal` |
| GeneralAgent security context | `src/agents/context.py` |
| GeneralAgent tool registration | `src/agents/tools/registry.py` |
| Progressive prompt workflow | `src/agents/skills/*` and `LocalSkills` in `src/agents/workflows/general/agent.py` |

## 3. Request and result contracts

The two tool functions each take one `request` object. All request models use `ConfigDict(extra="forbid")`; their `action` field is the discriminator.

```python
AgentBuilderInspectRequest = Annotated[
    AgentBuilderOverviewRequest
    | AgentBuilderTriggerCatalogRequest
    | AgentBuilderTriggerOptionsRequest,
    Field(discriminator="action"),
]

AgentBuilderChangeRequest = Annotated[
    AgentBuilderCreateRequest
    | AgentBuilderUseTemplateRequest
    | AgentBuilderAttachTriggerRequest,
    Field(discriminator="action"),
]
```

### Read actions

```python
class AgentBuilderOverviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["overview"]
    status: Literal["active", "paused"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class AgentBuilderTriggerCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["trigger_catalog"]


class AgentBuilderTriggerOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["trigger_options"]
    trigger_key: str = Field(min_length=1, max_length=160)
    control: Literal["source_selector", "filter", "direct_subject"]
    field_path: str | None = Field(default=None, max_length=200)
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=25, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=32)
```

Do not expose `audience` option search: chat V1 is personal-only, so audience is always the authenticated principal.

### Write actions

```python
class AgentBuilderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create_agent"]
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    prompt: str = Field(min_length=1, max_length=50_000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=128)
    allowed_skills: list[str] = Field(default_factory=list, max_length=64)
    max_turns: int | None = Field(default=None, ge=1, le=100)
    input_schema: dict[str, Any] | None = None
    output_mode: Literal["text", "json"] = "text"
    output_schema: dict[str, Any] | None = None


class AgentBuilderUseTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["use_template"]
    template_id: UUID
    version: int = Field(ge=1)
    confirm_duplicate: bool = False


class AgentBuilderAttachTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["attach_trigger"]
    agent_id: UUID
    name: str = Field(min_length=1, max_length=120)
    trigger_key: str = Field(min_length=3, max_length=160)
    status: Literal["active", "paused"]
    config: dict[str, Any] = Field(default_factory=dict, max_length=64)
    watch: dict[str, Any] | None = None
    filter: dict[str, Any] | None = Field(default=None, max_length=64)
    goal: str | None = Field(default=None, max_length=4_000)
```

Do not accept `organisation_id`, `user_id`, `principal_mode`, execution-principal IDs, scope, audience, template provenance, visibility, source type, revision, or agent type from the model.

Every tool result is a JSON-serializable dictionary with one of these statuses:

```text
ok | created | needs_input | needs_confirmation | unsupported | conflict | error
```

Successful mutations return readable names plus the created `agent_id` or `trigger_id` for follow-up continuity. Errors return a sanitized message and never return SQL, stack traces, tenant IDs, user IDs, webhook secrets, or raw exception representations.

## 4. Implementation tasks

### Task 1: Prove the two discriminated tool schemas

**Files:**
- Create: `src/agents/tools/agent_builder.py`
- Create: `tests/agents/tools/test_agent_builder.py`

- [ ] **Step 1: Write failing schema-contract tests**

Add tests that define a web `AgentContext`, call the two factories, convert each callable with `agno.tools.function.Function.from_callable(..., strict=True)`, and assert:

```python
def test_agent_builder_exposes_two_strict_discriminated_tools() -> None:
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="web")

    inspect_tool = create_inspect_agent_builder(context, lambda: None)
    change_tool = create_change_agent_builder(context, lambda: None)

    inspect_schema = Function.from_callable(inspect_tool, strict=True).parameters
    change_schema = Function.from_callable(change_tool, strict=True).parameters

    assert inspect_tool.__name__ == "inspect_agent_builder"
    assert change_tool.__name__ == "change_agent_builder"
    assert inspect_schema["additionalProperties"] is False
    assert change_schema["additionalProperties"] is False
    assert len(inspect_schema["properties"]["request"]["anyOf"]) == 3
    assert len(change_schema["properties"]["request"]["anyOf"]) == 3
    assert all(
        branch["additionalProperties"] is False
        for branch in inspect_schema["properties"]["request"]["anyOf"]
    )
    assert all(
        branch["additionalProperties"] is False
        for branch in change_schema["properties"]["request"]["anyOf"]
    )
```

Also assert none of the forbidden identity/authority field names occur in the serialized write schema.

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/tools/test_agent_builder.py -q
```

Expected: collection/import failure because `src.agents.tools.agent_builder` does not exist.

- [ ] **Step 3: Add only the request models and two factories**

In `src/agents/tools/agent_builder.py`, add the models above, the two annotated union aliases, and factory-returned async functions with exact names. Initially return a structured `unsupported` result from each action branch so the schema tests pass without persistence behavior.

- [ ] **Step 4: Run the schema tests and verify GREEN**

Run the same command. Expected: all schema-contract tests pass.

- [ ] **Step 5: Commit the contract slice**

```powershell
git add src/agents/tools/agent_builder.py tests/agents/tools/test_agent_builder.py
git commit -m "test(agents): define agent builder tool contracts"
```

### Task 2: Add one owner-scoped template-copy query

**Files:**
- Modify: `src/service/agents/access.py`
- Test: `tests/service/agents/test_agent_definition_access.py`

- [ ] **Step 1: Write the failing SQL-boundary test**

Add a test for `list_template_copies(template_ids)` that compiles the generated statement and asserts all of the following:

```python
@pytest.mark.asyncio
async def test_list_template_copies_is_owner_scoped_and_hides_archived_agents() -> None:
    session = _RecordingSession()
    organisation_id = uuid4()
    owner_user_id = uuid4()
    template_ids = {uuid4(), uuid4()}
    access = AgentDefinitionAccess(session, organisation_id, owner_user_id)  # type: ignore[arg-type]

    assert await access.list_template_copies(template_ids) == []

    sql = _sql(session.statements[0])
    _assert_owner_boundary(sql)
    assert "user_agents.source_type" in sql
    assert "user_agents.source_agent_id in" in sql
    assert "user_agents.status !=" in sql
    assert "order by user_agents.updated_at desc" in sql
```

- [ ] **Step 2: Run the focused access test and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/service/agents/test_agent_definition_access.py -q
```

Expected: `AttributeError` for `list_template_copies`.

- [ ] **Step 3: Implement the smallest query**

Add this method to `AgentDefinitionAccess`:

```python
async def list_template_copies(self, template_ids: set[UUID]) -> list[UserAgent]:
    """Return this owner's live agents copied from the requested system templates."""
    if not template_ids:
        return []
    columns = cast(Any, UserAgent).__table__.c
    statement = (
        select(UserAgent)
        .where(
            *self._owned_conditions(),
            columns.status != AgentStatus.ARCHIVED.value,
            columns.source_type == "system",
            columns.source_agent_id.in_(template_ids),
        )
        .order_by(columns.updated_at.desc(), columns.agent_id.desc())
    )
    return list((await self.session.execute(statement)).scalars().all())
```

This is the only new persistence method. Do not add a template-usage table, counter column, or N queries per template.

- [ ] **Step 4: Run the access tests and verify GREEN**

Run the same command. Expected: all access tests pass.

- [ ] **Step 5: Commit the query**

```powershell
git add src/service/agents/access.py tests/service/agents/test_agent_definition_access.py
git commit -m "feat(agents): query owned template copies"
```

### Task 3: Implement `inspect_agent_builder`

**Files:**
- Modify: `src/agents/tools/agent_builder.py`
- Test: `tests/agents/tools/test_agent_builder.py`

- [ ] **Step 1: Write failing read-action tests**

Cover these exact behaviors with mocked services/access objects:

1. Missing `context.user_id` returns `{"status": "error", "message": "Agent management requires an authenticated user."}` without opening a session.
2. `overview` calls `AgentDefinitionService.list_agents()` with the requested page, calls `list_template_copies()` once for all current template IDs, and returns:
   - owner-paged agents;
   - every template summary;
   - `existing_agents` containing only the current user's non-archived copies;
   - current tools with `automation_compatible` derived from `RETRY_UNSAFE_AUTOMATION_TOOLS`;
   - current user-configurable skills.
3. `trigger_catalog` retains definitions whose `principal_modes` contain `personal`, removes empty resources/categories, and adds `chat_supported=false` plus an explanation for `family="webhook"` while keeping webhook knowledge visible.
4. `trigger_options` constructs `AutomationTriggerOptionSearchRequest` with `principal_mode="personal"`; no request action can select organisation authority or audience users.
5. Service errors become structured `error`/`conflict` results without raw exception text.

- [ ] **Step 2: Run the tool tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/tools/test_agent_builder.py -q
```

Expected: the read-action assertions fail against the temporary `unsupported` implementation.

- [ ] **Step 3: Implement the read dispatcher**

Use one fresh organisation session inside the tool call:

```python
async with context.acquire_session() as session:
    definition_service = AgentDefinitionService(
        session,
        context.organisation_id,
        context.user_id,
    )
    trigger_service = AutomationTriggerService(
        session,
        context.organisation_id,
        context.user_id,
        is_admin=False,
    )
```

Do not call either service's `close()` method; `context.acquire_session()` owns session lifetime. Do not hold this session outside the tool call.

For `overview`, build template usage with one `list_template_copies()` query and group rows by `source_agent_id`. Return template copy fields limited to `agent_id`, `name`, `status`, `revision`, and `source_version`.

For `trigger_catalog`, transform a `model_dump(mode="json")` result; do not reimplement or enumerate registry trigger definitions. Preserve catalog limits, defaults, config fields, watch fields, filter fields, source selectors, and option sets.

For `trigger_options`, reuse `AutomationTriggerService.search_trigger_options()` and its existing tenant/authority validation.

- [ ] **Step 4: Run the read-action tests and verify GREEN**

Run the same focused command. Expected: all read tests pass.

- [ ] **Step 5: Commit the read tool**

```powershell
git add src/agents/tools/agent_builder.py tests/agents/tools/test_agent_builder.py
git commit -m "feat(agents): expose agent builder inspection tool"
```

### Task 4: Implement `change_agent_builder`

**Files:**
- Modify: `src/agents/tools/agent_builder.py`
- Test: `tests/agents/tools/test_agent_builder.py`

- [ ] **Step 1: Write failing mutation tests**

Cover these exact cases:

1. `create_agent` maps only editable fields into `AgentCreateRequest`, leaves model/provenance/visibility defaults server-owned, commits once, and returns `created` with `agent_id`, name, source type, revision, allowed tools/skills, and automation eligibility.
2. Invalid tool/skill keys and invalid JSON schemas return the existing sanitized service message and roll back once.
3. `use_template` checks all current-user copies of the requested template:
   - copies present plus `confirm_duplicate=false` returns `needs_confirmation` and does not call `use_template()`;
   - `confirm_duplicate=true` calls `use_template(template_id, version=exact_version)` and commits;
   - no silently substituted template version is allowed.
4. `attach_trigger` constructs this fixed authority block in code:

```python
AutomationTriggerCreateRequest(
    name=request.name,
    trigger_key=request.trigger_key,
    status=request.status,
    config=request.config,
    watch=request.watch,
    filter=request.filter,
    principal_mode="personal",
    scope={"type": "owned_or_created_by_principal", "config": {}},
    audience={"mode": "principal", "user_ids": []},
    task={"goal": request.goal},
)
```

5. An active trigger on a paused agent, incompatible capability, duplicate trigger, invalid option/filter, or foreign agent returns the exact service error class as a sanitized `conflict`, `needs_input`, or `error` result and rolls back.
6. A webhook trigger returns `unsupported` before credential provisioning and tells the caller to use the Agents UI; no secret enters tool results or transparent SSE summaries.
7. A successful mutation commits before returning; an unsuccessful mutation never commits.

- [ ] **Step 2: Run the mutation tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/tools/test_agent_builder.py -q
```

Expected: mutation assertions fail against the temporary implementation.

- [ ] **Step 3: Implement mutation dispatch with existing services**

Use `execute_with_capabilities()` so existing chat tool-progress frames remain consistent. Inside the actual mutation callable:

```python
async with context.acquire_session() as session:
    try:
        result = await _dispatch_agent_builder_change(session, context, request)
        if result["status"] == "created":
            await session.commit()
        else:
            await session.rollback()
        return result
    except (AgentServiceError, AutomationServiceError) as exc:
        await session.rollback()
        return _service_error_result(exc)
    except Exception:
        await session.rollback()
        logger.exception("Agent builder mutation failed action=%s", request.action)
        return {"status": "error", "message": "Agent management failed unexpectedly."}
```

Map `_service_error_result()` by concrete error type to `conflict`, `needs_input`, or `error`; never expose the exception representation. Log action and stable resource IDs only; do not log prompts, model settings, webhook credentials, or full filter/config payloads.

Do not make agent creation and trigger creation one transaction. The existing product model treats an agent without a trigger as valid, and the skill will report a created agent separately if trigger attachment later fails.

- [ ] **Step 4: Run the mutation tests and verify GREEN**

Run the same command. Expected: all schema, read, and mutation tests pass.

- [ ] **Step 5: Commit the mutation tool**

```powershell
git add src/agents/tools/agent_builder.py tests/agents/tools/test_agent_builder.py
git commit -m "feat(agents): create agents and personal triggers from chat"
```

### Task 5: Register the tools only on human web GeneralAgent

**Files:**
- Modify: `src/agents/tools/registry.py`
- Test: `tests/service/agents/test_channel_tool_registry.py`
- Test: `tests/service/agents/test_capability_catalog.py`
- Test: `tests/service/agents/test_agent_definition_validator.py`

- [ ] **Step 1: Write failing registry and isolation tests**

Assert all of the following:

```python
assert {
    ToolName.INSPECT_AGENT_BUILDER,
    ToolName.CHANGE_AGENT_BUILDER,
} <= AGENT_TOOLS["GeneralAgent"]
assert ToolName.CHANGE_AGENT_BUILDER in MUTATING_TOOLS
assert CHANNEL_ONLY_TOOLS[ToolName.INSPECT_AGENT_BUILDER] == {"web"}
assert CHANNEL_ONLY_TOOLS[ToolName.CHANGE_AGENT_BUILDER] == {"web"}
assert ToolName.INSPECT_AGENT_BUILDER not in GENERAL_AGENT_TOOLS
assert ToolName.CHANGE_AGENT_BUILDER not in GENERAL_AGENT_TOOLS
assert ToolName.INSPECT_AGENT_BUILDER not in AGENT_TOOLS["VoiceRealtimeAgent"]
assert ToolName.CHANGE_AGENT_BUILDER not in AGENT_TOOLS["VoiceRealtimeAgent"]
```

Also assert the capability catalog does not return either management key and `AgentDefinitionValidator` rejects either key for a user-configured agent.

- [ ] **Step 2: Run the focused registry tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/service/agents/test_channel_tool_registry.py tests/service/agents/test_capability_catalog.py tests/service/agents/test_agent_definition_validator.py -q
```

Expected: missing enum/registry entries.

- [ ] **Step 3: Add the isolated registry entries**

Add `ToolCategory.AGENTS`, the two `ToolName` values, and:

```python
GENERAL_AGENT_MANAGEMENT_TOOLS = {
    ToolName.INSPECT_AGENT_BUILDER,
    ToolName.CHANGE_AGENT_BUILDER,
}

AGENT_TOOLS = {
    "GeneralAgent": set(GENERAL_AGENT_TOOLS | GENERAL_AGENT_MANAGEMENT_TOOLS),
    "VoiceRealtimeAgent": set(GENERAL_AGENT_TOOLS) - VOICE_REALTIME_WRITE_EXCLUDED_TOOLS,
    # existing agents unchanged
}
```

Register both factories in `ToolRegistry._register_tool_factories()`, map both to `ToolCategory.AGENTS`, gate both to `{"web"}`, and add only `CHANGE_AGENT_BUILDER` to `MUTATING_TOOLS`.

Do not add either management key to `GENERAL_AGENT_TOOLS`. That set remains the selectable capability boundary for `UserConfiguredAgent` and the shared voice profile.

- [ ] **Step 4: Run the registry tests and verify GREEN**

Run the same command. Expected: all registry/isolation tests pass.

- [ ] **Step 5: Commit the registry isolation**

```powershell
git add src/agents/tools/registry.py tests/service/agents/test_channel_tool_registry.py tests/service/agents/test_capability_catalog.py tests/service/agents/test_agent_definition_validator.py
git commit -m "feat(agents): register web-only builder tools"
```

### Task 6: Add the progressively loaded `agent-builder` skill

**Files:**
- Create: `src/agents/skills/agent-builder/SKILL.md`
- Create: `src/agents/skills/agent-builder/references/trigger-building.md`
- Modify: `src/service/agents/capability_catalog.py`
- Modify: `src/agents/skills/README.md`
- Test: `tests/agents/test_skills_loading.py`
- Test: `tests/service/agents/test_capability_catalog.py`

- [ ] **Step 1: Write failing skill-loading and exclusion tests**

Add tests that assert:

```python
loader = LocalSkills(str(SKILLS_DIR))
skills = loader.load()
skill = next(item for item in skills if item.name == "agent-builder")

assert skill.description
assert len(skill.description) <= 1024
assert "inspect_agent_builder" in skill.instructions
assert "change_agent_builder" in skill.instructions
assert "trigger-building.md" in skill.references
assert "agent-builder" not in discover_skill_keys()
assert "agent-builder" not in {
    item.key for item in build_agent_capability_catalog().skills
}
```

The last two assertions are deliberate: GeneralAgent loads the folder directly, while user-configured agents must not select this control-plane skill.

- [ ] **Step 2: Run the focused skill tests and verify RED**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/test_skills_loading.py tests/service/agents/test_capability_catalog.py -q
```

Expected: `agent-builder` is absent.

- [ ] **Step 3: Add the skill workflow**

Use this content structure in `SKILL.md`:

```markdown
---
name: agent-builder
description: Build or configure CopilotGTM agents from chat. Use when a user asks to create an agent, use an agent template, automate an agent, attach a trigger, schedule an agent, or asks whether they already used a template.
---

# Agent Builder

Build private user agents from the live backend catalogs. Never invent a tool key, skill key,
template version, trigger key, option value, or filter path.

## Use this workflow

1. Inspect before changing.
- Call `inspect_agent_builder` with `action="overview"`.
- If a template matches, tell the user about every existing copy before creating another.
- For a custom agent, select only the minimum capabilities required for its stated purpose.
- If the user wants a trigger, exclude mutation tools because retry-unsafe agents cannot run automatically.

2. Create the agent only from explicit intent.
- Use `change_agent_builder` with `create_agent` for a custom definition.
- Use `use_template` with the exact listed version for a template.
- If the tool returns `needs_confirmation`, show the existing agent names and ask whether to create another copy. Do not set `confirm_duplicate=true` without that confirmation.

3. Configure a trigger from live metadata.
- Call `inspect_agent_builder` with `trigger_catalog`; never hard-code supported triggers.
- Use `trigger_options` to resolve human names to catalog values when an option source is present.
- Ask one focused question for any required config, ambiguous target, active/paused choice, or filter value that cannot be inferred safely.
- Call `change_agent_builder` with `attach_trigger` only after the target agent and trigger decisions are unambiguous.
- Personal authority is fixed by the tool. Do not request organisation execution, audiences, or internal principal IDs.

4. Report the persisted result.
- Separate Created, Attached, Blocked, and Needs confirmation outcomes.
- If agent creation succeeds but trigger attachment fails, keep and report the valid agent; do not claim rollback or delete it.
- Never claim a trigger fired merely because it was attached.

## Output contract

State the readable agent/template/trigger names, active or paused status, relevant created ID,
and the next action. Do not expose unrelated internal IDs. Do not paste raw tool JSON.

## Reference loading guide

- Use `references/trigger-building.md` for trigger-family behavior, catalog fields, safe activation,
  and unsupported chat operations.
```

- [ ] **Step 4: Add the trigger reference**

`trigger-building.md` must cover:

- event, schedule, manual, and webhook family semantics;
- `config`, `watch`, `filter`, and `task.goal` roles;
- live-catalog/default usage and option-search rules;
- personal scope/audience derived by code;
- active vs paused confirmation;
- no claim of firing/run success after attachment;
- webhook creation deferred because credentials are one-time secrets;
- organisation/publish/admin controls deferred until explicit policy and UI exist.

Do not copy the current registry's trigger list into the reference. The live catalog remains authoritative as new triggers are added.

- [ ] **Step 5: Keep the skill out of user-configurable catalogs**

In `src/service/agents/capability_catalog.py`, add:

```python
GENERAL_AGENT_ONLY_SKILL_KEYS = frozenset({"agent-builder"})
```

and exclude those names inside `discover_skill_keys()`. GeneralAgent's `LocalSkills` scan remains unchanged, so the skill is available to chat but cannot be pinned into a triggered user agent.

- [ ] **Step 6: Update the skills README and run tests**

Add `agent-builder` to the Current Skills table with `GeneralAgent-only control-plane workflow` in the Replaces column.

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/test_skills_loading.py tests/service/agents/test_capability_catalog.py -q
```

Expected: all skill-loading and capability-exclusion tests pass.

- [ ] **Step 7: Commit the skill**

```powershell
git add src/agents/skills/agent-builder src/agents/skills/README.md src/service/agents/capability_catalog.py tests/agents/test_skills_loading.py tests/service/agents/test_capability_catalog.py
git commit -m "feat(agents): teach GeneralAgent the builder workflow"
```

### Task 7: Add end-to-end service/tool regression coverage

**Files:**
- Modify: `tests/agents/tools/test_agent_builder.py`
- Modify: `tests/service/agents/test_channel_tool_registry.py`
- Modify: `tests/service/agents/test_agent_definition_access.py`

- [ ] **Step 1: Add the complete happy-path regression**

Using fake sessions and real Pydantic request objects, exercise this sequence:

```text
overview -> use_template -> trigger_catalog -> trigger_options -> attach_trigger
```

Assert:

- exact template version is passed;
- the created agent ID becomes the trigger target;
- writes run sequentially;
- two separate successful transactions commit;
- trigger request is personal/principal scoped;
- final result contains the created agent and trigger IDs;
- no user/tenant/principal identity appears in either model-authored schema.

- [ ] **Step 2: Add abuse and failure regressions**

Cover:

- foreign/archived agent target is indistinguishable from not found;
- organisation principal/audience fields are rejected as extra model input;
- a duplicate template copy needs confirmation;
- webhook creation is rejected before secret generation;
- retry-unsafe tools make attachment fail without changing the created agent;
- invalid trigger/filter/option keys are rejected by the existing trigger validator;
- management tools are absent on voice, Teams, Slack, and WhatsApp;
- management tools and skill cannot be pinned to a `UserConfiguredAgent`.

- [ ] **Step 3: Run focused verification**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/tools/test_agent_builder.py tests/agents/test_skills_loading.py tests/service/agents/test_agent_definition_access.py tests/service/agents/test_agent_definition_validator.py tests/service/agents/test_capability_catalog.py tests/service/agents/test_channel_tool_registry.py -q
```

Expected: all focused tests pass.

- [ ] **Step 4: Run agent/automation contract verification**

```powershell
.venv\Scripts\python.exe -m pytest tests/service/agents tests/service/automation/triggers tests/agents/test_user_configured_agent.py tests/agents/test_general_agent_skills.py -q
```

Expected: all tests pass. If the run is nondeterministic in parallel, rerun the exact command serially before diagnosing a code failure.

- [ ] **Step 5: Run static checks on the touched slice**

```powershell
.venv\Scripts\python.exe -m ruff check src/agents/tools/agent_builder.py src/agents/tools/registry.py src/service/agents/access.py src/service/agents/capability_catalog.py tests/agents/tools/test_agent_builder.py tests/agents/test_skills_loading.py tests/service/agents/test_agent_definition_access.py tests/service/agents/test_agent_definition_validator.py tests/service/agents/test_capability_catalog.py tests/service/agents/test_channel_tool_registry.py
.venv\Scripts\python.exe -m ruff format --check src/agents/tools/agent_builder.py src/agents/tools/registry.py src/service/agents/access.py src/service/agents/capability_catalog.py tests/agents/tools/test_agent_builder.py tests/agents/test_skills_loading.py tests/service/agents/test_agent_definition_access.py tests/service/agents/test_agent_definition_validator.py tests/service/agents/test_capability_catalog.py tests/service/agents/test_channel_tool_registry.py
git diff --check
```

Expected: all checks pass with no whitespace errors.

- [ ] **Step 6: Commit the regression slice**

```powershell
git add tests/agents/tools/test_agent_builder.py tests/service/agents/test_channel_tool_registry.py tests/service/agents/test_agent_definition_access.py
git commit -m "test(agents): cover GeneralAgent builder workflow"
```

## 5. Walking skeleton

Implement and demo this vertical slice first:

1. User: "Use the Deal Stage Health Review template and run it whenever one of my deals changes stage."
2. `inspect_agent_builder(overview)` returns the template and whether the same user already has a copy.
3. If no copy exists, `change_agent_builder(use_template)` creates one exact-version private copy.
4. `inspect_agent_builder(trigger_catalog)` returns `crm.deal.stage_changed` as a personal-compatible event trigger.
5. `change_agent_builder(attach_trigger)` creates an active personal binding using server-derived identity/scope/audience.
6. Final response reports the created agent, attached trigger, active status, and clarifies that no run has fired yet.

This slice touches the existing template, definition, registry, trigger-validation, persistence, and GeneralAgent skill boundaries without adding an alternate platform.

## 6. Acceptance audit

| Requirement | Proof |
| --- | --- |
| Create a custom agent from GeneralAgent | `change_agent_builder(create_agent)` test and created `AgentDetail` |
| Use an existing template | exact-version `use_template` service call test |
| Tell user a template is already used | one owner-scoped copy query plus `overview.templates[].existing_agents` test |
| Attach a trigger | personal `AutomationTriggerCreateRequest` and persisted `AutomationTriggerDetail` test |
| Know current triggers | live filtered `AutomationTriggerCatalog`; no hard-coded trigger-key list in skill/tool |
| Resolve business labels, not ask for IDs | `trigger_options` delegates to tenant-scoped option search |
| User-level isolation | no identity arguments; owner/tenant SQL predicates; personal principal/scope/audience fixed in code |
| Production-safe writes | validation, duplicate confirmation, one commit/rollback per action, sanitized errors, no secret output |
| Do not empower automated agents recursively | management tools outside `GENERAL_AGENT_TOOLS`; skill excluded from user capability catalog |
| Future publish/admin controls remain possible | existing visibility/provenance/organisation-principal model untouched; V1 exposes no premature publish API |
| No duplicate architecture | no migration, dependency, endpoint, MCP server, or second service |

## 7. Deferred follow-ups with explicit triggers

Revisit only when the named need is real:

- **Organisation/publish controls:** add after product policy defines publishers, reviewers, visibility, revocation, template governance, and audit requirements.
- **Webhook creation in chat:** add after one-time credentials can be delivered outside model context, logs, and SSE result summaries.
- **Agent editing and trigger lifecycle:** add when users request update/pause/archive/invoke flows from chat; reuse existing optimistic-concurrency tokens rather than adding blind writes.
- **Native deferred tool loading:** the current GeneralAgent exposes 68 functions before this change. Measure tool-selection accuracy and schema-token cost after this feature; if it regresses or the surface keeps growing, adopt provider/framework-supported tool search as a separate cross-cutting change instead of hiding this feature behind an ad hoc generic dispatcher.
