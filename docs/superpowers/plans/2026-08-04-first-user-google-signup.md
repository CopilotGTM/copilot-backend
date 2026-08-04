# First-User Google Signup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first-user Google signup resumable and reduce its public form to one editable `organisation_name` field while reusing the existing durable provisioning pipeline.

**Architecture:** `PUT /users/auth/google/signup/draft` verifies the Supabase Google identity and creates or resumes a server-side `DRAFT`. `POST /users/auth/google/signup` re-verifies that identity, finalizes the same draft under the existing subject and domain locks, records server-selected policy versions, and dispatches the existing worker only after the state becomes `RECEIVED`. The existing control-plane, tenant, storage, and activation milestones remain unchanged.

**Tech Stack:** FastAPI, Pydantic v2, SQLModel/SQLAlchemy async, PostgreSQL advisory locks, Alembic, pytest.

---

### Task 1: Lock the one-field public contract with failing tests

**Files:**
- Modify: `tests/api/routers/users/test_signup_handler.py`
- Modify: `tests/service/users/test_signup_service.py`

- [ ] **Step 1: Replace the API request fixture with the one-field payload**

```python
def _request() -> dict[str, object]:
    return {"organisation_name": "Acme"}
```

- [ ] **Step 2: Add a preflight test that expects a durable draft and provider-derived profile**

```python
def test_signup_preflight_creates_resumable_draft(monkeypatch) -> None:
    response = client.get(
        "/users/auth/google/signup",
        headers={"Authorization": "Bearer a.b.c"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "draft"
    assert response.json()["data"]["email"] == "founder@acme.com"
```

- [ ] **Step 3: Add request-boundary tests rejecting personal and authority fields**

```python
@pytest.mark.parametrize("field", ["first_name", "country", "terms_version", "primary_domain"])
def test_signup_rejects_every_field_except_organisation_name(monkeypatch, field) -> None:
    request = _request() | {field: "attacker-controlled"}
    response = client.post("/users/auth/google/signup", headers=auth_headers, json=request)
    assert response.status_code == 422
```

- [ ] **Step 4: Add service tests for draft creation, draft resume, and finalization**

```python
draft = await SignupService(session).get_or_create_draft(identity=_identity())
assert draft.status == SignupRequestStatus.DRAFT
assert draft.organisation_name is None

finalized = await SignupService(session).finalize_signup(
    identity=_identity(), request=ExternalSignupRequest(organisation_name="Acme")
)
assert finalized.status == SignupRequestStatus.RECEIVED
assert finalized.organisation_name == "Acme"
```

- [ ] **Step 5: Run the focused tests and verify RED**

Run: `uv run pytest tests/api/routers/users/test_signup_handler.py tests/service/users/test_signup_service.py -q`

Expected: failures because `DRAFT`, `get_or_create_draft`, `finalize_signup`, and the one-field request contract do not exist.

### Task 2: Add the minimal durable draft model

**Files:**
- Modify: `src/domain/models/user_signup_models.py`
- Modify: `src/domain/models/master_tables/signup_request_models.py`
- Modify: `migrations/master_migrations/versions/f1a2b3c4d5ee_012_add_user_authentication_fields.py`
- Modify: `tests/migrations/test_external_signup_migration.py`

- [ ] **Step 1: Reduce the public write model and expand the read model**

```python
class ExternalSignupRequest(BaseModel):
    organisation_name: str = Field(min_length=2, max_length=255)
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class ExternalSignupProfile(BaseModel):
    signup_request_id: UUID
    status: SignupRequestStatus
    provider: str
    email: str
    primary_domain: str
    full_name: str | None
    profile_image_url: str | None
    organisation_name: str | None
```

- [ ] **Step 2: Add `DRAFT` and permit draft-only incomplete columns**

```python
class SignupRequestStatus(str, Enum):
    DRAFT = "draft"
    RECEIVED = "received"

organisation_name: str | None
country: str | None
terms_version: str | None
privacy_version: str | None
profile_image_url: str | None
profile_incomplete: bool = False
```

- [ ] **Step 3: Keep revision 012 internally consistent because its contract marks it unreleased**

Update the `signup_requests` table definition so `organisation_name`, `country`, `terms_version`, and `privacy_version` are nullable, add nullable `profile_image_url`, add non-null `profile_incomplete` with default false, and change the status default to `draft`.

- [ ] **Step 4: Run model and migration tests**

Run: `uv run pytest tests/migrations/test_external_signup_migration.py tests/service/users/test_signup_service.py -q`

Expected: model imports succeed; service behavior remains RED until Task 3.

### Task 3: Reuse SignupService for draft creation and finalization

