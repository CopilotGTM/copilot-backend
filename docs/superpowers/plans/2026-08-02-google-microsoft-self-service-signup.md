# Google and Microsoft Self-Service Signup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a verified Google Workspace or Microsoft Entra user to create a new CopilotGTM organisation and become its first approved organisation admin, while reusing the existing tenant-default, storage-container, worker, research, and authentication infrastructure.

**Architecture:** Keep sign-in and signup as explicit separate intents. The public signup endpoint verifies an existing Supabase OAuth session, persists one idempotent `SignupRequest`, and sends only its UUID to the existing organisation worker. The worker creates the organisation, founder, and external identity atomically in `public`, then invokes shared tenant/resource provisioning with the founder's real application user ID before activating the organisation.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLModel/SQLAlchemy, PostgreSQL/Alembic, Supabase Auth, Celery/Redis, Azure Blob Storage, Resend.

---

## Scope

Included:

- Google Workspace self-signup (`hd` required);
- Microsoft Entra work/school self-signup (`tid` required);
- organisation and founder profile collection;
- verified primary-domain uniqueness;
- durable retry/reconciliation;
- internal CRM defaults and founder CRM user;
- deterministic Azure organisation container;
- founding organisation admin assignment;
- existing research and welcome-email follow-up;
- frontend-ready API contract and error codes.

Excluded:

- password signup;
- personal Gmail or personal Microsoft signup;
- automatic domain joining;
- multiple organisations per user;
- multiple authentication methods per user;
- user invitations and 50-email bulk invitations. Those remain required, but are an independent
  subsystem and receive their own implementation plan after this signup plan is approved.

## Current flow that must be reused, not copied

```text
POST /admin/organisations/create_organisation
  -> OrganisationJob (platform-admin owned)
  -> organization.create Celery task
  -> OrganizationManagementService.create_organization()
       -> public.organisations status=provisioning
       -> InternalCRMSetupService
       -> Azure container org-<organisation_id>
       -> status=pending
  -> best-effort Kai Training research

POST /admin/users/create_user
  -> public.users
  -> InternalCRMSetupService again, idempotently
  -> tenant_data CRMUser
```

This cannot be called unchanged from public signup because it assumes a platform
`super_admin.admin_id`, creates user-owned CRM defaults before the founder exists, and deliberately
leaves the organisation pending for manual approval. Do not call the admin HTTP endpoint from the
signup service and do not create a fake super-admin.

## Target flow

```text
/signup -> Supabase Google/Microsoft OAuth
  -> POST /users/auth/{provider}/signup + Idempotency-Key
       -> remotely verify Supabase session
       -> reject consumer identity/existing account/existing primary domain
       -> persist SignupRequest(status=received)
       -> dispatch organization.signup.provision(signup_request_id only)
       -> 202 Accepted

organization.signup.provision
  -> transaction: organisation + founder admin + external identity
  -> tenant_data: internal CRM + pipeline/stages + founder CRMUser
  -> Azure: org-<organisation_id> container
  -> transaction: organisation=approved + signup_request=active
  -> best-effort welcome email and organisation research

GET /users/auth/signup-requests/{id}
  -> provider-session-owned durable status
  -> frontend enters onboarding only when status=active
```

## Folder structure and responsibilities

```text
src/
├── api/routers/users/
│   ├── auth_handler.py                    Existing sign-in only
│   └── signup_handler.py                  New thin signup/start/status endpoints
├── domain/models/
│   ├── user_signup_models.py              Public request/response schemas only
│   └── master_tables/
│       ├── signup_request_models.py       Durable signup state machine
│       ├── organisation_models.py         Verified primary-domain fields
│       ├── user_models.py                 Existing admin/member roles; no new owner role
│       └── external_identity_models.py    Existing immutable provider link
├── service/
│   ├── users/
│   │   ├── external_auth_service.py       Provider claim extraction and signup eligibility
│   │   ├── signup_service.py              Master/control-plane signup rules and transitions
│   │   └── user_tenant_setup_service.py   Shared app-user -> CRMUser/defaults setup
│   └── orgnisation/
│       ├── organization_management.py     Existing platform-admin orchestration
│       └── organisation_provisioning_service.py
│                                             Shared tenant/storage provisioning
├── infra/storage/
│   └── azure_org_blob_storage.py          Single Azure container implementation
└── workers/organization_worker/
    ├── client.py                          Dispatch by signup_request_id only
    ├── config.py                          Route new task to organization queue
    └── tasks/
        ├── __init__.py                    Register signup task module
        └── signup.py                      Retryable provisioning worker
```

