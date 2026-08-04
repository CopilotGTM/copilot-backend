# Domain Membership Signup Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let verified Google Workspace and Microsoft Entra users join an existing same-domain organisation as members by default, while allowing organisation admins to switch to invite-only membership and invite members in either mode.

**Architecture:** Keep organisation creation and existing-organisation membership as separate backend actions. Signup preflight becomes a read/decision boundary that returns a server-owned flow; a dedicated idempotent join endpoint performs membership provisioning. Reuse `public.signup_requests` as the durable identity/idempotency record and `public.organisations.preferences` for the two-value policy, so this slice needs no database migration or new dependency.

**Tech Stack:** FastAPI, Pydantic, SQLModel/SQLAlchemy async sessions, PostgreSQL advisory locks, existing shared-tenant CRM setup, Supabase verified OAuth sessions, existing email service, Pytest.

---

## Decisions and invariants

1. `membership.domain_join_policy` has exactly two values: `open` and `invite_only`.
2. A missing policy normalizes to `open`. This applies to new and existing organisations, matching the product decision that same-domain signup is open unless an admin blocks it.
3. No extra policy step is added to first-user organisation creation. The founder can change the policy later through an organisation-admin-only endpoint.
4. Provider verification remains unchanged: Google requires verified Workspace `hd`; Microsoft requires a verified work identity and Entra tenant UUID. Consumer accounts remain rejected.
5. Domain matching is always performed on the backend against `Organisation.primary_domain`; the browser never selects an organisation or sends a domain.
6. Automatic joining always creates `UserAppRole.MEMBER`, `organisation_role="Member"`, `SupabaseUserStatus.ACTIVE`, and `AdminUserStatus.APPROVED`. Public signup can never grant admin.
7. A valid admin-created invitation is represented by an existing `public.users` row. It is honored in both policies and the OAuth callback proceeds through the existing `/auth/{provider}/complete` identity-linking path.
8. `invite_only` creates no user, external identity mapping, or CRM user. The durable signup request advances once to `invitation_required`, and active organisation admins receive one best-effort notification for that transition.
9. Automatic joining creates the master user, external identity mapping, audit events, and tenant CRM user idempotently. It never creates or modifies an organisation.
10. Email delivery happens after durable state commits. Delivery failure is logged and does not roll back membership or make the join endpoint unsafe to retry.
11. Invitations remain available in both policies and are restricted to authenticated organisation admins. V1 organisation-admin invitations create members only; admin promotion remains a separate existing lifecycle action.
12. Frontend changes are explicitly out of scope for this plan. Backend responses must be complete enough for the later frontend plan.

## Case matrix

| Case | Preflight flow | Join result | Side effects |
| --- | --- | --- | --- |
| No organisation owns domain | `create_organisation` | Join endpoint rejects | Existing founder flow only |
| Existing organisation, missing/`open` policy | `join_existing_organisation` | `active` | Member, mapping, CRM user, audit, admin notification |
| Existing organisation, `invite_only`, no invitation | `invitation_required` | `invitation_required` | No user; one admin notification |
| Existing invited user with matching auth method | `sign_in` | Not called | Existing `/complete` links identity and signs in |
| Existing mapped/active user | `sign_in` | Not called | Normal sign-in only |
| Existing submitted organisation signup | `resume_signup` | Not called | Existing status polling/provisioning only |
| Existing organisation suspended/inactive | `organisation_unavailable` | Reject `ORGANISATION_INACTIVE` | None |
| Same subject retries/concurrent calls | Same durable result | Same user/request | No duplicate user, mapping, CRM user, or email transition |

## File map

**Create**

- `src/api/schemas/membership_models.py` — membership policy and organisation-admin invitation request/response schemas.
- `src/service/orgnisation/membership_policy_service.py` — read/update the organisation policy.
- `src/service/users/organisation_membership_service.py` — existing-domain join and external member invitation provisioning.
- `src/api/routers/app_preferences/membership_handler.py` — admin-only policy API.
- `src/api/routers/users/membership_handler.py` — organisation-admin invitation API.
- `tests/service/users/test_organisation_membership_service.py` — join/invitation business rules and idempotency.
- `tests/service/orgnisation/test_membership_policy_service.py` — policy persistence.
- `tests/api/routers/app_preferences/test_membership_handler.py` — policy authorization/API contract.
- `tests/api/routers/users/test_membership_handler.py` — organisation-admin invitation API.

**Modify**

- `src/domain/models/master_tables/signup_request_models.py` — add the application-level `INVITATION_REQUIRED` status; column remains varchar.
- `src/domain/models/user_signup_models.py` — add preflight flow and nullable request ID for `sign_in`.
- `src/service/orgnisation/organisation_preferences.py` — normalize/merge membership policy.
- `src/api/dependencies/organisation_dependencies.py` — central organisation-admin dependency.
- `src/api/routers/chat_attachments/catalog_handler.py` — reuse the central admin dependency instead of its local copy.
- `src/service/users/signup_service.py` — server-owned preflight resolution and durable target organisation binding.
- `src/api/routers/users/signup_handler.py` — expose preflight flow and idempotent join endpoint.
- `src/infra/auth/email_service.py` — member-joined, access-attempt, and invitation notification templates.
- `src/api/routers/app_preferences/__init__.py` and `src/api/fastapi_app.py` — register new routers.
- `tests/service/test_organisation_preferences.py` — normalization/merge regression coverage.
- `tests/service/users/test_signup_service.py` — preflight case matrix.
- `tests/api/routers/users/test_signup_handler.py` — Google/Microsoft public API cases.
- `tests/infra/auth/test_email_service.py` — notification content and recipients.
- `docs/API Documentation/users-auth-api.md` — public preflight/join contract.
- `docs/Technical Documentation/self-service-signup-and-organisation-invitations.md` — replace the old invite-only V1 statement with the implemented policy.

