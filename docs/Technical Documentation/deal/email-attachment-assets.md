# Email attachment deal assets

This document describes how an associated Gmail or Outlook email becomes one or
more deal assets.

## Ownership

| Responsibility | Owner |
|---|---|
| Exact email-to-deal attachment signal | `src/service/crm/communications` |
| Asset extraction and deduplication | `src/service/crm/assets/email_attachment_service.py` |
| Gmail and Graph protocol behavior | `src/service/integrations/email/attachment_provider.py` |
| Checksum, thumbnails, and asset state | `src/service/crm/assets/post_upload_service.py` |
| Locks, retries, queue publication, and status-event publication | `src/workers/deal_worker/tasks` |

Service modules do not import worker modules or publish Celery work.

## Entry points

The normal path starts after communication hydration commits an active
email-to-deal association:

```text
email sync or deal replay
  -> communication hydration commits associations
  -> hydration returns exact attachment email IDs grouped by deal
  -> deal_asset.extract_email_attachments_deal(email_ids=[...])
  -> extraction commits PROCESSING assets
  -> task publishes deal_asset.post_upload
  -> post-upload commits ACTIVE or FAILED
  -> task publishes the committed asset status event
```

Thread history may be loaded to make a deal association decision, but only email
IDs in the current hydration scope can emit attachment work. This prevents a
single changed message from widening into a whole-thread or whole-deal provider
scan.

The all-organisation and organisation tasks remain available for explicit
operator repair. They are intentionally not registered in the periodic schedule.

## Celery tasks

| Task | Purpose | Retries |
|---|---|---:|
| `deal_asset.extract_email_attachments_deal` | Extract one deal, optionally restricted to at most 100 exact email IDs | 3 |
| `deal_asset.extract_email_attachments_org` | Locked operator repair for one organisation | 2 |
| `deal_asset.extract_email_attachments_all_orgs` | Fan out operator repair to approved organisations | 0 |
| `deal_asset.post_upload` | Compute media metadata and finalize one asset | 3 |

The deal task accepts `email_ids=None` only for explicit broad repair. An empty
list is rejected so an automatic caller cannot accidentally fall through to
broad mode. When the per-run asset limit is reached, the task republishes
bounded continuation chunks.

The organisation task publishes every returned post-upload asset and bounded
deal continuation. Any reported provider error makes the task retry rather than
returning a successful-looking partial result.

## Extraction

For each canonical `internet_message_id`, extraction:

1. Loads all active, non-deleted, organisation-owned mailbox copies associated
   with the deal.
2. Orders copies deterministically.
3. Resolves the provider token through
   `src.service.integrations.providers.token_resolution`.
4. Lists Gmail MIME attachments or Microsoft Graph file attachments.
5. Filters junk and unsupported MIME types.
6. Tries the next mailbox copy when one copy cannot be read.
7. Downloads bytes and computes SHA-256 before choosing the stable asset
   identity.
8. Reuses a matching legacy row when possible.
9. Uploads to a deterministic blob path and inserts a `PROCESSING` `DealAsset`.
10. Returns committed asset IDs to the task for post-upload publication.

Database sessions are released before provider and Azure operations.

### Provider behavior

Gmail uses:

- `users.messages.get?format=full` for the MIME tree;
- recursive MIME-part traversal for `body.attachmentId`;
- `users.messages.attachments.get` for URL-safe base64 content.

Microsoft Graph uses:

- `GET /me/messages/{message_id}/attachments`;
- only `#microsoft.graph.fileAttachment` rows;
- `GET /me/messages/{message_id}/attachments/{attachment_id}` for standard
  base64 content.

Provider path identifiers are URL-escaped. Decoded content over 25 MB is
rejected.

### Filtering

The provider removes:

- files over 25 MB or under 1 KB;
- calendar (`.ics`) and forwarded-email (`.eml`) payloads;
- signature-style image names;
- images under 100 KB.

Extraction then requires the normalized MIME type to exist in
`chat_file_upload_config.ALLOWED_MIMETYPES`.

## Stable identity and legacy compatibility

New assets use a deal-scoped, mailbox-independent identity:

```text
message_hash = sha256(internet_message_id)[:16]
file_hash = sha256(casefold(safe_filename) + NUL + content_sha256)[:32]
source_record_id = email:{message_hash}:file:{file_hash}
```

Mailbox-local provider attachment IDs remain provenance only in
`asset_metadata.provider_attachment_id`.

Before inserting a new row, extraction also checks legacy attachment-ID rows by
canonical message ID, normalized filename, and checksum. If an old row has no
checksum but has a blob, the service computes and persists that checksum before
deciding whether to reuse it.

The database unique constraint is the final concurrency guard. An insert
conflict returns the winning `PROCESSING` or recoverable `FAILED` row for
post-upload republication.

## Storage and asset fields

Blob paths are deterministic:

```text
deal_assets/{deal_id}/email_attachments/{message_hash}/{identity_hash}/{safe_filename}
```

Created rows use:

| Field | Value |
|---|---|
| `status` | `PROCESSING` |
| `source_type` | `email_attachment` |
| `checksum` | SHA-256 of downloaded bytes |
| `direction` | `received`, `shared`, or `internal` from the association |
| `added_by_user_id` | `AUTOMATED_PIPELINE_USER_ID` |
| `asset_metadata` | Internal email ID, canonical message ID, provider attachment ID, and integration type |

## Post-upload state machine

The post-upload service returns a structured outcome:

- `processed`: checksum exists, optional thumbnails are committed, status is
  `ACTIVE`;
- `already_processed`: the existing `ACTIVE` row and event are reusable;
- `retryable_failure`: the row is committed `FAILED` and the task retries;
- `invalid`: missing asset or unusable input is terminal.

An asset is never marked `ACTIVE` without a checksum. Thumbnail failure is
non-fatal only after a usable checksum exists. The service commits before
returning its event; the task owns event publication and retries a publication
failure safely.

Manual uploads use the same post-upload task. If the API commits `PROCESSING`
but broker publication fails, it returns HTTP 503. Repeating finalize returns
the same `PROCESSING` row and republishes work.

## Verification

```bash
uv run pytest \
  tests/service/integrations/email/test_attachment_provider.py \
  tests/service/crm/assets \
  tests/workers/deal_worker/tasks/test_deal_asset_email_attachment_tasks.py \
  tests/workers/deal_worker/tasks/test_deal_asset_post_upload_task.py \
  tests/workers/deal_worker/tasks/test_email_attachment_task_registration.py \
  tests/workers/deal_worker/test_cron_dispatch.py \
  tests/api/routers/crm/test_deal_assets_handler.py \
  -q

uv run ruff check \
  src/service/crm/assets \
  src/service/integrations/email/attachment_provider.py \
  src/workers/deal_worker/tasks/deal_asset_email_attachment_tasks.py \
  src/workers/deal_worker/tasks/deal_asset_post_upload_task.py
```