Do not create provider-specific Google and Microsoft services. Both providers already normalize
through `ExternalAuthService`; provider branches belong there and converge on `SignupService`.

## Task 0: Prove the migration and runtime preconditions

**Files:**

- Read: `migrations/master_migrations/versions/f1a2b3c4d5ee_012_add_user_authentication_fields.py`
- Read: `src/domain/models/master_tables/organisation_models.py`
- Read: `src/service/orgnisation/organization_management.py`

- [ ] **Step 1: Confirm combined migration 012 has not been applied in any target environment**

Run this read-only query in local, dev, and the intended rollout environment:

```sql
SELECT version_num FROM public.alembic_version;
```

Expected: no environment reports `f1a2b3c4d5ee`. If any environment does, stop: do not edit
migration 012 and revise this plan to create migration 013 instead.

- [ ] **Step 2: Record current Alembic head**

Run:

```powershell
uv run alembic -c migrations/master_migrations/alembic.ini heads
```

Expected: `f1a2b3c4d5ee (head)`.

- [ ] **Step 3: Confirm storage and tenant runtime configuration exists in development**

Check only presence, never print values:

```powershell
uv run python -c "from src.infra.config.configs import cloud_storage_config; assert cloud_storage_config.ORGANISATION_STORAGE_NAME; print('storage-configured')"
```

Expected: `storage-configured`.

- [ ] **Step 4: Classify every existing active organisation domain before changing uniqueness**

Run read-only:

```sql
WITH existing_domains AS (
    SELECT
        o.organisation_id,
        o.organisation_name,
        lower(trim(trailing '.' FROM d.value)) AS normalized_domain
    FROM public.organisations AS o
    LEFT JOIN LATERAL json_array_elements_text(
        COALESCE(o.domain, '[]'::json)
    ) WITH ORDINALITY AS d(value, position) ON d.position = 1
    WHERE o.is_active IS TRUE
)
SELECT * FROM existing_domains ORDER BY normalized_domain NULLS FIRST, organisation_id;
```

Then run:

```sql
WITH existing_domains AS (
    SELECT lower(trim(trailing '.' FROM d.value)) AS normalized_domain
    FROM public.organisations AS o
    CROSS JOIN LATERAL json_array_elements_text(
        COALESCE(o.domain, '[]'::json)
    ) WITH ORDINALITY AS d(value, position)
    WHERE o.is_active IS TRUE AND d.position = 1
)
SELECT normalized_domain, count(*)
FROM existing_domains
GROUP BY normalized_domain
HAVING count(*) > 1;
```

Expected: every active organisation has a correct first domain and the duplicate query returns no
rows. Stop rollout and correct source organisation data if either condition fails; signup must not
launch while an existing organisation can be recreated under its own domain.

## Task 1: Add the durable signup and primary-domain schema

**Files:**

- Create: `src/domain/models/master_tables/signup_request_models.py`
- Modify: `src/domain/models/master_tables/organisation_models.py`
- Modify: `src/domain/models/master_tables/__init__.py`
- Modify: `migrations/master_migrations/env.py`
- Modify: `migrations/master_migrations/versions/f1a2b3c4d5ee_012_add_user_authentication_fields.py`
- Modify: `tests/migrations/test_user_authentication_method_migration.py`
- Create: `tests/migrations/test_external_signup_migration.py`

- [ ] **Step 1: Write a failing migration contract test**

The test must assert that combined migration 012:

```python
assert '"signup_requests"' in source
assert '"primary_domain"' in source
assert '"uq_organisations_primary_domain"' in source
assert '"uq_signup_provider_subject_tenant"' in source
assert 'drop_constraint("organisations_organisation_name_key"' in source
```

- [ ] **Step 2: Run the migration tests and verify red**

Run:

```powershell
uv run pytest tests/migrations/test_external_signup_migration.py tests/migrations/test_user_authentication_method_migration.py -q
```

Expected: failures because the signup table/domain fields are absent.

- [ ] **Step 3: Define the durable state model**

Create these exact enums and table responsibility in `signup_request_models.py`:

```python
class ExternalSignupProvider(str, Enum):
    GOOGLE_WORKSPACE = "google_workspace"
    MICROSOFT_ENTRA = "microsoft_entra"


class SignupRequestStatus(str, Enum):
    RECEIVED = "received"
    CONTROL_PLANE_CREATED = "control_plane_created"
    TENANT_DEFAULTS_CREATED = "tenant_defaults_created"
    STORAGE_CREATED = "storage_created"
    ACTIVE = "active"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"
```

`SignupRequest` must persist typed columns for:

```text
signup_request_id, idempotency_key_hash, request_fingerprint,
provider, supabase_user_id, provider_user_id, provider_tenant_id,
normalized_email, first_name, last_name, job_title,
organisation_name, primary_domain, country, sales_team_size,
website_url, linkedin_url, terms_version, privacy_version,
status, organisation_id, user_id, attempt_count,
last_error_code, last_error_at, completed_at, created_at, updated_at
```

Required uniqueness:

```python
UniqueConstraint("idempotency_key_hash", name="uq_signup_idempotency_key_hash")
UniqueConstraint(
    "provider", "provider_user_id", "provider_tenant_id",
    name="uq_signup_provider_subject_tenant",
)
```

Do not store OAuth tokens, raw idempotency keys, provider payloads, or exception bodies.

- [ ] **Step 4: Make organisation identity domain-based**

Add to `Organisation`:

```python
primary_domain: str | None
domain_verification_method: str | None
domain_verified_at: datetime | None
```

Remove global uniqueness from `organisation_name`. Add a nullable unique normalized
`primary_domain`; existing rows remain null and are classified/backfilled before enforcing signup.

- [ ] **Step 5: Extend combined migration 012**

Migration 012 must add the existing authentication fields plus the signup table and organisation
domain fields in one upgrade. Its downgrade must drop the signup table/indexes first, then domain
columns/constraints, then authentication fields. Do not change the revision ID or down revision.

After adding nullable `primary_domain`, backfill it from the normalized first entry of the existing
`domain` JSON list, then create `uq_organisations_primary_domain`. The Task 0 classification is the
deployment precondition that makes this deterministic; do not silently pick another domain or
discard duplicate organisations.

- [ ] **Step 6: Register the model and metadata**

Export `SignupRequest`, `SignupRequestStatus`, and `ExternalSignupProvider` from
`master_tables/__init__.py`, and add `SignupRequest` to the explicitly isolated master metadata in
`migrations/master_migrations/env.py`.

- [ ] **Step 7: Verify migration tests**

Run:

```powershell
uv run pytest tests/migrations/test_external_signup_migration.py tests/migrations/test_user_authentication_method_migration.py -q
```

Expected: all pass.

## Task 2: Define the frontend request and status contract

**Files:**

- Create: `src/domain/models/user_signup_models.py`
- Create: `tests/domain/models/test_user_signup_models.py`

- [ ] **Step 1: Write failing validation tests**

Cover:

```python
def test_signup_rejects_authority_fields(): ...  # user_role, status, organisation_id
def test_signup_normalizes_primary_domain(): ...
def test_signup_rejects_website_outside_primary_domain(): ...
def test_signup_requires_terms_and_privacy_versions(): ...
def test_signup_response_never_contains_provider_tokens(): ...
```

- [ ] **Step 2: Run the schema tests and verify red**

```powershell
uv run pytest tests/domain/models/test_user_signup_models.py -q
```

Expected: import failure because `user_signup_models.py` does not exist.

- [ ] **Step 3: Add strict request/response models**

Use this public shape:

```python
class ExternalSignupRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    job_title: str | None = Field(default=None, max_length=100)
    organisation_name: str = Field(min_length=2, max_length=255)
    primary_domain: str = Field(min_length=3, max_length=253)
    country: str = Field(min_length=2, max_length=2)
    sales_team_size: int | None = Field(default=None, ge=0, le=1_000_000)
    website_url: HttpUrl | None = None
    linkedin_url: HttpUrl | None = None
    terms_version: str = Field(min_length=1, max_length=32)
    privacy_version: str = Field(min_length=1, max_length=32)

    model_config = ConfigDict(extra="forbid")
```

