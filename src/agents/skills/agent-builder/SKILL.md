---
name: agent-builder
description: Build or configure CopilotGTM agents from chat. Use when the user asks to create an agent, use an agent template, automate or schedule an agent, attach a trigger, or check whether they already used a template.
---

# Agent Builder

Build private user agents from live backend catalogs. Never invent a tool key, skill key,
template version, trigger key, option value, or filter path.

## Use this workflow

1. Inspect before changing.
- Call `inspect_agent_builder` with `action="overview"`.
- If a template matches, tell the user about every existing copy before creating another.
- For a custom agent, select only the minimum capabilities needed. Write a self-contained
  prompt with its objective, relevant inputs, allowed actions, constraints, and expected output.
- If the user wants a trigger, exclude capabilities marked `automation_compatible=false`. If
  one is essential to the request, explain that the requested agent cannot run automatically.

2. Create only from explicit intent.
- Use `change_agent_builder` with `action="create_agent"` for a custom definition.
- Use `action="use_template"` with the exact listed template ID and version.
- If the tool returns `needs_confirmation`, show the existing agent names and ask whether to
  create another copy. Never set `confirm_duplicate=true` without that confirmation.

3. Configure a trigger from live metadata.
- Read `references/trigger-building.md` before configuring any trigger.
- Call `inspect_agent_builder` with `action="trigger_catalog"`; never hard-code supported
  triggers.
- Use `action="trigger_options"` to resolve human names when the catalog exposes an option
  source. Never ask the user for an internal ID they can identify by name.
- Ask one focused question for required configuration or ambiguity that cannot be resolved
  safely. Use active status when the user explicitly asks it to run; otherwise clarify active
  versus paused when intent is unclear.
- Call `change_agent_builder` with `action="attach_trigger"` only after the agent, trigger,
  configuration, and filters are unambiguous.
- Personal authority is fixed by the tool. Never request organisation scope, audiences, or
  internal principal IDs.

4. Report persisted results.
- Separate `Created`, `Attached`, `Blocked`, and `Needs confirmation` outcomes.
- Agent creation and trigger attachment are separate durable changes. If attachment fails,
  keep and report the valid agent; never claim it was rolled back or silently recreate it.
- Never claim a trigger fired or an agent run succeeded merely because the trigger was attached.

## Output contract

State readable agent, template, and trigger names; active or paused status; relevant created ID;
and the next action. Do not expose unrelated internal IDs or paste raw tool JSON.

## Reference loading guide

- Use `references/trigger-building.md` for trigger-family behavior, catalog fields, safe
  activation, and unsupported chat operations.
