# CRM Read-only Emails API

The CRM sidebar can show synchronized email messages without becoming an email client.
These endpoints are read-only: they do not send, reply, forward, delete, move, mark as
read, or otherwise change the provider mailbox.

Every response uses the standard envelope:

```json
{
  "status": "SUCCESS",
  "message": "Emails retrieved successfully",
  "data": { "items": [], "cursor": null }
}
```

## Routes

| UI level | Method and route | Message membership |
| --- | --- | --- |
| Overall mailbox | `GET /users/crm/emails` | The authenticated user's active Google or Microsoft mailbox integrations. |
| Contact | `GET /users/crm/contacts/{contact_id}/emails` | Emails whose canonical EMAIL activity is linked to that contact. |
| Account | `GET /users/crm/accounts/{company_id}/emails` | Emails whose canonical EMAIL activity is linked to that account. |
| Deal | `GET /users/crm/deals/{deal_id}/emails` | The user's synced copies of emails with an active deal-email association. The normal deal visibility check runs first. |

All routes accept:

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `limit` | integer | `20` | `1` through `100`. |
| `cursor` | string | none | Opaque cursor from the prior response. |
| `integration_id` | UUID | none | Overall endpoint only; narrows to one of the caller's connected mailboxes. |

Messages are newest first by `received_at`, then `email_id`. A non-null `cursor` means
another page is available; the frontend sends it unchanged on the next request.

## Message item

```json
{
  "email_id": "813aa013-1e5d-4ad8-87f6-c4a2bd5d4d43",
  "integration_id": "e3a785de-24c5-4052-a3d5-7c93e7e303e2",
  "internet_message_id": "<abc@example.com>",
  "thread_id": "AAMk...",
  "subject": "Quarterly review",
  "from_name": "Maya Shah",
  "from_email": "maya@acme.com",
  "to_emails": ["seller@your-company.com"],
  "cc_emails": [],
  "received_at": "2026-07-26T09:30:00Z",
  "sent_at": "2026-07-26T09:29:42Z",
  "direction": "inbound",
  "is_read": false,
  "is_important": true,
  "has_attachments": true,
  "labels": ["Inbox"],
  "snippet": "Could we review the revised proposal…",
  "body_preview": "Could we review the revised proposal…"
}
```

`body_preview` is capped at 1,000 characters. HTML bodies and attachment contents are
intentionally absent, so the sidebar cannot accidentally render untrusted email HTML or
confuse a preview with an authorized attachment download. A future full-message drawer
must use a dedicated authorized detail endpoint.

## Frontend behavior

Render a compact Outlook-like list: direction, sender/recipient, subject, preview, time,
importance, and attachment indicator. Treat it as a mailbox viewer: never display compose,
reply, forward, delete, move, mark-read, or provider-state controls from this API.