---

### Task 1: Add the membership policy contract and shared admin guard

**Files:**

- Create: `src/api/schemas/membership_models.py`
- Modify: `src/service/orgnisation/organisation_preferences.py`
- Modify: `src/api/dependencies/organisation_dependencies.py`
- Modify: `src/api/routers/chat_attachments/catalog_handler.py`
- Modify: `tests/service/test_organisation_preferences.py`
- Test: `tests/api/dependencies/test_organisation_admin_dependency.py`

- [ ] **Step 1: Write failing preference tests**

Add these cases to `tests/service/test_organisation_preferences.py`:

```python
def test_missing_membership_policy_defaults_to_open() -> None:
    normalized = normalize_organisation_preferences({})
    assert normalized["membership"] == {"domain_join_policy": "open"}


def test_invalid_membership_policy_falls_back_to_open() -> None:
    normalized = normalize_organisation_preferences(
        {"membership": {"domain_join_policy": "unknown"}}
    )
    assert normalized["membership"] == {"domain_join_policy": "open"}


def test_merge_membership_policy_preserves_other_preferences() -> None:
    merged = merge_organisation_app_settings(
        {"integrations": {"google": {"gmail_read_enabled": True}}},
        membership_domain_join_policy="invite_only",
    )
    assert merged["membership"] == {"domain_join_policy": "invite_only"}
    assert merged["integrations"]["google"]["gmail_read_enabled"] is True
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
uv run pytest -q tests/service/test_organisation_preferences.py
```

Expected: FAIL because the normalized payload has no `membership` branch and the merge function has no `membership_domain_join_policy` argument.

- [ ] **Step 3: Implement the two-value preference without a new abstraction**

Add to `src/service/orgnisation/organisation_preferences.py`:

```python
DOMAIN_JOIN_POLICY_OPEN = "open"
DOMAIN_JOIN_POLICY_INVITE_ONLY = "invite_only"
DOMAIN_JOIN_POLICIES = {DOMAIN_JOIN_POLICY_OPEN, DOMAIN_JOIN_POLICY_INVITE_ONLY}
DEFAULT_DOMAIN_JOIN_POLICY = DOMAIN_JOIN_POLICY_OPEN


def _clean_domain_join_policy(value: Any) -> str:
    return value if value in DOMAIN_JOIN_POLICIES else DEFAULT_DOMAIN_JOIN_POLICY
```

In `normalize_organisation_preferences`, normalize `raw_membership` alongside the existing branches and assign:

```python
root["membership"] = {
    "domain_join_policy": _clean_domain_join_policy(
        membership.get("domain_join_policy")
    )
}
```

Extend `merge_organisation_app_settings` with:

```python
membership_domain_join_policy: str | None = None,
```

and merge only that branch:

```python
if membership_domain_join_policy is not None:
    root["membership"] = {
        "domain_join_policy": _clean_domain_join_policy(
            membership_domain_join_policy
        )
    }
```

- [ ] **Step 4: Add exact API schemas**

Create `src/api/schemas/membership_models.py`:

```python
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from src.domain.models.master_tables import UserAuthenticationMethod

DomainJoinPolicy = Literal["open", "invite_only"]


class MembershipPolicyResponse(BaseModel):
    domain_join_policy: DomainJoinPolicy


class UpdateMembershipPolicyRequest(BaseModel):
    domain_join_policy: DomainJoinPolicy
    model_config = ConfigDict(extra="forbid")


class OrganisationMemberInvitationRequest(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    authentication_method: Literal[
        UserAuthenticationMethod.GOOGLE_WORKSPACE,
        UserAuthenticationMethod.MICROSOFT_ENTRA,
    ]
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OrganisationMemberInvitationResponse(BaseModel):
    user_id: UUID
    email: EmailStr
```

- [ ] **Step 5: Centralize the organisation-admin dependency**

Add to `src/api/dependencies/organisation_dependencies.py`:

```python
from src.domain.models.master_tables import User, UserAppRole


def require_organisation_admin(current_user: User = Depends(user_auth)) -> User:
    if current_user.user_role != UserAppRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation admin access required",
        )
    return current_user
```

Export it in `__all__`. Update `src/api/routers/chat_attachments/catalog_handler.py` to import this dependency and delete only its now-duplicated local function/imports.

Add `tests/api/dependencies/test_organisation_admin_dependency.py` with direct member/admin cases:

```python
def test_require_organisation_admin_rejects_member() -> None:
    with pytest.raises(HTTPException) as error:
        require_organisation_admin(SimpleNamespace(user_role=UserAppRole.MEMBER))
    assert error.value.status_code == 403


def test_require_organisation_admin_returns_admin() -> None:
    admin = SimpleNamespace(user_role=UserAppRole.ADMIN)
    assert require_organisation_admin(admin) is admin
```

- [ ] **Step 6: Run focused verification**

Run:

```powershell
uv run pytest -q tests/service/test_organisation_preferences.py tests/api/dependencies/test_organisation_admin_dependency.py tests/api/routers/chat_attachments/test_catalog_handler.py
uv run ruff check src/service/orgnisation/organisation_preferences.py src/api/dependencies/organisation_dependencies.py src/api/routers/chat_attachments/catalog_handler.py src/api/schemas/membership_models.py
```

Expected: all selected tests pass and Ruff exits `0`.

- [ ] **Step 7: Commit the policy foundation**

