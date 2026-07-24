# Dedicated User-Configured Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give triggered user-owned agents a dedicated `UserConfiguredAgent` class and independent runtime configuration without reusing the interactive `GeneralAgent` or duplicating the durable worker stack.

**Architecture:** Keep Celery delivery, `AgentRunExecutionService`, claim fencing, immutable snapshots, lifecycle persistence, `AgentContext`, and the vetted tool registry unchanged. Extract Agno model/tool/skill assembly from `AgentRunExecutor` into a dedicated non-streaming `UserConfiguredAgent`; the executor continues validating run boundaries, creating the event message, normalizing output, and recording usage.

**Tech Stack:** Python 3.12, Agno, Azure OpenAI, Celery, SQLAlchemy/SQLModel, pytest

---

## Scope decisions

- The triggered-agent path already constructs Agno directly and never instantiates `src.agents.workflows.general.agent.GeneralAgent`.
- Separate means a distinct runtime class, prompt, selected capabilities, model settings, and operational defaults. It does not mean another queue, worker, database lifecycle, or copy of the tools.
- `UserConfiguredAgent` is non-interactive: one immutable trigger snapshot enters and one final text/JSON result leaves. Streaming frames, attachments, conversation history, and the General Agent prompt stay outside this runtime.
- Keep persisted `agent_type='general'` unchanged in this slice. Renaming existing rows is an API/data compatibility project and adds no execution control.
- Continue using `ToolRegistry` and local skills, but expose only the subset pinned into the run snapshot.
- No schema, migration, endpoint, template, trigger, frontend, or deployment change belongs in this plan.

## File map

- Create `src/agents/workflows/user_configured/__init__.py`: dedicated runtime package.
- Create `src/agents/workflows/user_configured/agent.py`: build and invoke one snapshot-configured Agno agent.
- Create `tests/agents/test_user_configured_agent.py`: prove prompt/model/tool/skill isolation and fail-closed selection.
- Modify `src/infra/config/agent_config.py`: add independent `user_configured_agent` defaults.
- Create `tests/infra/config/test_agent_config.py`: lock the new configuration key.
- Modify `src/service/automation/runs/runtime_executor.py`: delegate execution while retaining durable-run responsibilities.
- Modify `tests/service/automation/runs/test_runtime_executor.py`: verify delegation and existing security/output contracts.

### Task 1: Add independent runtime configuration

**Files:**

- Create: `tests/infra/config/test_agent_config.py`
- Modify: `src/infra/config/agent_config.py`

- [ ] **Step 1: Write the failing test**

~~~python
from src.infra.config.agent_config import AgentConfig


def test_user_configured_agent_has_a_dedicated_config_key() -> None:
    config = AgentConfig.get_config('user_configured_agent')

    assert 'user_configured_agent' in AgentConfig.list_agents()
    assert config.model == AgentConfig.USER_CONFIGURED_MODEL
    assert config.debug_mode == AgentConfig.USER_CONFIGURED_DEBUG
    assert config.timeout_seconds == AgentConfig.USER_CONFIGURED_TIMEOUT
