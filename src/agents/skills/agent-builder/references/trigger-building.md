# Trigger Building

Use the live trigger catalog as the authority. Do not copy trigger keys, fields, or option values
from memory.

## Trigger families

| Family | Meaning | Configuration rule |
|---|---|---|
| Event | Reacts to a source record or integration event. | Configure the catalog's source, watch, and filter fields. Attachment proves registration, not that a matching event occurred. |
| Schedule | Starts on a catalog-supported cadence or time. | Preserve the user's local scheduling intent and use only catalog fields and defaults. |
| Manual | Has no autonomous event or cadence. | Attach only when the catalog supports it; do not describe it as scheduled or continuously running. |
| Webhook | Starts from an inbound request and returns one-time credentials. | Do not create in chat. Direct the user to the Agents UI so URL and secret never enter chat or tool history. |

Do not infer that a family is supported because it appears in this table. It must appear in the
current personal trigger catalog with `chat_supported=true`.

## Payload roles

- `config`: trigger-family settings and source selectors defined by the catalog.
- `watch`: the catalog-defined changes or fields that count as an event. Omit when not offered.
- `filter`: optional match rules using exact catalog paths, operators, and option values.
- `goal`: the self-contained task the agent should perform when the trigger starts a run.

## Safe configuration

1. Load `trigger_catalog` and choose only a trigger supporting personal execution and chat.
2. Apply a catalog default only when it matches the user's intent. A default is not permission
   to invent a missing required decision.
3. When a field exposes an option source, call `trigger_options` with its exact control and field
   path. Resolve human-readable names; ask a grounded choice question if multiple options match.
4. Check the target agent's automation eligibility. Never attach a trigger to an agent with a
   retry-unsafe capability.
5. Use `active` when the user explicitly asks the automation to run. Use `paused` when they ask
   to prepare it without starting; otherwise clarify when the intent is ambiguous.
6. Attach once. Report the returned trigger status and any partial state exactly.

The tool derives the personal principal, owned-or-created scope, and principal-only audience.
Never accept or solicit organisation execution, publish/admin controls, audience user IDs, or
principal IDs. Those controls remain unavailable until an explicit product policy and UI exist.

Attachment confirms persisted configuration only. Never claim an event fired, a schedule ticked,
an agent ran, or a downstream effect succeeded without separate runtime evidence.