```powershell
git add src/api/schemas/membership_models.py src/service/orgnisation/organisation_preferences.py src/api/dependencies/organisation_dependencies.py src/api/routers/chat_attachments/catalog_handler.py tests/service/test_organisation_preferences.py tests/api/dependencies/test_organisation_admin_dependency.py
git commit -m "feat: add organisation domain join policy"
```

---

### Task 2: Make signup preflight return the correct server-owned flow

**Files:**

- Modify: `src/domain/models/master_tables/signup_request_models.py`
- Modify: `src/domain/models/user_signup_models.py`
- Modify: `src/service/users/signup_service.py`
- Modify: `src/api/routers/users/signup_handler.py`
- Modify: `tests/service/users/test_signup_service.py`
- Modify: `tests/api/routers/users/test_signup_handler.py`

- [ ] **Step 1: Write the failing service-level case matrix**

Add tests that configure fake query results for these exact outcomes:

```python
@pytest.mark.asyncio
async def test_preflight_new_domain_returns_create_organisation() -> None:
    session = _Session(query_results=[None, None, None, None])
    result = await SignupService(session).preflight(identity=_identity())
    assert result.flow == SignupFlow.CREATE_ORGANISATION
    assert result.signup is not None
    assert result.signup.organisation_id is None


@pytest.mark.asyncio
async def test_preflight_existing_open_domain_targets_existing_organisation() -> None:
    organisation = Organisation(
        organisation_name="Acme",
        primary_domain="acme.com",
        preferences={"membership": {"domain_join_policy": "open"}},
        status=OrganisationStatus.APPROVED,
    )
    session = _Session(query_results=[None, None, None, organisation])
    result = await SignupService(session).preflight(identity=_identity())
    assert result.flow == SignupFlow.JOIN_EXISTING_ORGANISATION
    assert result.signup.organisation_id == organisation.organisation_id


@pytest.mark.asyncio
async def test_preflight_existing_invite_only_domain_requires_invitation() -> None:
    organisation = Organisation(
        organisation_name="Acme",
        primary_domain="acme.com",
        preferences={"membership": {"domain_join_policy": "invite_only"}},
        status=OrganisationStatus.APPROVED,
    )
    session = _Session(query_results=[None, None, None, organisation])
    result = await SignupService(session).preflight(identity=_identity())
    assert result.flow == SignupFlow.INVITATION_REQUIRED
    assert result.signup.organisation_id == organisation.organisation_id


@pytest.mark.asyncio
async def test_preflight_existing_invited_user_returns_sign_in_without_new_draft() -> None:
    invited = User(
        organisation_id=uuid4(),
        email="founder@acme.com",
        first_name="Founding",
        last_name="Admin",
        user_role=UserAppRole.MEMBER,
        organisation_role="Member",
        authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
    )
    session = _Session(query_results=[None, invited])
    result = await SignupService(session).preflight(identity=_identity())
    assert result.flow == SignupFlow.SIGN_IN
    assert result.signup is None
    assert session.added == []
```

Also preserve a submitted request test asserting `RESUME_SIGNUP`, and an inactive organisation test asserting `ORGANISATION_UNAVAILABLE`.

- [ ] **Step 2: Run tests and verify the missing contract fails**

Run:

```powershell
uv run pytest -q tests/service/users/test_signup_service.py -k "preflight"
```

Expected: FAIL because `SignupFlow`, `SignupPreflightResult`, and `SignupService.preflight` do not exist.

- [ ] **Step 3: Define the flow without leaking organisation identity**

In `src/domain/models/user_signup_models.py`, add:

```python
from enum import Enum


class SignupFlow(str, Enum):
    CREATE_ORGANISATION = "create_organisation"
    JOIN_EXISTING_ORGANISATION = "join_existing_organisation"
    INVITATION_REQUIRED = "invitation_required"
    SIGN_IN = "sign_in"
    RESUME_SIGNUP = "resume_signup"
    ORGANISATION_UNAVAILABLE = "organisation_unavailable"
```

Change `ExternalSignupProfile.signup_request_id` to `UUID | None`, and add:

```python
flow: SignupFlow
```

Do not return an organisation ID or organisation name for an existing-domain match.

In `src/domain/models/master_tables/signup_request_models.py`, add:

```python
INVITATION_REQUIRED = "invitation_required"
```

No Alembic migration is required because `signup_requests.status` is a varchar column without a database enum/check constraint.

- [ ] **Step 4: Implement one preflight resolver in the existing signup service**

Add the result dataclass to `src/service/users/signup_service.py`:

```python
from dataclasses import dataclass
from src.domain.models.user_signup_models import SignupFlow


@dataclass(frozen=True)
class SignupPreflightResult:
    flow: SignupFlow
    signup: SignupRequest | None
```

Implement `preflight` with this order under the existing subject advisory lock:

```python
async def preflight(self, *, identity: ExternalSignupIdentity) -> SignupPreflightResult:
    await self.lock_subject(identity)
    signup = await self._subject_request(identity)
    if signup is not None and signup.status not in {
        SignupRequestStatus.DRAFT,
        SignupRequestStatus.INVITATION_REQUIRED,
    }:
        flow = (
            SignupFlow.SIGN_IN
            if signup.status == SignupRequestStatus.ACTIVE
            else SignupFlow.RESUME_SIGNUP
        )
        return SignupPreflightResult(flow, signup)

    existing_mapping = await self._identity_mapping(identity)
    existing_user = await self._identity_user(identity)
    if existing_mapping is not None or existing_user is not None:
        return SignupPreflightResult(SignupFlow.SIGN_IN, None)

    organisation = await self._organisation_for_domain(identity.primary_domain)
    if signup is None:
        signup = self._new_draft(identity)
        self._session.add(signup)

    if organisation is None:
        signup.organisation_id = None
        await self._session.commit()
        await self._session.refresh(signup)
        return SignupPreflightResult(SignupFlow.CREATE_ORGANISATION, signup)

    signup.organisation_id = organisation.organisation_id
    await self._session.commit()
    await self._session.refresh(signup)
    if not organisation.is_active or organisation.status != OrganisationStatus.APPROVED:
        return SignupPreflightResult(SignupFlow.ORGANISATION_UNAVAILABLE, signup)

    policy = normalize_organisation_preferences(organisation.preferences)["membership"]
    flow = (
        SignupFlow.JOIN_EXISTING_ORGANISATION
        if policy["domain_join_policy"] == DOMAIN_JOIN_POLICY_OPEN
        else SignupFlow.INVITATION_REQUIRED
    )
    return SignupPreflightResult(flow, signup)
```