~~~

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/infra/config/test_agent_config.py -q`.

Expected: FAIL with `KeyError: Unknown agent: user_configured_agent`.

- [ ] **Step 3: Add the minimum configuration**

Add beside the existing General Agent settings:

~~~python
USER_CONFIGURED_MODEL: str = os.getenv(
    'AGENT_USER_CONFIGURED_MODEL',
    GENERAL_MODEL,
)
USER_CONFIGURED_DEBUG: bool = (
    os.getenv('AGENT_USER_CONFIGURED_DEBUG', 'false').lower() == 'true'
)
USER_CONFIGURED_TIMEOUT: int = int(
    os.getenv('AGENT_USER_CONFIGURED_TIMEOUT', '600')
)
~~~

Add to `_CONFIG_MAP`:

~~~python
'user_configured_agent': (
    'USER_CONFIGURED_MODEL',
    'USER_CONFIGURED_DEBUG',
    'USER_CONFIGURED_TIMEOUT',
),
~~~

The default model intentionally matches the current deployment to avoid rollout drift, while the new environment keys allow independent operation.

- [ ] **Step 4: Verify GREEN**

Run `uv run pytest tests/infra/config/test_agent_config.py -q`.

Expected: `1 passed`.

### Task 2: Introduce the dedicated runtime class

**Files:**

- Create: `src/agents/workflows/user_configured/__init__.py`
- Create: `src/agents/workflows/user_configured/agent.py`
- Create: `tests/agents/test_user_configured_agent.py`

- [ ] **Step 1: Write failing assembly tests**

Use injected fake Agno/model/registry factories and cover both cases:

~~~python
class FakeResponse:
    content = 'Call the buyer'


class FakeAgent:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.inputs: list[str] = []

    async def arun(self, *, input: str):  # noqa: A002 - mirrors Agno
        self.inputs.append(input)
        return FakeResponse()


class FakeRegistry:
    def __init__(self) -> None:
        self.requested = []

    def get_tools(self, tool_names):
        self.requested = tool_names
        return [lambda: None for _ in tool_names]


class EmptyRegistry:
    def get_tools(self, _tool_names):
        return []


@pytest.mark.asyncio
async def test_agent_uses_only_pinned_configuration(tmp_path) -> None:
    skill_dir = tmp_path / 'pipeline-review'
    skill_dir.mkdir()
    (skill_dir / 'SKILL.md').write_text(
        '---\nname: pipeline-review\ndescription: Review pipeline\n---\nUse CRM evidence.',
        encoding='utf-8',
    )
    registry = FakeRegistry()
    created = []
    runtime = UserConfiguredAgent(
        context=agent_context(),
        prompt='Act as a deal coach.',
        model_name='gpt-5-mini',
        model_settings={'temperature': 0.2},
        allowed_tools=('search_deals',),
        allowed_skills=('pipeline-review',),
        max_turns=6,
        output_mode='text',
        output_schema=None,
        skills_dir=tmp_path,
        agent_factory=lambda **kwargs: created.append(FakeAgent(**kwargs)) or created[-1],
        model_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        tool_registry_factory=lambda _context: registry,
    )

    response = await runtime.run('{"event": "deal.stage_changed"}')

    assert response.content == 'Call the buyer'
    assert [tool.value for tool in registry.requested] == ['search_deals']
    assert created[0].kwargs['description'] == 'Act as a deal coach.'
    assert created[0].kwargs['model'].id == 'gpt-5-mini'
    assert created[0].kwargs['skills'].get_skill_names() == ['pipeline-review']


def test_agent_rejects_a_missing_pinned_tool(tmp_path) -> None:
    with pytest.raises(ValueError, match='not registered'):
        UserConfiguredAgent(
            context=agent_context(),
            prompt='Act as a deal coach.',
            model_name='gpt-5-mini',
            model_settings={},
            allowed_tools=('search_deals',),
            allowed_skills=(),
            max_turns=6,
            output_mode='text',
            output_schema=None,
            skills_dir=tmp_path,
            agent_factory=FakeAgent,
            model_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            tool_registry_factory=lambda _context: EmptyRegistry(),
        )
~~~

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/agents/test_user_configured_agent.py -q`.

Expected: collection fails because `src.agents.workflows.user_configured.agent` does not exist.

- [ ] **Step 3: Implement the dedicated class**

Create an empty package `__init__.py`. In `agent.py`, implement this public contract:

~~~python
class UserConfiguredAgent:
    '''Execute one non-interactive agent from an immutable saved definition.'''

    def __init__(
        self,
        *,
        context: AgentContext,
        prompt: str,
        model_name: str | None,
        model_settings: dict[str, Any],
        allowed_tools: tuple[str, ...],
        allowed_skills: tuple[str, ...],
        max_turns: int | None,
        output_mode: str,
        output_schema: dict[str, Any] | None,
        skills_dir: Path = DEFAULT_SKILLS_DIR,
        agent_factory: Callable[..., Any] = AgnoAgent,
        model_factory: Callable[..., Any] = AzureOpenAI,
        tool_registry_factory: Callable[[AgentContext], Any] | None = None,
    ) -> None:
        config = agent_config.get_config('user_configured_agent')
        effective_model = model_name or config.model
        model = model_factory(
            id=effective_model,
            api_version=config.api_version,
            retries=config.retries,
            delay_between_retries=1,
            exponential_backoff=True,
            request_params={**model_settings, 'parallel_tool_calls': True},
        )
        registry = (tool_registry_factory or (lambda item: ToolRegistry(item, lambda: None)))(context)
        tool_names = [ToolName(key) for key in allowed_tools]
        tools = registry.get_tools(tool_names)
        if len(tools) != len(tool_names):
            raise ValueError('one or more pinned tools are not registered in this runtime')
        skills = self._load_selected_skills(allowed_skills, skills_dir)
        structured = output_mode == 'json'
        self.model_name = effective_model
        self.agent = agent_factory(
            model=model,
            description=prompt,
            structured_outputs=structured,
            output_schema=output_schema if structured else None,
            parse_response=True,
            markdown=not structured,
            tools=tools,
            tool_call_limit=max_turns or 150,
            skills=skills,
            telemetry=False,
            debug_mode=config.debug_mode,
        )

    async def run(self, message: str) -> Any:
        return await self.agent.arun(input=message)

    @staticmethod
    def _load_selected_skills(
        skill_keys: tuple[str, ...],
        skills_dir: Path,
    ) -> Skills | None:
        if not skill_keys:
            return None
        loaders: list[SkillLoader] = []
        for key in skill_keys:
            path = skills_dir / key
            if not path.is_dir() or not (path / 'SKILL.md').is_file():
                raise ValueError(f'pinned skill is not available: {key}')
            loaders.append(LocalSkills(str(path)))
        return Skills(loaders=loaders)
~~~

Define `DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / 'skills'`, matching the existing `src/agents/skills` location. Do not import API schemas, `GeneralAgent`, streaming frames, templates, triggers, or database models.

- [ ] **Step 4: Verify GREEN**

Run `uv run pytest tests/agents/test_user_configured_agent.py -q`.

Expected: both tests pass.

### Task 3: Delegate run execution to the dedicated class

**Files:**

- Modify: `src/service/automation/runs/runtime_executor.py`
- Modify: `tests/service/automation/runs/test_runtime_executor.py`

- [ ] **Step 1: Write the failing delegation test**

Replace the low-level Agno-constructor assertion with this dedicated-agent delegation contract:

~~~python
class FakeUserConfiguredAgent:
    model_name = 'gpt-5-mini'

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.messages: list[str] = []

    async def run(self, message: str):
        self.messages.append(message)
        return SimpleNamespace(
            content='Call the buyer',
            model='gpt-5-mini',
            metrics=None,
        )


@pytest.mark.asyncio
async def test_executor_passes_pinned_snapshot_to_user_configured_agent() -> None:
    created: list[FakeUserConfiguredAgent] = []

    def create_agent(**kwargs) -> FakeUserConfiguredAgent:
        agent = FakeUserConfiguredAgent(**kwargs)
        created.append(agent)
        return agent

    item = envelope()
    runtime = AgentRunExecutor(agent_factory=create_agent)
    context = AgentContext(
        organisation_id=item.organisation_id,
        deal_id=item.deal_id,
        user_id=item.owner_user_id,
    )

    result = await runtime.execute(item, context=context)

    assert created[0].kwargs['context'] is context
    assert created[0].kwargs['prompt'] == item.prompt
    assert created[0].kwargs['model_name'] == item.model_name
    assert created[0].kwargs['model_settings'] == item.model_config
    assert created[0].kwargs['allowed_tools'] == item.allowed_tools
    assert created[0].kwargs['allowed_skills'] == item.allowed_skills
    assert created[0].kwargs['max_turns'] == item.max_turns
    assert created[0].kwargs['output_mode'] == item.output_mode
    assert created[0].kwargs['output_schema'] == item.output_schema
    assert '"name": "Acme"' in created[0].messages[0]
    assert result.output_text == 'Call the buyer'
~~~