Response data contains only:

```python
signup_request_id: UUID
status: SignupRequestStatus
organisation_id: UUID | None
user_id: UUID | None
```

The client never sends email, provider tenant, provider subject, authentication method, role,
status, IDs, or approval fields; derive all of them from the verified session or server policy.

- [ ] **Step 4: Verify schema tests**

```powershell
uv run pytest tests/domain/models/test_user_signup_models.py -q
```

Expected: all pass.

## Task 3: Add provider-specific signup eligibility to the existing auth service

**Files:**

- Modify: `src/service/users/external_auth_service.py`
- Create: `tests/service/users/test_external_signup_policy.py`

- [ ] **Step 1: Write failing Google and Microsoft policy tests**

Required cases:

```text
Google Workspace: verified email + matching hd -> accepted domain
Google without hd -> CONSUMER_ACCOUNT_NOT_SUPPORTED
Google hd different from email domain -> SIGNUP_IDENTITY_INVALID
Microsoft verified email + UUID tid -> accepted domain
Microsoft missing/invalid tid -> SIGNUP_IDENTITY_INVALID
outlook.com/hotmail.com/live.com -> CONSUMER_ACCOUNT_NOT_SUPPORTED
request primary_domain differs from derived domain -> SIGNUP_DOMAIN_MISMATCH
```

- [ ] **Step 2: Run the policy tests and verify red**

```powershell
uv run pytest tests/service/users/test_external_signup_policy.py -q
```

Expected: failure because signup policy does not exist.

- [ ] **Step 3: Add one provider-neutral signup policy method**

Add a result type and method to `ExternalAuthService`:

```python
@dataclass(frozen=True)
class ExternalSignupIdentity:
    claims: ExternalAuthClaims
    primary_domain: str


def validate_signup_identity(
    self,
    claims: ExternalAuthClaims,
    *,
    requested_primary_domain: str,
) -> ExternalSignupIdentity:
    ...
```

Google derives the domain from `claims.provider_tenant_id` (`hd`). Microsoft derives it from the
verified email suffix after `_tenant_id()` has validated `tid`. Normalize with lowercase, trim,
IDNA domain conversion, and trailing-dot removal. Return stable errors; never return raw provider
payloads.

- [ ] **Step 4: Verify existing sign-in policy remains unchanged**

```powershell
uv run pytest tests/service/users/test_external_signup_policy.py tests/api/routers/users/test_auth_handler.py -q
```

Expected: all pass; unknown identities through `/{provider}/complete` still receive invite-required
and do not create organisations.

## Task 4: Extract shared app-user tenant setup

**Files:**

- Create: `src/service/users/user_tenant_setup_service.py`
- Modify: `src/service/admin/admin_user_service.py`
- Modify: `tests/service/admin/test_admin_user_service_identity.py`
- Create: `tests/service/users/test_user_tenant_setup_service.py`

- [ ] **Step 1: Write a failing shared-service test**

The test must prove one call:

```python
result = await UserTenantSetupService(org_session).ensure_user(user_snapshot)
```

creates or reuses `InternalCRMSetupService`, creates exactly one `CRMUser` with
`user_id == app_user.user_id`, and is safe to repeat.

- [ ] **Step 2: Run focused tests and verify red**

```powershell
uv run pytest tests/service/users/test_user_tenant_setup_service.py -q
```

Expected: import failure because the service does not exist.

- [ ] **Step 3: Move, do not duplicate, existing logic**

Move the behavior currently owned by
`AdminUserService._ensure_internal_crm_for_new_app_user()` into the new service. Define a frozen
snapshot so no master session is held during tenant work:

```python
@dataclass(frozen=True)
class TenantUserSnapshot:
    user_id: UUID
    organisation_id: UUID
    email: str
    first_name: str
    last_name: str
    user_role: str
    job_title: str | None
    is_active: bool
```