Extract the existing mapping/user queries from `_ensure_identity_available` into private read helpers and keep `_ensure_identity_available` delegating to them. Add `_organisation_for_domain` using normalized `primary_domain`. Do not duplicate query logic in the handler.

Rename `_lock_subject` to `lock_subject` and update its existing internal callers in the same file. This narrow public method is reused by the membership service in Task 3; do not add another lock implementation.

Update `finalize_signup` to reject a draft already bound to an existing organisation:

```python
if signup.organisation_id is not None:
    raise SignupServiceError(
        "This domain already belongs to an organisation",
        code="ORGANISATION_ACCESS_REQUIRED",
    )
```

- [ ] **Step 5: Expose the flow from PUT signup draft**

In `put_external_signup_draft`, call `SignupService(session).preflight(identity=identity)`. Build `ExternalSignupProfile` from `result.signup` when present and use `None` for `signup_request_id`, status, and organisation name only for `SIGN_IN`. To keep the response model explicit, make `status: SignupRequestStatus | None` and `organisation_name: str | None`.

The returned data must have this shape for an open existing domain:

```json
{
  "flow": "join_existing_organisation",
  "signup_request_id": "<uuid>",
  "status": "draft",
  "provider": "google",
  "email": "founder@acme.com",
  "primary_domain": "acme.com",
  "full_name": "Founding Admin",
  "profile_image_url": null,
  "organisation_name": null
}
```

- [ ] **Step 6: Add route tests for both providers**

Parameterize Google and Microsoft preflight tests and assert:

```python
@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_existing_open_domain_returns_join_flow(monkeypatch, provider) -> None:
    client, service = _client(monkeypatch, provider, flow="join_existing_organisation")
    response = client.put(
        f"/users/auth/{provider}/signup/draft",
        headers={"Authorization": "Bearer a.b.c"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["flow"] == "join_existing_organisation"
    assert service.dispatched == []
```

Retain the existing Google hosted-domain and Microsoft tenant verification coverage.

- [ ] **Step 7: Run focused verification and commit**

```powershell
uv run pytest -q tests/service/users/test_signup_service.py tests/api/routers/users/test_signup_handler.py
uv run ruff check src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/service/users/signup_service.py src/api/routers/users/signup_handler.py
git add src/domain/models/master_tables/signup_request_models.py src/domain/models/user_signup_models.py src/service/users/signup_service.py src/api/routers/users/signup_handler.py tests/service/users/test_signup_service.py tests/api/routers/users/test_signup_handler.py
git commit -m "feat: resolve signup flow before organisation setup"
```

Expected: focused tests pass, Ruff exits `0`, and no organisation form decision is left to the client.

---

### Task 3: Provision an existing-domain member idempotently

**Files:**

- Create: `src/service/users/organisation_membership_service.py`
- Create: `tests/service/users/test_organisation_membership_service.py`
- Modify: `src/service/users/signup_service.py`

- [ ] **Step 1: Write failing join tests**

Cover these invariants with fake master/tenant sessions:

```python
@pytest.mark.asyncio
async def test_open_domain_join_creates_member_mapping_and_audit() -> None:
    result = await service.join_verified_identity(identity, signup_request_id)
    assert result.status == SignupRequestStatus.ACTIVE
    assert result.user.user_role == UserAppRole.MEMBER
    assert result.user.admin_status == AdminUserStatus.APPROVED
    assert result.user.organisation_id == organisation.organisation_id
    assert result.mapping.external_user_id == identity.claims.provider_user_id
    assert result.signup.user_id == result.user.user_id


@pytest.mark.asyncio
async def test_invite_only_join_creates_no_user_or_mapping() -> None:
    result = await service.join_verified_identity(identity, signup_request_id)
    assert result.status == SignupRequestStatus.INVITATION_REQUIRED
    assert result.user is None
    assert not any(isinstance(item, User) for item in master_session.added)
    assert not any(isinstance(item, ExternalIdentityMapping) for item in master_session.added)


@pytest.mark.asyncio
async def test_repeated_active_join_returns_same_user_without_new_side_effects() -> None:
    first = await service.join_verified_identity(identity, signup_request_id)
    second = await service.join_verified_identity(identity, signup_request_id)
    assert second.user.user_id == first.user.user_id
    assert tenant_setup.call_count == 1


@pytest.mark.asyncio
async def test_join_rechecks_policy_after_preflight() -> None:
    organisation.preferences = {"membership": {"domain_join_policy": "invite_only"}}
    result = await service.join_verified_identity(identity, signup_request_id)
    assert result.status == SignupRequestStatus.INVITATION_REQUIRED
```

Add separate Google and Microsoft mapping-provider cases. Add a compensation test proving a tenant CRM user is removed if the master commit fails.

- [ ] **Step 2: Run tests and verify the service is missing**

