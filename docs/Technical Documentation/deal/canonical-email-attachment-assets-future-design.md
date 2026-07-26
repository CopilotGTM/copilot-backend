# Future design: canonical email attachment assets

Status: proposal for future implementation. This is not the current production
schema or runtime flow.

The current implementation is documented in
[`email-attachment-assets.md`](email-attachment-assets.md). Today, an email
attachment is extracted only after the email confidently matches a deal, and
the resulting `DealAsset` is owned by that deal.

## Decision

An email attachment should have one canonical asset record per organisation.
The source email owns the attachment. Contacts, accounts, and deals see that
asset through the email's existing CRM activity links.

Do not create separate copies of the same file for every contact, account, or
deal. Do not make a projected CRM activity the source of truth for attachment
ownership, because activities can be rebuilt from source communications.

```text
contact / account / deal
          |
          v
   crm_activity_links
          |
          v
   canonical email activity
          |
          v
         email
          |
          v
   email_asset_links
          |
          v
        assets
```

## Why change the current model

`deal_assets.deal_id` is currently required, and both asset identity and blob
paths are deal-scoped. This works for a deal asset library, but it has three
limitations:

- contact and account activity feeds contain the email but do not directly
  expose its file assets;
- one email associated with multiple deals can produce multiple asset rows and
  blobs for the same attachment;
- email provenance is partly stored in `asset_metadata` JSON instead of an
  indexed relational link.

The future model separates the stored file from the CRM objects through which
users discover it.

## Target data model

Names are illustrative and should be finalized against the schema at
implementation time.

### `assets`

One row represents one stored file and its processing lifecycle.

Suggested fields:

- `asset_id`
- `organisation_id`
- `storage_path`
- `filename`
- `mimetype`
- `size_bytes`
- `checksum`
- `status`
- thumbnail and knowledge-base fields
- created/updated/deleted fields

The table should not require a `deal_id`.

### `email_asset_links`

One row records that an asset originated from an email attachment.

Suggested fields:

- `organisation_id`
- `asset_id`
- canonical email identity, preferably `internet_message_id`
- local `email_id` used to retrieve the provider copy
- provider attachment ID as provenance, not canonical identity
- integration ID/type used for the successful download

The canonical uniqueness boundary should be organisation, canonical message,
normalized filename, and content checksum. Provider-local attachment IDs are
not stable across mailbox copies.

### Existing `crm_activity_links`

Continue using the existing activity links to expose the email activity to:

- `CONTACT`
- `COMPANY`
- `DEAL`

Contact, account, and deal asset reads can follow those links to the source
email and then to `email_asset_links`. This preserves one file while allowing
it to appear in several CRM contexts.

### Optional `asset_object_links`

Add a direct polymorphic link only when an asset is not inherited from an email,
for example:

- a user uploads a contract directly to a deal;
- a user attaches a document directly to a contact;
- a CRM provider supplies an explicit account-document association.

Suggested fields:

- `organisation_id`
- `asset_id`
- `object_type`
- `object_id`
- `link_role`
- `link_source`

Do not eagerly copy every email-derived relationship into this table. Add an
email-derived projection only if measured read performance later requires it.

## Runtime flow

```text
email sync commits an email with has_attachments=true
  -> communication hydration resolves contacts and projects one email activity
  -> existing activity links connect the activity to contacts/accounts/deals
  -> after the email is relevant to at least one CRM object, enqueue extraction
  -> extraction resolves all usable mailbox copies of the canonical message
  -> list and filter provider attachments
  -> download each canonical attachment once
  -> upload one deterministic blob
  -> insert one PROCESSING asset and one email_asset_link
  -> run the existing post-upload lifecycle
  -> mark the canonical asset ACTIVE or FAILED
```

Do not download every attachment from every retained mailbox email
unconditionally. The `has_attachments` flag is only a cheap candidate signal.
Download when the email becomes relevant to at least one CRM object, or during
an explicit historical backfill.

## Read behavior

### Contact assets

Return assets whose source email activity has an active `CONTACT` activity link
to the requested contact.

### Account assets

Return assets whose source email activity has an active `COMPANY` activity link
to the requested account. The existing communication projection already
resolves company IDs from participating contacts.

### Deal assets

Return both:

- email-derived assets reached through an active `DEAL` activity link; and
- directly linked/manual assets from `asset_object_links`.

Every read must enforce `organisation_id`, soft-delete state, and asset status.
Pagination should use a stable timestamp plus asset ID tie-breaker.

## Migration and backfill

Schema migration and provider backfill are separate operations.

1. Create the canonical asset and email-link schema without downloading files
   inside Alembic.
2. Dual-write new email-derived assets to the new model while current deal
   asset reads remain available.
3. Backfill existing `DealAsset` rows into canonical assets and links, grouping
   verified duplicates by canonical message identity, filename, and checksum.
4. Run a resumable worker over remaining active emails with
   `has_attachments=true` that are linked to at least one CRM object.
5. Process bounded batches with persisted progress, idempotent inserts, retries,
   and per-organisation concurrency limits.
6. Compare old and new deal asset results before switching reads.
7. Switch contact, account, and deal asset APIs to the canonical read model.
8. Remove the old deal-owned columns or table only after the compatibility
   period and rollback window.

Existing JSON provenance includes local email ID, canonical internet message
ID, provider attachment ID, and integration type. The backfill should migrate
those values into relational link columns rather than discard them.

## Production requirements

- Tenant isolation on every table and query.
- Database uniqueness as the final concurrency guard.
- One deterministic asset identity across mailbox copies.
- No database session held during provider or blob-storage calls.
- Retry-safe extraction and post-upload processing.
- Recovery for assets stranded in `PROCESSING` or retryable `FAILED`.
- Bounded provider calls, attachment sizes, batch sizes, and task concurrency.
- Safe fallback to another active mailbox copy when one provider copy fails.
- Reference-aware deletion: removing one CRM link must not delete an asset still
  reachable elsewhere.
- Metrics for candidates, downloads, deduplicated files, failures, retries,
  bytes stored, and backfill progress.

## Non-goals

- Do not introduce separate contact, account, and deal copies of the same file.
- Do not make page loads download or process provider attachments.
- Do not attach files only to projected activities.
- Do not run provider downloads from a database migration.
- Do not add a new worker or queue unless measured load proves the existing
  deal-worker boundary is insufficient.

## Implementation checkpoint

Before implementation, re-check the then-current asset, email, activity-link,
and storage schemas. The preferred first slice is the smallest one that proves:

1. one email attachment creates one canonical asset;
2. the same asset appears in its contact, account, and deal contexts;
3. associating the email with another deal does not download or store the file
   again.