**Files:**
- Modify: `src/service/users/signup_service.py`
- Test: `tests/service/users/test_signup_service.py`

- [ ] **Step 1: Add provider-profile fallback parsing**

```python
def _founder_profile(identity: ExternalSignupIdentity) -> tuple[str, str, bool]:
    full_name = (identity.claims.full_name or "").strip()
    if full_name:
        first_name, _, last_name = full_name.partition(" ")
        return first_name, last_name, not bool(last_name)
    return identity.claims.email.split("@", 1)[0], "", True
```

- [ ] **Step 2: Add `get_or_create_draft(identity)`**

Under the existing provider-subject advisory lock, return the same subject's existing request or insert one `DRAFT`. Generate the internal idempotency hash from the verified provider subject; do not accept it from the browser. Store provider-derived email, name, tenant, domain, avatar completeness marker, and no organisation name. Do not take the domain lock and do not query/reserve an organisation domain at this stage.

- [ ] **Step 3: Replace `start_signup` with `finalize_signup(identity, request)`**

Acquire both existing subject and domain locks, reload the subject draft, return any already-finalized state unchanged, perform the existing mapping/user/organisation/domain conflict checks, set `organisation_name`, server-selected policy versions, and status `RECEIVED`, then commit. A competing `DRAFT` on the same domain must not block finalization; a non-failed finalized request must.

- [ ] **Step 4: Prevent workers from provisioning drafts**

```python
if signup.status == SignupRequestStatus.DRAFT:
    raise SignupServiceError("Signup has not been submitted", code="SIGNUP_STATE_INVALID")
```

- [ ] **Step 5: Persist provider profile data on the founder**

Set `User.profile_image_url` from the trusted provider claim and record `preferences={"profile_incomplete": True}` only when the fallback was required.

- [ ] **Step 6: Run the service tests and verify GREEN**

Run: `uv run pytest tests/service/users/test_signup_service.py -q`

Expected: all signup service tests pass.

### Task 4: Wire draft creation, finalization, resume, and dispatch

**Files:**
- Modify: `src/api/routers/users/signup_handler.py`
- Modify: `tests/api/routers/users/test_signup_handler.py`

- [ ] **Step 1: Make the idempotent draft PUT create or resume the draft**

```python
async with master_async_session_scope() as session:
    signup = await SignupService(session).get_or_create_draft(identity=identity)
```

Return the draft UUID/status, verified email/domain/name/avatar, and current organisation name.

- [ ] **Step 2: Make POST finalize without a browser idempotency header**

```python
signup = await SignupService(session).finalize_signup(
    identity=identity,
    request=signup_request,
)
```

Dispatch whenever the returned state is `RECEIVED`. This is intentionally at-least-once: if the
database commit succeeds but queue delivery fails or times out, a repeated POST repairs dispatch.
Worker row locks and monotonic milestones make duplicate queue delivery safe.

- [ ] **Step 3: Run route tests and verify GREEN**

Run: `uv run pytest tests/api/routers/users/test_signup_handler.py -q`

Expected: all route tests pass.

### Task 5: Align documentation and verify the complete backend slice

**Files:**
- Modify: `docs/API Documentation/users-auth-api.md`
- Modify: `docs/Technical Documentation/first-user-self-service-signup-flow.md`

- [ ] **Step 1: Document the draft PUT and one-field POST contract**

```json
{ "organisation_name": "Swiftsell AI" }
```

Document `draft -> received -> control_plane_created -> tenant_defaults_created -> storage_created -> active` and remove browser-selected identity, profile, policy-version, website, country, and idempotency inputs.

- [ ] **Step 2: Mark the first-user design as implemented**

Update the status line without expanding the second-user/member flow.

- [ ] **Step 3: Run focused verification**

Run: `uv run pytest tests/api/routers/users/test_signup_handler.py tests/service/users/test_signup_service.py tests/workers/organization_worker/test_signup_task.py tests/migrations/test_external_signup_migration.py -q`

Expected: all tests pass.

- [ ] **Step 4: Run static checks on touched Python files**

Run: `uv run ruff check src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/service/users/signup_service.py src/api/routers/users/signup_handler.py tests/api/routers/users/test_signup_handler.py tests/service/users/test_signup_service.py tests/migrations/test_external_signup_migration.py`

Run: `uv run mypy src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/service/users/signup_service.py src/api/routers/users/signup_handler.py`

Expected: both commands pass.

- [ ] **Step 5: Verify worktree hygiene**

Run: `git diff --check` and `git status --short`.

Expected: no whitespace errors; pre-existing staged files remain staged and task-owned changes remain clearly identifiable. Do not stage, commit, migrate a database, push, or deploy.