```powershell
uv run pytest -q tests/service/users/test_organisation_membership_service.py
```

Expected: collection/import failure because `OrganisationMembershipService` does not exist.

- [ ] **Step 3: Implement one focused service shared by join and invitations**

Create `src/service/users/organisation_membership_service.py` with:

```python
@dataclass(frozen=True)
class MembershipResult:
    signup: SignupRequest
    status: SignupRequestStatus
    user: User | None
    mapping: ExternalIdentityMapping | None
    notify_admins: bool


class OrganisationMembershipError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 409) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class OrganisationMembershipService:
    def __init__(self, master_session: AsyncSession) -> None:
        self._session = master_session
```

Add `join_verified_identity(self, identity: ExternalSignupIdentity, signup_request_id: UUID) -> MembershipResult` to this class using the exact sequence below.

The implementation must:

1. Acquire the same subject advisory lock used by `SignupService`, then an advisory lock for the normalized email, and lock the signup request row. The email lock prevents simultaneous Google/Microsoft identities for one address from racing the unique user constraint.
2. Verify request ownership using provider, subject, tenant, and Supabase user ID.
3. Return the existing result if the request is already `ACTIVE` with user/organisation IDs.
4. Load and lock the target organisation from `signup.organisation_id`.
5. Require active/approved organisation state and re-read the normalized policy.
6. For `invite_only`, transition `DRAFT -> INVITATION_REQUIRED` once, commit, and return `notify_admins=True`; retries return `notify_admins=False`.
7. For `open`, rerun identity/email uniqueness checks and create exactly one member plus mapping and both audit rows.
8. Provision the tenant CRM identity through `UserTenantSetupService.ensure_user` using `acquire_org_async_session`.
9. Commit tenant state before master state, matching the existing admin-user provisioning pattern; if master commit fails, delete only the CRM user created by this request.
10. Set `signup.organisation_id`, `signup.user_id`, `signup.status=ACTIVE`, and `signup.completed_at` in the master commit.

The created user fields are fixed server values:

```python
user = User(
    organisation_id=organisation.organisation_id,
    supabase_user_id=identity.claims.supabase_user_id,
    email=identity.claims.email,
    first_name=first_name,
    last_name=last_name,
    user_role=UserAppRole.MEMBER,
    organisation_role="Member",
    authentication_method=identity.claims.authentication_method,
    profile_image_url=identity.claims.profile_image_url,
    status=SupabaseUserStatus.ACTIVE,
    admin_status=AdminUserStatus.APPROVED,
    email_verified=True,
    approved_at=now,
)
```

Use request-scoped audit idempotency:

```python
request_id = f"membership:{signup.signup_request_id}"
```

Create `UserActionHistory(action=UserEventType.CREATED, request_id=request_id, details={"source": "domain_auto_join"})` and `OrganisationActionHistory(action=OrganisationEventType.USER_ADDED, request_id=request_id, details={"source": "domain_auto_join", "user_id": str(user.user_id)})`.

- [ ] **Step 4: Keep signup locking/query helpers single-sourced**

Reuse `SignupService.lock_subject` from Task 2 and add narrowly named email-lock and ownership methods; do not copy lock construction or ownership SQL:

```python
async def lock_email(self, email: str) -> None:
    await self._lock(f"signup-email:{email.strip().lower()}")


async def get_owned_request_for_update(
    self,
    signup_request_id: UUID,
    identity: ExternalSignupIdentity,
) -> SignupRequest | None:
    claims = identity.claims
    return (
        (
            await self._session.execute(
                select(SignupRequest)
                .where(
                    col(SignupRequest.signup_request_id) == signup_request_id,
                    col(SignupRequest.supabase_user_id) == claims.supabase_user_id,
                    col(SignupRequest.provider_user_id) == claims.provider_user_id,
                    col(SignupRequest.provider_tenant_id) == claims.provider_tenant_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
```

Keep the existing non-locking `get_owned_request` for status reads.

- [ ] **Step 5: Run service verification and commit**

```powershell
uv run pytest -q tests/service/users/test_organisation_membership_service.py tests/service/users/test_signup_service.py tests/service/users/test_user_tenant_setup_service.py
uv run ruff check src/service/users/organisation_membership_service.py src/service/users/signup_service.py
git add src/service/users/organisation_membership_service.py src/service/users/signup_service.py tests/service/users/test_organisation_membership_service.py
git commit -m "feat: provision verified domain members"
```

Expected: all selected tests pass; retries create one public user, one mapping, and one tenant CRM user.

---

### Task 4: Add the public join endpoint and post-commit admin notifications

**Files:**

- Modify: `src/api/routers/users/signup_handler.py`
- Modify: `src/infra/auth/email_service.py`
- Modify: `tests/api/routers/users/test_signup_handler.py`
- Modify: `tests/infra/auth/test_email_service.py`

- [ ] **Step 1: Write failing endpoint tests for Google and Microsoft**

Add parameterized route tests:

```python
@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_join_existing_organisation_returns_active_member(monkeypatch, provider) -> None:
    response = client.post(
        f"/users/auth/{provider}/signup/join",
        headers={"Authorization": "Bearer a.b.c"},
        json={"signup_request_id": str(signup_request_id)},
    )
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "active"


def test_invite_only_join_returns_durable_invitation_required(monkeypatch) -> None:
    response = client.post(
        "/users/auth/google/signup/join",
        headers={"Authorization": "Bearer a.b.c"},
        json={"signup_request_id": str(signup_request_id)},
    )
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "invitation_required"
    assert email_notifications == ["founder@acme.com"]


def test_repeated_invitation_required_join_does_not_repeat_admin_email(monkeypatch) -> None:
    first = post_join()
    second = post_join()
    assert first.status_code == second.status_code == 202
    assert len(email_notifications) == 1
```

