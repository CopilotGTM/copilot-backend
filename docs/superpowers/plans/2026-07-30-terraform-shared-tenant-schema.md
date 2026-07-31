# Terraform Shared Tenant Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Terraform own the permanent `tenant_data` schema while the
existing organisation Alembic chain can migrate any explicitly named schema.

**Architecture:** The PostgreSQL Terraform provider declaratively creates
`tenant_data` once in dev and prod and protects it from deletion. Alembic never
creates schemas; `scripts.run_org_migrations --schema <name>` validates an
explicit PostgreSQL identifier and runs the existing chain against that
already-existing schema. Canonical `org_*` backfills still require their
matching organisation UUID, while any other empty schema is a valid shared-row
test target.

**Tech Stack:** Terraform, PostgreSQL provider, Alembic, Python 3.11, pytest

---

### Task 1: Lock the Terraform ownership contract

**Files:**

- Create: `tests/infra/test_shared_tenant_schema_terraform.py`
- Modify: `infra/terraform/azure/dev/provider.tf`
- Modify: `infra/terraform/azure/dev/main.tf`
- Modify: `infra/terraform/azure/prod/provider.tf`
- Modify: `infra/terraform/azure/prod/main.tf`

- [x] Write a failing static contract proving dev and prod declare the
  `cyrilgdn/postgresql` provider, configure it with the existing server
  connection, and manage exactly one protected `postgresql_schema
  "tenant_data"` resource.
- [x] Run the contract and verify it fails because the provider and resource do
  not exist.
- [x] Add provider version `1.26.0`, SSL-required admin configuration, and:

  ```hcl
  resource "postgresql_schema" "tenant_data" {
    name          = "tenant_data"
    database      = "postgres"
    owner         = module.application_database.db_user
    if_not_exists = true
    drop_cascade  = false

    lifecycle {
      prevent_destroy = true
    }

    depends_on = [module.application_database]
  }
  ```

- [x] Run `terraform fmt -check` and the Terraform contract.

### Task 2: Add one explicit schema migration path

**Files:**

- Modify: `tests/scripts/test_run_org_migrations.py`
- Modify: `scripts/run_org_migrations.py`

- [x] Write failing tests proving `--schema tenant_data` and another safe test
  schema call `migration_manager.run_organization_migration()` with no
  organisation UUID, precheck does not mutate, unsafe identifiers fail, and
  `--schema` cannot be mixed with organisation IDs or `--all`.
- [x] Run the focused tests and verify the new schema-path tests fail.
- [x] Add `--schema` to the existing CLI and one
  `run_migration_for_schema()` path. Reuse the existing migration manager;
  retain legacy ID and `--all` behavior unchanged.
- [x] Run the focused script tests.

### Task 3: Keep schema creation out of Alembic

**Files:**

- Modify: `tests/migrations/org/test_082_add_missing_organisation_identity.py`
- Modify: `migrations/org_migrations/tenant_identity.py`
- Modify: `migrations/org_migrations/versions/3c7f79637717_082_add_missing_organisation_identity.py`
- Modify: `migrations/org_migrations/env.py`

- [x] Write failing tests proving a non-`org_*` schema without an organisation
  UUID is accepted as an empty shared-row target, canonical `org_*` schemas
  still require and validate their UUID, and Alembic emits no `CREATE SCHEMA`.
- [x] Run the focused migration tests and verify red.
- [x] Generalize the empty-target validation and error text without weakening
  canonical source-schema identity checks.
- [x] Remove offline `CREATE SCHEMA IF NOT EXISTS`; online mode already assumes
  the schema exists.
- [x] Run migration identity, runner, and head contracts.

### Task 4: Verify the corrected Task 6 boundary

- [x] Run Ruff on changed Python files.
- [x] Run `terraform fmt -check` for dev and prod.
- [x] Run focused Terraform, runner, identity, migration-head, and Task 5
  contracts.
- [x] Run `git diff --check` and confirm earlier staged/unstaged work remains
  preserved.
- [x] Do not run Terraform apply, Alembic against a live database, commit, push,
  or deploy without separate approval.