Keep the existing compensation behavior in `AdminUserService`; it delegates creation to the shared
service and deletes the tenant CRM user if its master transaction later fails.

- [ ] **Step 4: Verify both callers' contract**

```powershell
uv run pytest tests/service/users/test_user_tenant_setup_service.py tests/service/admin/test_admin_user_service_identity.py -q
```

Expected: all pass.

## Task 5: Consolidate organisation tenant and storage provisioning

**Files:**

- Create: `src/service/orgnisation/organisation_provisioning_service.py`
- Modify: `src/service/orgnisation/organization_management.py`
- Modify: `src/infra/storage/azure_org_blob_storage.py`
- Modify: `src/api/routers/admin/organizations_handler.py`
- Modify: `src/workers/organization_worker/client.py`
- Modify: `src/workers/organization_worker/tasks/organization.py`
- Modify: `tests/infra/storage/test_azure_org_blob_storage.py`
- Modify: `tests/service/test_organization_management_shared_tenant.py`
- Modify: `tests/api/routers/admin/test_organizations_handler.py`
- Create: `tests/service/test_organisation_provisioning_service.py`

- [ ] **Step 1: Write failing provisioning tests**

Prove:

```text
founder tenant setup runs before storage completion is reported
storage uses get_org_container_name(organisation_id)
ContainerAlreadyExists is success
other Azure errors fail provisioning
retry creates no duplicate CRM defaults or CRM user
platform-admin organisation creation no longer seeds CRM with a super-admin UUID
platform-admin retries resolve by preallocated organisation UUID, never display name
```

- [ ] **Step 2: Run tests and verify red**

```powershell
uv run pytest tests/service/test_organisation_provisioning_service.py tests/infra/storage/test_azure_org_blob_storage.py -q
```

Expected: failures because the shared provisioning entrypoint is absent.

- [ ] **Step 3: Expose one strict async container provisioner**

Add to `AzureOrganisationBlobStorage`:

```python
async def provision_container(self) -> None:
    """Create the deterministic organisation container or confirm it exists."""
```

It must reuse the class's credential/client lifecycle, treat Azure
`ResourceExistsError`/`ContainerAlreadyExists` as success, and raise other errors. Replace the
duplicate synchronous Azure SDK code in `OrganizationManagementService` with this method.

- [ ] **Step 4: Add the shared orchestration service**

```python
class OrganisationProvisioningService:
    async def provision_founder(
        self,
        organisation_id: UUID,
        founder: TenantUserSnapshot,
    ) -> None:
        ...
```

It acquires the tenant session only for `UserTenantSetupService`, releases it, then provisions
storage. It never holds a database session during Azure I/O.

- [ ] **Step 5: Correct the platform-admin creation order**

`OrganizationManagementService.create_organization()` should create the organisation and storage
container, but defer user-owned internal CRM defaults until the first real application user is
created. `AdminUserService` already guarantees those defaults through Task 4.

- [ ] **Step 6: Replace display-name idempotency in the existing admin path**

Removing global name uniqueness makes the current lookup-by-name retry unsafe. Generate one
`organisation_id` when the admin `OrganisationJob` is created, persist it on the job, and pass that
UUID through `send_organization_create_task()` and `organization.create` into:

```python
async def create_organization(
    self,
    organisation_id: UUID,
    admin_id: UUID,
    organisation_name: str,
    **org_details: Any,
) -> dict[str, Any]:
    ...
```

Retries load by `organisation_id`. A different existing row with the same display name is allowed;
a conflicting verified `primary_domain` is not. This preserves admin-job idempotency after the name
constraint is removed.

- [ ] **Step 7: Verify provisioning tests**

```powershell
uv run pytest tests/service/test_organisation_provisioning_service.py tests/service/test_organization_management_shared_tenant.py tests/infra/storage/test_azure_org_blob_storage.py tests/service/admin/test_admin_user_service_identity.py -q
```

Expected: all pass.

## Task 6: Implement durable control-plane signup transitions

**Files:**

- Create: `src/service/users/signup_service.py`
- Create: `tests/service/users/test_signup_service.py`

- [ ] **Step 1: Write failing concurrency and repeat-signup tests**

Required cases:

```text
same idempotency key + same fingerprint -> same SignupRequest
same idempotency key + different body -> IDEMPOTENCY_KEY_REUSED
same provider subject + same domain while running -> same SignupRequest
same provider subject + same domain before control-plane creation -> profile-only fields may update
same provider subject after control-plane creation -> identity/domain immutable, resume only
existing provider mapping -> ACCOUNT_ALREADY_EXISTS
existing normalized email -> ACCOUNT_ALREADY_EXISTS
existing primary domain -> ORGANISATION_ACCESS_REQUIRED
same name + different primary domain -> allowed
two concurrent requests for same domain -> one organisation
control-plane retry -> same organisation/user/mapping IDs
existing user attempting another organisation -> ACCOUNT_IN_OTHER_ORGANISATION
```

- [ ] **Step 2: Run the service tests and verify red**

```powershell
uv run pytest tests/service/users/test_signup_service.py -q
```

Expected: import failure because `SignupService` does not exist.

- [ ] **Step 3: Implement `start_signup()`**

Define:

```python
class SignupService:
    def __init__(self, session: AsyncSession): ...

    async def start_signup(
        self,
        *,
        identity: ExternalSignupIdentity,
        request: ExternalSignupRequest,
        idempotency_key: str,
    ) -> SignupRequest:
        ...
```

Hash the idempotency key with SHA-256, calculate a canonical request fingerprint, and acquire
transaction-scoped advisory locks for the provider subject and primary domain before reading or
creating. Compare existing intent on replay. Do not log email, provider tokens, or request bodies.

- [ ] **Step 4: Implement atomic control-plane creation**

Define:

```python
async def ensure_control_plane(
    self,
    signup_request_id: UUID,
) -> tuple[SignupRequest, Organisation, User, TenantUserSnapshot]:
    ...
```

Under one master transaction and row lock, create/reuse:

```text
Organisation(status=provisioning, primary_domain=<verified>)
User(user_role=admin, organisation_role="Administrator",
     status=ACTIVE, admin_status=APPROVED, email_verified=True)
ExternalIdentityMapping(provider subject + tenant -> founder user)
UserActionHistory and OrganisationActionHistory with source=self_service_signup
```

Set `created_by_auth_user_id` to the verified Supabase user ID, not a platform admin ID. Advance to
`control_plane_created` only after commit.

- [ ] **Step 5: Implement monotonic state transitions**

Add methods that lock the request and permit only:

```text
received -> control_plane_created -> tenant_defaults_created
-> storage_created -> active
```

Retries may re-enter the current or an earlier proven phase but may never move state backward.
Failures store a stable local code and timestamp, then use `reconciliation_required` when work is
retryable.

- [ ] **Step 6: Verify service tests**

```powershell
uv run pytest tests/service/users/test_signup_service.py -q
```

Expected: all pass, including the concurrent-domain test against PostgreSQL.

## Task 7: Add the retryable organisation-worker signup task

**Files:**

- Create: `src/workers/organization_worker/tasks/signup.py`
- Modify: `src/workers/organization_worker/tasks/__init__.py`
- Modify: `src/workers/organization_worker/config.py`
- Modify: `src/workers/organization_worker/client.py`
- Create: `tests/workers/organization_worker/test_signup_task.py`

- [ ] **Step 1: Write a failing phase-resume test**

The test must invoke the task repeatedly from every persisted state and assert that completed
phases are not duplicated. It must also assert the Celery payload is exactly:

```json
{"signup_request_id": "<uuid>"}
```

- [ ] **Step 2: Run worker tests and verify red**

```powershell
uv run pytest tests/workers/organization_worker/test_signup_task.py -q
```

Expected: task registration failure.

- [ ] **Step 3: Register and route the task**

Use:

```python
@app.task(
    name="organization.signup.provision",
    bind=True,
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def provision_signup_task(self, signup_request_id: str) -> dict[str, str]:
    ...
```

Add its route to the existing `organization` queue and import `signup` in `tasks/__init__.py`.

- [ ] **Step 4: Implement phase-by-phase worker orchestration**

The task reloads server-trusted data for each phase, calls `ensure_control_plane()`, invokes
`OrganisationProvisioningService`, records tenant/storage milestones, and finally marks both the
organisation and signup active in one master transaction. Do not mark active if tenant setup or
storage failed.