Also assert missing/foreign request IDs return `404`, and a create-organisation draft returns `409 SIGNUP_FLOW_INVALID`.

- [ ] **Step 2: Define the join request schema**

In `src/domain/models/user_signup_models.py` add:

```python
class ExistingOrganisationJoinRequest(BaseModel):
    signup_request_id: UUID
    model_config = ConfigDict(extra="forbid")
```

- [ ] **Step 3: Add the endpoint with no organisation/domain client input**

In `src/api/routers/users/signup_handler.py` add:

```python
@router.post(
    "/{provider}/signup/join",
    response_model=GenericResponse[ExternalSignupResponse],
    status_code=202,
    tags=["user_signup"],
)
async def join_existing_organisation(
    provider: str,
    join_request: ExistingOrganisationJoinRequest,
    request: Request,
    response: FastAPIResponse,
    _rate_limit: None = Depends(rate_limit_signup),
) -> GenericResponse[ExternalSignupResponse]:
    identity = _verify_identity(request, provider=provider)
    async with master_async_session_scope() as session:
        result = await OrganisationMembershipService(session).join_verified_identity(
            identity,
            join_request.signup_request_id,
        )
    if result.notify_admins:
        await _notify_membership_admins(result, identity)
    return Response.accepted_response(
        message=(
            "Organisation membership activated"
            if result.status == SignupRequestStatus.ACTIVE
            else "An administrator invitation is required"
        ),
        data=_response(result.signup),
        response=response,
    )
```

Use the same provider validation and exception mapping as `start_external_signup`; do not create a second token-verification helper.

- [ ] **Step 4: Add narrow email methods and active-admin recipient lookup**

In `src/infra/auth/email_service.py`, add:

```python
async def send_domain_member_joined_email(
    self,
    to_email: str,
    member_email: str,
    organisation_name: str,
) -> bool:
    subject = f"A teammate joined {organisation_name}"
    text = f"{member_email} joined your {organisation_name} workspace as a member."
    return await self.send_email(
        to_email,
        subject,
        self._create_styled_html(subject, text, f"<p>{html_escape(text)}</p>"),
        text,
    )


async def send_domain_access_attempt_email(
    self,
    to_email: str,
    requester_email: str,
    organisation_name: str,
) -> bool:
    subject = f"A teammate requested access to {organisation_name}"
    text = (
        f"{requester_email} tried to join your workspace. "
        f"Invite them from {self.app_url}/settings/members if they should have access."
    )
    return await self.send_email(
        to_email,
        subject,
        self._create_styled_html(subject, text, f"<p>{html_escape(text)}</p>"),
        text,
    )
```

After the membership transaction context closes, `_notify_membership_admins` must open a fresh `master_async_session_scope` for its recipient query. Query `public.users` using all of:

```python
User.organisation_id == organisation_id
User.user_role == UserAppRole.ADMIN
User.status == SupabaseUserStatus.ACTIVE
User.admin_status == AdminUserStatus.APPROVED
```

Do not expose recipient addresses in the public response. Log delivery failures with signup request ID, organisation ID, and event name, but not OAuth tokens/provider payloads.

- [ ] **Step 5: Verify endpoint and email behavior**

```powershell
uv run pytest -q tests/api/routers/users/test_signup_handler.py tests/infra/auth/test_email_service.py
uv run ruff check src/api/routers/users/signup_handler.py src/infra/auth/email_service.py src/domain/models/user_signup_models.py
git add src/api/routers/users/signup_handler.py src/domain/models/user_signup_models.py src/infra/auth/email_service.py tests/api/routers/users/test_signup_handler.py tests/infra/auth/test_email_service.py
git commit -m "feat: expose existing organisation join flow"
```

Expected: Google/Microsoft joins return the same contract; the first durable transition sends one admin notification; retries do not.

---

### Task 5: Add organisation-admin policy settings and invitations

**Files:**

- Create: `src/service/orgnisation/membership_policy_service.py`
- Create: `src/api/routers/app_preferences/membership_handler.py`
- Create: `src/api/routers/users/membership_handler.py`
- Create: `tests/service/orgnisation/test_membership_policy_service.py`
- Create: `tests/api/routers/app_preferences/test_membership_handler.py`
- Create: `tests/api/routers/users/test_membership_handler.py`
- Modify: `src/service/users/organisation_membership_service.py`
- Modify: `src/api/routers/app_preferences/__init__.py`
- Modify: `src/api/fastapi_app.py`

- [ ] **Step 1: Write failing policy service/API tests**

Test missing-policy default, update persistence, and member rejection:

```python
def test_member_cannot_read_or_change_membership_policy(client, member_override) -> None:
    assert client.get("/users/preference/membership").status_code == 403
    assert client.patch(
        "/users/preference/membership",
        json={"domain_join_policy": "invite_only"},
    ).status_code == 403


def test_admin_can_change_membership_policy(client, admin_override) -> None:
    response = client.patch(
        "/users/preference/membership",
        json={"domain_join_policy": "invite_only"},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {"domain_join_policy": "invite_only"}
```

- [ ] **Step 2: Implement the settings service using the existing preference merger**

Create `src/service/orgnisation/membership_policy_service.py` following the meeting-recording settings pattern:

