# CRM Search Strategy: Contacts, Accounts, and Deals

## Objective

Make CRM search fast and predictable without adding Elasticsearch or a broad search
platform. The first version should use Postgres well, keep the query surface small,
and leave room to expand later when product behavior proves the need.

This document covers search for:

- Contacts (`crm_contacts`)
- Accounts/companies (`crm_companies`)
- Deals (`crm_deals`)

## Current State

The current CRM APIs mostly use case-insensitive partial search:

- Contacts list: `full_name`, `email`, `company_name`
- Contact picker/search: intended for fast typeahead
- Accounts list/search: `name`, `domain`, `website`, and optional `industry`
- Deals list: `name`, `company_name`
- Deal owner filter: `owner_name`, `owner_email`, plus exact owner UUID

These queries use patterns like:

```sql
column ILIKE '%query%'
```

Normal btree indexes do not make this pattern fast. Postgres trigram indexes
(`pg_trgm` + `GIN`) are the right first production step for this workload.

## Design Principle

Do not create one generic "search everything" endpoint.

Use backend-owned search functions/endpoints. The caller chooses the workflow
endpoint, and the backend maps that intent to a fixed, reviewed list of
searchable fields and ranking rules.

Example:

```http
GET /users/crm/contacts/search/create-candidates?q=sam
GET /users/crm/contacts/search?q=sam
GET /users/crm/accounts/search?q=openai
GET /users/crm/deals/search?q=acme
```

The frontend or gateway may choose the endpoint/function, but it must not choose
raw column names. Column choice stays in backend code so search remains safe,
indexed, and consistent.

## Search vs Filter

The main production loophole is mixing search fields and filter fields.

Free-text search should cover fields a sales rep naturally types into one box:
person name, email, account name/domain, deal name, and sometimes the account
name on a deal. Structured fields should stay as filters because they are low
cardinality or better represented by dropdowns.

| Entity | Free-text search | Filter/sort, not text search |
|--------|------------------|------------------------------|
| Contacts | Name, email, alternate emails, company string | owner, lifecycle, lead status, marketing status, country, enrichment state |
| Accounts | Account name, domain, website | owner, industry, lifecycle, country, employee count, revenue |
| Deals | Deal name, company/account name, primary contact identity when needed | owner, pipeline, stage, status, close date, amount, priority, forecast category |

This keeps the search box relevant while still covering the fields sales teams
normally use to find CRM records.

## Why Named Search Paths

Different screens have different relevance needs.

When creating a contact, matching by company name is noisy. If a user types
`sam@openai.com`, we should find the person or allow creation. We should not
return every OpenAI contact because company matched.

When using a contact picker, company can be useful because users often remember
"Sam from OpenAI".

When browsing all contacts, company search is expected because the page behaves
like a CRM table.

One query shape cannot serve all three without either missing useful results or
returning noisy ones.

## Proposed Search Paths

### Contact Search Paths

#### `search_contacts_for_create`

Purpose: prevent duplicate creation and show exact likely matches.

Search columns:

| Column | Match Type | Index Now | Reason |
|--------|------------|-----------|--------|
| `crm_contacts.email` | exact, prefix, partial | Yes | Primary duplicate and identity field |
| `crm_contacts.additional_emails` | exact, partial as JSON text | Yes | Provider may store alternate emails |
| `crm_contacts.full_name` | prefix, partial | Yes | User may type a name instead of email |

Do not search:

| Column | Reason |
|--------|--------|
| `company_name` | Too noisy for create; company match does not identify a person |
| `job_title` | Not identity; high false positives |
| `owner_name`, `owner_email` | Ownership is not contact identity |
| `first_name`, `last_name` | Usually derived from `full_name`; only add if provider data proves `full_name` is often missing |

Ranking:

1. Exact primary email
2. Exact additional email
3. Email prefix
4. Exact full name
5. Name prefix
6. Name contains

#### `search_contacts`

Purpose: select an existing contact for a deal, account, or workflow.

Search columns:

| Column | Match Type | Index Now | Reason |
|--------|------------|-----------|--------|
| `crm_contacts.email` | exact, prefix, partial | Yes | Fast identity lookup |
| `crm_contacts.additional_emails` | exact, partial as JSON text | Yes | Alternate identities |
| `crm_contacts.full_name` | prefix, partial | Yes | Main person search |
| `crm_contacts.company_name` | partial | Yes | Useful for "person at company" in picker/table search |

Recommendation: include `company_name` only for normal search, not
`search_contacts_for_create`.