Keep the existing tests for tenant-context rejection, JSON Schema validation, base `jsonschema` installation, and the light-worker/API import boundary.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/service/automation/runs/test_runtime_executor.py -q`.

Expected: the delegation test fails because the executor still accepts low-level Agno/model/tool factories.

- [ ] **Step 3: Reduce the executor to orchestration**

Replace its constructor with:

~~~python
def __init__(
    self,
    *,
    agent_factory: Callable[..., Any] = UserConfiguredAgent,
) -> None:
    self.agent_factory = agent_factory
~~~

After `_validate_context`, construct and run the dedicated agent:

~~~python
agent = self.agent_factory(
    context=context,
    prompt=envelope.prompt,
    model_name=envelope.model_name,
    model_settings=envelope.model_config,
    allowed_tools=envelope.allowed_tools,
    allowed_skills=envelope.allowed_skills,
    max_turns=envelope.max_turns,
    output_mode=envelope.output_mode,
    output_schema=envelope.output_schema,
)
message = json.dumps(
    {'goal': envelope.task_goal, 'event': envelope.input_snapshot},
    default=str,
    sort_keys=True,
)
started = perf_counter()
response = await agent.run(message)
~~~

Use `agent.model_name` as the fallback for usage metadata. Retain `_validate_context`, `_normalize_output`, event-message creation, timing, and token/cost normalization in the executor. Delete only imports, fields, and the skill-loader helper made unused by the extraction.

Do not modify `AgentRunExecutionService`, Celery tasks, claim/retry/recovery behavior, or lifecycle error classification.

- [ ] **Step 4: Verify GREEN**

Run `uv run pytest tests/service/automation/runs/test_runtime_executor.py -q`.

Expected: every executor test passes, including the light-worker import regression.

### Task 4: Verify the bounded slice

**Files:** Verify only.

- [ ] **Step 1: Run focused tests**

~~~powershell
uv run pytest tests/infra/config/test_agent_config.py tests/agents/test_user_configured_agent.py tests/service/automation/runs/test_runtime_executor.py tests/workers/chat_worker/services/test_agent_run_execution_service.py tests/workers/chat_worker/tasks/test_agent_run_tasks.py -q
~~~

Expected: all selected tests pass.

- [ ] **Step 2: Run lint and types**

~~~powershell
uv run ruff check src/infra/config/agent_config.py src/agents/workflows/user_configured src/service/automation/runs/runtime_executor.py tests/infra/config/test_agent_config.py tests/agents/test_user_configured_agent.py tests/service/automation/runs/test_runtime_executor.py
uv run mypy src/agents/workflows/user_configured/agent.py src/service/automation/runs/runtime_executor.py
~~~

Expected: both commands exit `0`.

- [ ] **Step 3: Prove General Agent and worker orchestration stayed untouched**

~~~powershell
git diff --name-only -- src/agents/workflows/general src/agents/orchestrator.py src/workers/chat_worker/services/agent_run_execution_service.py src/workers/chat_worker/tasks/agent_run_tasks.py
~~~

Expected: no output.

- [ ] **Step 4: Review the final diff**

Run `git diff --check`, then inspect the exact files in the file map. Expected: no whitespace errors, and every line maps to dedicated configuration, agent assembly, executor delegation, or tests.

## Acceptance criteria

- Triggered runs instantiate `UserConfiguredAgent`, never `GeneralAgent`.
- Prompt, model settings, tools, skills, turn limit, and output contract come only from the immutable runtime envelope.
- Missing pinned tools or skills fail before the model call.
- Organisation, owner, and deal checks remain before selected tools execute.
- `AGENT_USER_CONFIGURED_MODEL`, `AGENT_USER_CONFIGURED_DEBUG`, and `AGENT_USER_CONFIGURED_TIMEOUT` are independent operational controls.
- Existing delivery, claims, retries, recovery, run history, triggers, templates, and APIs remain unchanged.
- The light-worker runtime still imports without API-only packages.

## Explicitly deferred

- Interactive chat execution and streaming for user-created agents.
- User-uploaded code, arbitrary tools, or arbitrary skill paths.
- Per-agent Azure credentials, API versions, retries, or worker timeout policy.
- Renaming persisted `agent_type='general'` values.
- Runtime dispatch by agent type; add it only when a second real execution strategy exists.