```python
class MembershipPolicyService:
    def __init__(self, master_session: AsyncSession, organisation_id: UUID) -> None:
        self._session = master_session
        self._organisation_id = organisation_id

    async def get_policy(self) -> MembershipPolicyResponse:
        organisation = await self._organisation()
        membership = normalize_organisation_preferences(
            organisation.preferences
        )["membership"]
        return MembershipPolicyResponse.model_validate(membership)

    async def update_policy(
        self,
        request: UpdateMembershipPolicyRequest,
    ) -> MembershipPolicyResponse:
        organisation = await self._organisation()
        organisation.preferences = merge_organisation_app_settings(
            organisation.preferences,
            membership_domain_join_policy=request.domain_join_policy,
        )
        await self._session.commit()
        await self._session.refresh(organisation)
        return MembershipPolicyResponse.model_validate(
            organisation.preferences["membership"]
        )
```

- [ ] **Step 3: Add admin-only GET/PATCH routes**

Create `src/api/routers/app_preferences/membership_handler.py` with `/membership` GET and PATCH routes. Both depend on `require_organisation_admin`, derive the organisation ID from that authenticated user, use `get_master_async_session`, and apply existing read/write rate limits.

The route signatures must not accept `organisation_id`:

```python
async def update_membership_policy(
    request: UpdateMembershipPolicyRequest,
    current_user: User = Depends(require_organisation_admin),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[MembershipPolicyResponse]:
```

- [ ] **Step 4: Write failing invitation tests**

```python
def test_member_cannot_invite(client, member_override) -> None:
    response = client.post("/users/organisation/invitations", json=invitation_payload())
    assert response.status_code == 403


def test_admin_can_invite_member_in_open_mode(client, admin_override) -> None:
    response = client.post("/users/organisation/invitations", json=invitation_payload())
    assert response.status_code == 200
    assert response.json()["data"]["email"] == "teammate@acme.com"


def test_admin_can_invite_member_in_invite_only_mode(client, invite_only_admin_override) -> None:
    response = client.post("/users/organisation/invitations", json=invitation_payload())
    assert response.status_code == 200


def test_invitation_rejects_email_outside_organisation_domain(client, admin_override) -> None:
    payload = invitation_payload(email="person@other.com")
    response = client.post("/users/organisation/invitations", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_COMPANY_EMAIL_DOMAIN"
```

- [ ] **Step 5: Implement external member invitation in the membership service**

Add:

```python
async def invite_member(
    self,
    *,
    organisation: Organisation,
    inviter: User,
    request: OrganisationMemberInvitationRequest,
) -> User:
```

It must:

1. Require inviter organisation equality and `UserAppRole.ADMIN` even though the route already checks it.
2. Validate email through `ExternalAuthService.email_matches_organisation_domain` with `organisation_domains=[organisation.primary_domain, *(organisation.domain or [])]`, filtering a null primary domain before the call. This supports both migrated scalar identity and the legacy domain list without changing the shared helper contract.
3. Call `SignupService(self._session).lock_email(normalized_email)` and reject any existing public user.
4. Create an approved active `MEMBER` with the requested Google/Microsoft authentication method, no Supabase ID, and `created_by_auth_user_id=inviter.supabase_user_id`.
5. Create the tenant CRM user through the same private provisioning/compensation helper used by automatic join.
6. Record `UserActionHistory` and `OrganisationActionHistory` with `details={"source": "organisation_admin_invitation", "actor_user_id": str(inviter.user_id)}`; leave `actor_admin_id=None` because that foreign key points to platform `super_admins`.
7. Commit before email delivery.

- [ ] **Step 6: Add the organisation-admin invitation route**

Create `src/api/routers/users/membership_handler.py`:

```python
@router.post(
    "/invitations",
    response_model=GenericResponse[OrganisationMemberInvitationResponse],
)
async def invite_organisation_member(
    request: OrganisationMemberInvitationRequest,
    current_user: User = Depends(require_organisation_admin),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[OrganisationMemberInvitationResponse]:
    organisation = await master_session.get(Organisation, current_user.organisation_id)
    user = await OrganisationMembershipService(master_session).invite_member(
        organisation=organisation,
        inviter=current_user,
        request=request,
    )
    provider_name = (
        "Google"
        if user.authentication_method == UserAuthenticationMethod.GOOGLE_WORKSPACE
        else "Microsoft"
    )
    await email_service.send_external_auth_invitation_email(
        to_email=str(user.email),
        first_name=user.first_name,
        provider_name=provider_name,
    )
    return Response.success_response(
        message="Member invited successfully",
        data=OrganisationMemberInvitationResponse(
            user_id=user.user_id,
            email=user.email,
        ),
    )
```

Map duplicate email, invalid domain, inactive organisation, and tenant provisioning errors to stable `409`, `422`, `403`, and `503` responses. A failed email returns success for the durable invitation and logs `membership_invitation_delivery_failed`.

- [ ] **Step 7: Register routers**

Export `membership_policy_router` from `src/api/routers/app_preferences/__init__.py`. Register:

```python
fastapp.include_router(
    membership_policy_router,
    prefix="/users/preference",
    tags=["user-preferences"],
)
fastapp.include_router(
    organisation_membership_router,
    prefix="/users/organisation",
    tags=["user", "membership"],
)
```

- [ ] **Step 8: Run focused verification and commit**

```powershell
uv run pytest -q tests/service/orgnisation/test_membership_policy_service.py tests/api/routers/app_preferences/test_membership_handler.py tests/api/routers/users/test_membership_handler.py tests/service/users/test_organisation_membership_service.py
uv run ruff check src/service/orgnisation/membership_policy_service.py src/api/routers/app_preferences/membership_handler.py src/api/routers/users/membership_handler.py src/service/users/organisation_membership_service.py src/api/fastapi_app.py
git add src/service/orgnisation/membership_policy_service.py src/api/routers/app_preferences/membership_handler.py src/api/routers/users/membership_handler.py src/api/routers/app_preferences/__init__.py src/api/fastapi_app.py src/service/users/organisation_membership_service.py tests/service/orgnisation/test_membership_policy_service.py tests/api/routers/app_preferences/test_membership_handler.py tests/api/routers/users/test_membership_handler.py
git commit -m "feat: let organisation admins control membership"
```