- [ ] **Step 5: Handle dispatch and worker failure safely**

`send_signup_provisioning_task()` accepts only the request UUID. If broker dispatch fails, the API
returns a retryable service error while retaining `received`; replaying the same request and
idempotency key dispatches it again. Celery retry uses exponential backoff. Exhausted retries leave
`reconciliation_required`, not a misleading successful or permanently deleted request.

- [ ] **Step 6: Verify worker tests**

```powershell
uv run pytest tests/workers/organization_worker/test_signup_task.py -q
```

Expected: all pass.

## Task 8: Expose explicit signup and owned status endpoints

**Files:**

- Create: `src/api/routers/users/signup_handler.py`
- Modify: `src/api/fastapi_app.py`
- Modify: `src/api/dependencies/rate_limit.py`
- Create: `tests/api/routers/users/test_signup_handler.py`

- [ ] **Step 1: Write failing endpoint tests**

Cover exact routes:

```text
POST /users/auth/google/signup
POST /users/auth/microsoft/signup
GET  /users/auth/signup-requests/{signup_request_id}
```

Assert missing bearer token, missing/short `Idempotency-Key`, wrong provider, consumer account,
domain mismatch, existing account, existing domain, success `202`, replay, cross-user status read,
and rate limit behavior.

- [ ] **Step 2: Run endpoint tests and verify red**

```powershell
uv run pytest tests/api/routers/users/test_signup_handler.py -q
```

Expected: 404 for the new routes.

- [ ] **Step 3: Add thin handlers**

The POST handler performs only:

```text
extract bearer -> verify Supabase session -> extract provider claims
-> validate signup identity/domain -> validate Idempotency-Key
-> SignupService.start_signup -> dispatch request UUID -> 202
```

The status handler remotely verifies the same Supabase identity and returns a request only when
its `supabase_user_id` and provider subject match. A UUID alone grants no access.

- [ ] **Step 4: Add a dedicated signup limiter**

Add `rate_limit_signup` with both verified client-IP and hashed identity dimensions. It fails
closed when the limiter backend is unavailable. Do not put raw emails in Redis keys.

- [ ] **Step 5: Register the router without changing sign-in routes**

Include `signup_handler.router` under `/users/auth` in `src/api/fastapi_app.py`. Keep existing
`POST /users/auth/{provider}/complete` as sign-in completion; do not make it create users.

- [ ] **Step 6: Verify endpoint and regression tests**

```powershell
uv run pytest tests/api/routers/users/test_signup_handler.py tests/api/routers/users/test_auth_handler.py tests/infra/auth/test_user_auth_policy.py -q
```

Expected: all pass.

## Task 9: Finish activation, welcome email, and research behavior

**Files:**

- Modify: `src/infra/auth/email_service.py`
- Modify: `src/workers/organization_worker/tasks/signup.py`
- Modify: `tests/infra/auth/test_email_service.py`
- Modify: `tests/workers/organization_worker/test_signup_task.py`

- [ ] **Step 1: Write failing post-activation tests**

Prove:

```text
welcome email is attempted only after status=active
email failure does not roll back an active organisation
research is queued only after status=active
research failure is recorded but does not disable the organisation
no second email-verification message is sent
```

- [ ] **Step 2: Run tests and verify red**

```powershell
uv run pytest tests/infra/auth/test_email_service.py tests/workers/organization_worker/test_signup_task.py -q
```

Expected: new assertions fail.

- [ ] **Step 3: Reuse existing delivery and research components**

Add one provider-neutral welcome template that links to `/signin` or onboarding and never includes
tokens. After activation, reuse `KaiTrainingService.mark_pending()` and
`send_organization_research_task()` exactly as the current organisation worker does.

- [ ] **Step 4: Verify post-activation behavior**

```powershell
uv run pytest tests/infra/auth/test_email_service.py tests/workers/organization_worker/test_signup_task.py -q
```

Expected: all pass.

## Task 10: Document the frontend contract and run production gates

**Files:**