Do not index `first_name` and `last_name` in the first pass. If imported CRM data
regularly has split names without `full_name`, we should fix ingestion to populate
a canonical search/display name instead of indexing redundant columns.

### Account Search Paths

#### `search_accounts`

Purpose: find accounts while creating/linking accounts, contacts, and deals.
Account creation and account selection both use the same first-pass account
identity fields, so one endpoint/function is enough.

Search columns:

| Column | Match Type | Index Now | Reason |
|--------|------------|-----------|--------|
| `crm_companies.domain` | exact, prefix, partial | Yes | Most precise company identity |
| `crm_companies.name` | prefix, partial | Yes | Main account lookup |
| `crm_companies.website` | partial | Yes | Useful when users paste URLs |

Ranking:

1. Exact domain
2. Domain prefix
3. Exact name
4. Name prefix
5. Website/domain contains

Recommendation: do not add trigram on `industry` first unless industry search
becomes slow in production. It is often a filter/dropdown field, not free-text
search.

### Deal Search Paths

#### `search_deals`

Purpose: find a deal quickly for linking notes, activities, contacts, or assets.

Search columns:

| Column | Match Type | Index Now | Reason |
|--------|------------|-----------|--------|
| `crm_deals.name` | prefix, partial | Yes | Main deal identity |
| `crm_deals.company_name` | partial | Yes | Sales reps often remember the account before the deal name |
| `crm_deals.primary_contact_name` | prefix, partial | Defer | Useful, but noisier and not used by current deal list search |
| `crm_deals.primary_contact_email` | exact, prefix | Defer | Useful for future contact-to-deal lookup |

Ranking:

1. Exact deal name
2. Deal name prefix
3. Deal name contains
4. Company name contains
5. Primary contact match, if enabled later

Do not add owner search into deal browse by default. Owner already has a separate
owner filter API.

#### `deal_owner_filter`

Purpose: owner dropdown/filter, not deal search.

Search columns:

| Column | Match Type | Index Now | Reason |
|--------|------------|-----------|--------|
| `crm_deals.owner_name` | partial | Defer | Search happens on grouped distinct owners, likely small |
| `crm_deals.owner_email` | partial | Defer | Search happens on grouped distinct owners, likely small |
| `crm_deals.owner_id` | exact UUID | Existing btree path | Exact UUID does not need trigram |

Recommendation: do not add trigram owner indexes first. Measure before indexing
because owner fields change with deal ownership and are not the primary deal
search use case.

## Recommended First Migration

Create one migration for CRM search indexes, not separate migrations per entity.

Required:

```sql
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
```

Recommended first-pass trigram indexes:

```sql
-- Contacts: create-candidate + normal contact search
CREATE INDEX IF NOT EXISTS ix_crm_contact_search_email_trgm
ON crm_contacts USING gin (email gin_trgm_ops)
WHERE email IS NOT NULL AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_contact_search_full_name_trgm
ON crm_contacts USING gin (full_name gin_trgm_ops)
WHERE full_name IS NOT NULL AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_contact_search_additional_emails_trgm
ON crm_contacts USING gin ((additional_emails::text) gin_trgm_ops)
WHERE additional_emails <> '[]'::jsonb AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_contact_search_company_name_trgm
ON crm_contacts USING gin (company_name gin_trgm_ops)
WHERE company_name IS NOT NULL AND is_deleted = false;

-- Accounts: picker/table identity search
CREATE INDEX IF NOT EXISTS ix_crm_company_search_name_trgm
ON crm_companies USING gin (name gin_trgm_ops)
WHERE name IS NOT NULL AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_company_search_domain_trgm
ON crm_companies USING gin (domain gin_trgm_ops)
WHERE domain IS NOT NULL AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_company_search_website_trgm
ON crm_companies USING gin (website gin_trgm_ops)
WHERE website IS NOT NULL AND is_deleted = false;

-- Deals: primary deal search
CREATE INDEX IF NOT EXISTS ix_crm_deal_search_name_trgm
ON crm_deals USING gin (name gin_trgm_ops)
WHERE name IS NOT NULL AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_deal_search_company_name_trgm
ON crm_deals USING gin (company_name gin_trgm_ops)
WHERE company_name IS NOT NULL AND is_deleted = false;
```

Optional, add only if we intentionally support broader future lookup:

```sql
CREATE INDEX IF NOT EXISTS ix_crm_deal_search_primary_contact_name_trgm
ON crm_deals USING gin (primary_contact_name gin_trgm_ops)
WHERE primary_contact_name IS NOT NULL AND is_deleted = false;

CREATE INDEX IF NOT EXISTS ix_crm_deal_search_primary_contact_email_trgm
ON crm_deals USING gin (primary_contact_email gin_trgm_ops)
WHERE primary_contact_email IS NOT NULL AND is_deleted = false;
```

Defer:

```sql
-- Defer until measured:
-- crm_companies.industry
-- crm_companies.owner_name
-- crm_companies.owner_email
-- crm_deals.owner_name
-- crm_deals.owner_email
-- crm_deals.stage_name
-- crm_deals.pipeline_name
-- crm_deals.priority
-- crm_deals.lead_source
-- crm_contacts.first_name
-- crm_contacts.last_name
-- crm_contacts.job_title
-- crm_contacts.owner_name
-- crm_contacts.owner_email
```

Do not trigram-index numeric, date, enum, or UUID fields for this search work.
Use existing btree indexes and structured filters for:

- Deal status, pipeline, stage, owner, amount, close date
- Account owner, lifecycle, country, employee count, annual revenue
- Contact owner, lifecycle, lead status, marketing status, enrichment state

## API Shape

Keep the existing table endpoints. Add named search endpoints for real
workflows:

```http
GET /users/crm/contacts/search/create-candidates?q=&cursor=&limit=
GET /users/crm/contacts/search?q=&cursor=&limit=
GET /users/crm/accounts/search?q=&cursor=&limit=
GET /users/crm/deals/search?q=&cursor=&limit=
```

Rules:

- Cursor should encode ranking keys and entity id.
- Search responses should not return `total_count`; use `has_more`.
- Existing table list endpoints can keep page/total count for table UI.

## Gateway Responsibility

Gateway/frontend chooses intent:

- Contact create modal uses `/contacts/search/create-candidates`.
- Add-contact-to-deal picker and contact browse use `/contacts/search`.
- Account create modal and account selector use `/accounts/search`.
- Deal linker/search uses `/deals/search`.

Backend service owns:

- Exact columns per endpoint/function
- Ranking
- Limit bounds
- Cursor encoding
- Tenant/org isolation
- Exclusion of unindexed or noisy fields

## Why Not Elasticsearch Now

Postgres trigram search is enough for this stage because:

- Data is already in Postgres and tenant-scoped.
- Search is simple entity lookup/typeahead, not document search.
- We need exact-ish CRM record suggestions, not semantic retrieval.
- Elasticsearch adds sync, infra, failure modes, relevance tuning, and auth
  boundaries we do not need yet.

Revisit Elasticsearch or a dedicated search service if we need:

- Cross-object global search over contacts/accounts/deals/tasks/notes/emails.
- Highlighting/snippets.
- Complex typo tolerance and ranking analytics.
- Search across large unstructured text bodies.
- Multi-field scoring that Postgres can no longer handle cleanly.

## Downsides of Trigram Indexes

Trigram indexes are useful but not free:

- They use disk space.
- They slow writes/updates to indexed text columns.
- They make migrations slower on large tables.
- Too many indexes can increase vacuum and maintenance load.
- Indexes improve speed but do not improve relevance by themselves.

That is why the first pass should index only proven search fields.

## Proposed Rollout

1. Agree on named search paths and column scope.
2. Trim any uncommitted migration to the recommended first-pass indexes.
3. Implement named search service methods with tests.
4. Keep existing list APIs stable.
5. Measure query plans on one realistic tenant dataset.
6. Add deferred owner, industry, title, stage, and primary-contact deal indexes
   only if measured product workflows require those fields.

## Open Questions

- Should normal contact search rank company-name matches below identity matches? The
  recommended answer is yes.
- Is `crm_contacts.full_name` reliably populated for synced contacts? If not,
  fix ingestion or add a canonical `search_name`.
- Should deal search include primary contact name/email in the first picker, or
  wait until a concrete workflow needs it?
- Do we need normal list routes to call the same private helpers internally, or
  should they remain separate table-query code?

## Recommendation

Use named backend service methods/endpoints and start with the narrow first-pass
indexes:

- Contacts: `email`, `full_name`, `additional_emails`, `company_name`
- Accounts: `name`, `domain`, `website`
- Deals: `name`, `company_name`

Defer owner, stage, industry, title, and primary-contact deal search until a
specific screen needs those matches. This keeps the first version fast, simple,
production-safe, and still aligned with normal sales-rep lookup behavior.
