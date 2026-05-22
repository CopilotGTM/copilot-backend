# Agent Action Approval Preferences API

User-scoped preferences for whether Kai should ask before running write-capable agent tools.

The durable preference is stored under `public.users.preferences.agent_action_approvals`. Organisation policy in `public.organisations.preferences` remains the hard gate and cannot be bypassed by user preferences.

## GET `/users/preference/agent-actions`

Returns the current user's effective action approval preferences.

Effective preferences are built from code defaults plus any saved user override. Locked tools always return their locked value even if malformed JSON exists in the database.

### Response

```json
{
  "status": "SUCCESS",
  "message": "Agent action approval preferences retrieved successfully",
  "data": {
    "schema_version": 1,
    "tools": [
      {
        "tool_name": "create_task",
        "label": "Create task",
        "category": "crm",
        "approval_required": false,
        "default_approval_required": false,
        "editable": true,
        "locked_reason": null
      },
      {
        "tool_name": "send_email",
        "label": "Send email",
        "category": "communication",
        "approval_required": true,
        "default_approval_required": true,
        "editable": false,
        "locked_reason": "External email sending is always approval-gated."
      }
    ]
  }
}
```

## PATCH `/users/preference/agent-actions`

Updates editable tool approval preferences for the current user.

Unknown tool names are ignored. Locked tools are forced back to their default approval value.

### Request

```json
{
  "tools": {
    "create_task": true,
    "create_deal": false,
    "send_email": false
  }
}
```

### Response

```json
{
  "status": "SUCCESS",
  "message": "Agent action approval preferences updated successfully",
  "data": {
    "schema_version": 1,
    "tools": [
      {
        "tool_name": "create_task",
        "label": "Create task",
        "category": "crm",
        "approval_required": true,
        "default_approval_required": false,
        "editable": true,
        "locked_reason": null
      },
      {
        "tool_name": "send_email",
        "label": "Send email",
        "category": "communication",
        "approval_required": true,
        "default_approval_required": true,
        "editable": false,
        "locked_reason": "External email sending is always approval-gated."
      }
    ]
  }
}
```

## Tool Execution Contract

When a tool is approval-gated by user preference, the tool must not mutate data on the first call.

Possible tool responses:

| Status | Meaning |
|---|---|
| `approval_required` | No interactive card was sent. Ask the user to approve, then call the same tool again with `confirmed=true` and the returned `approval_state_id` if supported. |
| `confirmation_required` | The tool returned a structured confirmation payload. Ask the user to confirm, then call the same tool again with `confirmed=true` and the returned `approval_state_id`. |
| `preview_sent` / `form_sent` | An interactive Teams/Slack confirmation was sent. Wait for the user's card action. |
| `error` with `crm_write_scope_disabled` | Organisation policy blocks the write. User approval cannot override this. |

## Confirmation Token Verification

For chat/tool retry confirmations, the first approval-gated response includes a Redis-backed `approval_state_id`.

The token binds:

| Field | Verification |
|---|---|
| User | Must match the current `AgentContext.user_id`. |
| Organisation | Must match the current `AgentContext.organisation_id`. |
| Tool | Must match the tool being executed. |
| Payload | The retry payload must hash to the same canonical payload hash. |
| Expiry | Token expires after 10 minutes. |

Example first response:

```json
{
  "status": "confirmation_required",
  "code": "agent_action_approval_required",
  "tool_name": "send_email",
  "approval_state_id": "act_xxx",
  "confirmation_payload": {
    "to": ["buyer@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Follow up",
    "body": "Thanks for your time.",
    "deal_id": null,
    "sender_email": "seller@example.com"
  },
  "confirmation_reasons": ["user_action_approval_preference"]
}
```

Approved retry:

```json
{
  "to": ["buyer@example.com"],
  "subject": "Follow up",
  "body": "Thanks for your time.",
  "sender_email": "seller@example.com",
  "confirmed": true,
  "approval_state_id": "act_xxx"
}
```

If the payload changes, the token expires, or the token belongs to another user/org/tool, execution is rejected and the user must confirm again.

## Default Tool Policy

| Tool | Default approval required | Editable |
|---|---:|---:|
| `create_task` | `false` | `true` |
| `add_deal_note` | `false` | `true` |
| `create_deal` | `true` | `true` |
| `create_account` | `true` | `true` |
| `create_contact` | `true` | `true` |
| `add_stakeholder` | `true` | `true` |
| `create_reminder` | `true` | `true` |
| `send_calendar_invite` | `true` | `true` |
| `send_email` | `true` | `false` |

`send_email` is visible so the UI can explain the policy, but it is not editable. It is always approval-required.