- Modify: `docs/API Documentation/users-auth-api.md`
- Modify: `docs/API Documentation/external-auth-api.md`
- Modify: `docs/Technical Documentation/self-service-signup-and-organisation-invitations.md`
- Modify: `src/workers/organization_worker/README.md`

- [ ] **Step 1: Document the exact frontend sequence**

Document `/signup` -> provider OAuth -> details form -> POST -> durable polling -> onboarding,
including request/response examples and every stable error code. Explicitly state that `/signin`
never creates an organisation.

- [ ] **Step 2: Run the focused automated matrix**

```powershell
uv run pytest tests/migrations/test_external_signup_migration.py tests/migrations/test_user_authentication_method_migration.py tests/domain/models/test_user_signup_models.py tests/service/users/test_external_signup_policy.py tests/service/users/test_user_tenant_setup_service.py tests/service/users/test_signup_service.py tests/service/test_organisation_provisioning_service.py tests/service/test_organization_management_shared_tenant.py tests/service/admin/test_admin_user_service_identity.py tests/infra/storage/test_azure_org_blob_storage.py tests/workers/organization_worker/test_signup_task.py tests/api/routers/users/test_signup_handler.py tests/api/routers/users/test_auth_handler.py tests/infra/auth/test_user_auth_policy.py tests/infra/auth/test_email_service.py -q
```

Expected: all pass.

- [ ] **Step 3: Run static checks**

```powershell
uv run ruff check src/api/routers/users/signup_handler.py src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/domain/models/master_tables/organisation_models.py src/service/users/external_auth_service.py src/service/users/signup_service.py src/service/users/user_tenant_setup_service.py src/service/orgnisation/organisation_provisioning_service.py src/service/orgnisation/organization_management.py src/infra/storage/azure_org_blob_storage.py src/workers/organization_worker/tasks/signup.py tests/domain/models/test_user_signup_models.py tests/service/users/test_external_signup_policy.py tests/service/users/test_signup_service.py tests/service/users/test_user_tenant_setup_service.py tests/service/test_organisation_provisioning_service.py tests/workers/organization_worker/test_signup_task.py tests/api/routers/users/test_signup_handler.py
uv run mypy src/api/routers/users/signup_handler.py src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/service/users/external_auth_service.py src/service/users/signup_service.py src/service/users/user_tenant_setup_service.py src/service/orgnisation/organisation_provisioning_service.py src/workers/organization_worker/tasks/signup.py
```

Expected: no errors.

- [ ] **Step 4: Run non-production OAuth and failure rehearsal**

Verify both Google Workspace and Microsoft Entra with:

```text
new founder success
consumer account rejection
existing account
existing domain
same-name/different-domain organisation
callback replay
concurrent same-domain signup
broker unavailable then idempotent retry
worker killed after every durable milestone
tenant database failure
Azure container failure and ContainerAlreadyExists retry
welcome-email failure
research failure
suspended organisation/user at refresh and /me
```

Record request IDs, worker task IDs, resulting organisation/user IDs, migration revision, and
post-state. No production rollout is approved by code completion alone.

- [ ] **Step 5: Review the final diff without staging unrelated work**

```powershell
git diff --check
git status --short
git diff --name-only
```

Expected: no whitespace errors; only explicitly approved signup/auth/provisioning paths are part of
this implementation slice. Do not commit, push, migrate, or deploy until separately requested.

## Walking skeleton

The first executable slice is deliberately narrow:

```text
Google Workspace session
-> POST /users/auth/google/signup
-> SignupRequest
-> worker creates organisation + admin + identity
-> founder tenant defaults + Azure container
-> active
-> GET /me succeeds
```

Build and prove this slice through Task 8 before adding Microsoft-specific test fixtures, welcome
email, research, or frontend polish. Microsoft then uses the same path with only provider claim
normalization differing.

## Approval gate

Implementation must not start until these product decisions are approved:

1. V1 signup is Google Workspace and Microsoft Entra only; consumer accounts are rejected.
2. The verified primary domain, not display name, identifies an existing organisation.
3. The first user receives the existing organisation `admin` role; no new owner role is added.
4. Same-domain users require an invitation; signup never auto-joins.
5. Organisation activation requires tenant defaults and Azure container success.
6. One user can belong to only one organisation in V1.