Expected: only organisation admins can manage policy/invitations, and invitations work in both policy modes.

---

### Task 6: Update contracts and run the completion audit

**Files:**

- Modify: `docs/API Documentation/users-auth-api.md`
- Modify: `docs/Technical Documentation/self-service-signup-and-organisation-invitations.md`
- Verify: all files changed in Tasks 1–5

- [ ] **Step 1: Update API documentation with exact request/response contracts**

Document:

- `PUT /users/auth/{provider}/signup/draft` and all six `flow` values.
- `POST /users/auth/{provider}/signup/join` with only `signup_request_id` in the body.
- `GET/PATCH /users/preference/membership` as organisation-admin-only.
- `POST /users/organisation/invitations` as organisation-admin-only and member-only.
- Missing policy defaults to `open`.
- Existing-domain automatic join creates a member, never an admin or organisation.
- `invite_only` produces durable `invitation_required` and no user.

- [ ] **Step 2: Replace stale design statements**

In `self-service-signup-and-organisation-invitations.md`, remove statements that same-domain signup always returns `ORGANISATION_ACCESS_REQUIRED`. Replace them with the implemented case matrix and explicitly retain:

```text
Provider identity proves the user controls one work account. It never grants an admin role.
Organisation membership policy decides only member admission; record-level authorization remains separate.
```

- [ ] **Step 3: Run the complete focused backend suite**

```powershell
uv run pytest -q tests/service/users/test_signup_service.py tests/service/users/test_organisation_membership_service.py tests/service/users/test_user_tenant_setup_service.py tests/service/orgnisation/test_membership_policy_service.py tests/service/test_organisation_preferences.py tests/api/routers/users/test_signup_handler.py tests/api/routers/users/test_membership_handler.py tests/api/routers/app_preferences/test_membership_handler.py tests/api/routers/users/test_auth_handler.py tests/infra/auth/test_email_service.py tests/workers/organization_worker/test_signup_task.py tests/migrations/test_external_signup_migration.py
```

Expected: all selected tests pass with no failures.

- [ ] **Step 4: Run static checks on the changed backend slice**

```powershell
uv run ruff check src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/service/users/signup_service.py src/service/users/organisation_membership_service.py src/service/orgnisation/organisation_preferences.py src/service/orgnisation/membership_policy_service.py src/api/dependencies/organisation_dependencies.py src/api/routers/users/signup_handler.py src/api/routers/users/membership_handler.py src/api/routers/app_preferences/membership_handler.py src/api/schemas/membership_models.py src/infra/auth/email_service.py src/api/fastapi_app.py
uv run ruff format --check src/domain/models/user_signup_models.py src/domain/models/master_tables/signup_request_models.py src/service/users/signup_service.py src/service/users/organisation_membership_service.py src/service/orgnisation/organisation_preferences.py src/service/orgnisation/membership_policy_service.py src/api/dependencies/organisation_dependencies.py src/api/routers/users/signup_handler.py src/api/routers/users/membership_handler.py src/api/routers/app_preferences/membership_handler.py src/api/schemas/membership_models.py src/infra/auth/email_service.py src/api/fastapi_app.py
uv run mypy src/service/users/signup_service.py src/service/users/organisation_membership_service.py src/service/orgnisation/membership_policy_service.py src/api/routers/users/signup_handler.py src/api/routers/users/membership_handler.py src/api/routers/app_preferences/membership_handler.py
```

Expected: every command exits `0`.

- [ ] **Step 5: Verify contract and mutation invariants by search**

```powershell
rg -n "join_existing_organisation|invitation_required|domain_join_policy" src tests docs
rg -n "user_role=UserAppRole.ADMIN" src/service/users/organisation_membership_service.py
rg -n "organisation_id.*join|primary_domain.*request" src/api/routers/users/signup_handler.py src/domain/models/user_signup_models.py
```

Expected:

- First search finds schemas, service logic, tests, and documentation.
- Second search returns no auto-join assignment of `ADMIN`.
- Third search confirms the join request accepts neither organisation ID nor domain.

- [ ] **Step 6: Confirm repository hygiene**

```powershell
git status --short
git diff --check
git diff --name-only origin/main...HEAD
```

Expected: no whitespace errors; only the planned backend/docs paths are changed; unrelated work remains untouched.

- [ ] **Step 7: Commit documentation**

```powershell
git add 'docs/API Documentation/users-auth-api.md' 'docs/Technical Documentation/self-service-signup-and-organisation-invitations.md'
git commit -m "docs: define domain membership signup behavior"
```

---

## Completion criteria

The backend slice is complete only when all of the following are proven:

- Google and Microsoft preflight return identical flow semantics.
- An existing domain never reaches organisation creation.
- Missing/`open` policy joins a verified user as one member with one identity mapping and one CRM user.
- `invite_only` creates no user/mapping/CRM user and returns durable `invitation_required`.
- Existing invitees are routed to sign-in and linked by the existing completion endpoint.
- Organisation admins, and only organisation admins, can read/change policy and invite members.
- Invitations work in both policies.
- Automatic join and blocked access attempt each notify admins once per durable transition.
- Retry and concurrency tests prove no duplicate organisation, user, mapping, CRM user, or audit record.
- Public payloads contain no client-authored organisation ID, domain, role, approval state, or provider authority fields.
- Focused tests, Ruff, formatting, Mypy, diff checks, and documentation all pass.
