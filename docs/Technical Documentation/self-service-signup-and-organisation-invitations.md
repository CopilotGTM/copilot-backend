# Self-Service Signup and Organisation Invitations

## Status

**Signup, same-domain member admission, organisation-admin membership policy, and direct external
member invitations are implemented in source. Migration and provider rollout are not applied by
this change.**

The backend keeps sign-in separate from signup and exposes three product entry paths:

1. **Sign up** creates a new organisation and its first admin.
2. **Same-domain signup** joins a verified user as a member when policy is open, or records an
   invitation-required request when policy is invite-only.
3. **Admin invitation** pre-creates a member that completes provider linking through sign-in.

Sign in remains a third, separate intent for users who already have an account. Sign in must
never create an organisation implicitly.

## Product decision

CopilotGTM should support both self-service signup and organisation invitations.

**V1 signup supports only Google Workspace and Microsoft Entra work/school accounts.** There is
no password signup, personal Gmail signup, or personal Microsoft signup in this phase. Existing
password users and admin-created password invitations continue to work; this restriction applies
only to public self-service organisation creation.

| User intent | Result |
| --- | --- |
| Sign up for a new account | Create a new organisation and make the verified founding user its first admin. |
| Sign up with an existing organisation domain | Join as a member when policy is open; otherwise notify admins and require an invitation. |
| Sign in to an existing account | Authenticate only; never create a user or organisation. |
| Accept an invitation | Join the exact organisation and role named by the invitation. |
| Existing organisation admin invites a teammate | Create an approved member for the selected provider and send the invitation. |

The frontend may reuse one visual authentication shell, but it must expose distinct `/signup`
and `/signin` routes and preserve the selected intent through OAuth using signed `state`. An
unknown identity returning from **Sign in** receives an account-not-found action with a link to
Sign up. It is not silently converted into a signup.

## Why the first signup user is the admin

Salesforce documents that the person who signs up for a trial becomes the Salesforce admin.
HubSpot likewise treats account creation and inviting a team as separate steps, then lets an
authorised administrator assign permissions to invited users.

CopilotGTM should reuse the existing organisation-scoped `admin` role for the founding user rather
than adding an unnecessary owner role or overloading the platform `super_admin` role:

- `admin`: organisation user, invitation, and settings administration;
- `member`: ordinary product access;
- `platform_super_admin`: CopilotGTM staff authority only; never granted by public signup.

The first user becomes admin because that user completed the durable organisation-creation
request, not because their email domain or identity provider looks authoritative. The admin is
activated only after their OAuth identity is verified. An organisation must always have at least
one active admin, so the last active admin cannot be removed, suspended, or demoted until another
admin exists.

For V1, the existing one-user-to-one-organisation constraint remains. Multi-organisation
membership is a separate product decision and should not be introduced merely to deliver these
two flows.

## Authentication-method policy

Each user still has one application-owned primary authentication method in V1:

- `password`
- `google_workspace`
- `microsoft_entra`

Public self-signup accepts only `google_workspace` and `microsoft_entra`. Google must supply a
Workspace hosted-domain (`hd`) claim; Microsoft must supply an Entra tenant ID (`tid`). Personal
Gmail, Outlook.com, Hotmail, and other consumer identities are rejected. Supporting them safely
would require a separate company-domain verification flow and is intentionally outside V1.

The method is selected as follows:

- self-signup: the method the founder used to sign up;
- invitation: the method selected in the invitation or required by organisation policy;
- existing account: the already configured method remains authoritative.

Method mismatch remains deterministic:

| Attempt | Existing method | Result |
| --- | --- | --- |
| Google OAuth | password | `PASSWORD_SIGN_IN_REQUIRED` |
| Password login | Google | `GOOGLE_SIGN_IN_REQUIRED` |
| Microsoft OAuth | password or Google | corresponding `*_SIGN_IN_REQUIRED` |
| Forgot password | Google or Microsoft | generic success response; send no reset email |

Explicit account-linking or multiple login methods are not required for V1. They can be added as
a separately authenticated settings flow later; OAuth callback email equality alone must never
link accounts silently.

## Flow A: self-service signup

### Frontend

1. User opens `/signup`, not `/signin`.
2. User chooses Continue with Google or Continue with Microsoft.
3. OAuth returns a verified provider session. The frontend preserves `intent=signup`, provider,
   redirect target, and anti-CSRF nonce in signed OAuth `state`.
4. The UI calls the provider signup draft PUT endpoint to create or resume a durable draft, then displays
   provider name/email as read-only context.
5. The UI asks only for `organisation_name` and submits it with the verified bearer session.
6. The UI shows durable provisioning status while the backend completes or reconciles the request.
7. On success, the founding user enters onboarding as organisation admin.

### Information collected before provisioning

The only browser-authored field is `organisation_name`. Provider name, email, avatar, subject,
tenant, and domain are read-only authority data. The backend records the current Terms of Service
and Privacy Policy versions when the user clicks Create workspace.

Country, job title, team size, website, and LinkedIn are deferred until after activation.

Do not block signup on `agent_name`, company address, summary, logo, CRM selection, integrations,
or channel setup. Collect those in post-signup onboarding or derive them through the existing
best-effort organisation research job. The backend assigns `user_role=admin`,
`organisation_role="Administrator"`, authentication method, email, provider tenant, status, IDs, and all
approval fields; the public request cannot choose them.

The public request does not contain `primary_domain`. The backend derives and normalizes it from
the remotely verified Google Workspace hosted-domain claim or Microsoft Entra identity.

### Backend

1. Remotely verify the Supabase session and extract the immutable provider subject, verified
   email, Workspace hosted domain or Entra tenant ID, name, and trusted avatar.
2. Reject an exact existing user with `ACCOUNT_ALREADY_EXISTS`; direct the user to Sign in.
3. Derive and normalize `primary_domain` from verified provider claims. A matching existing
   organisation returns a server-owned join or invitation-required flow; it never creates a
   second organisation or promotes the user to admin.
4. Create or resume one `draft` without reserving the domain. Supabase has already created the
   OAuth identity, so no password or second auth identity is created.
5. On Create workspace, lock the provider subject and domain, transition the draft to `received`,
   and dispatch provisioning. A retry may dispatch again while state is still `received`; the
   worker is idempotent. In one later master transaction, create the organisation, founding user,
   external identity mapping, and audit records idempotently.
6. Assign the founding user `admin`, `admin_status=approved`, and an active account state while the
   organisation remains `provisioning` and therefore inaccessible.
7. Reuse the existing tenant setup to create the internal CRM integration, default pipeline and
   stages, and the founder's CRM user using the founder's application user ID.
8. Reuse the organisation storage component to create the deterministic Azure container
   `org-<organisation-uuid>`; container creation is idempotent and a required provisioning gate.
9. Mark the organisation `approved`, mark the durable signup request `active`, and only then allow
   `/me`, refresh, and protected API access.
10. Send a welcome email and queue existing best-effort organisation research. OAuth-verified users
    do not need a second verification email.

If an approved active organisation owns the verified domain, bind the durable request to that
organisation without exposing its identity. Missing/`open` policy permits idempotent member
admission. `invite_only` creates no user, mapping, or CRM identity and notifies active admins once.
Personal email domains remain unsupported.

### Existing organisation and repeat-signup rules

The normalised verified `primary_domain`, not the organisation display name, determines whether
the organisation already exists:

| Situation | Result |
| --- | --- |
| Same provider subject already mapped | Return `sign_in`; do not repeat signup. |
| Same verified email already belongs to an invited or active user | Return `sign_in`; enforce that user's configured method. |
| Same primary domain belongs to an organisation with open policy | Return `join_existing_organisation`; create only a member on join. |
| Same primary domain belongs to an invite-only organisation | Return `invitation_required`; create no user until an admin invites them. |
| Same display name but a different verified primary domain | Allow a different organisation; names are not identity. |
| Previous signup request is still running | Return the same request; while status is `received`, retry dispatch because the previous queue result may be uncertain. |
| Previous request failed after partial work | Resume from its last committed milestone using the same request. |
| Existing user requests another organisation | Reject in V1 with `ACCOUNT_IN_OTHER_ORGANISATION`. |

Before submission, the same provider subject resumes its draft and may edit only the organisation
name. After submission, organisation identity and user identity are immutable; retries return the
same request and only resume provisioning.

The database must enforce one normalised `primary_domain` per organisation so concurrent requests
cannot create duplicates. The current globally unique `organisation_name` constraint should be
removed because two unrelated legal entities may share a display name.

### Reuse of the current organisation lifecycle

The current admin path is not callable as-is from signup. It creates a super-admin-owned
`OrganisationJob`, passes `admin_id` through provisioning/audit, provisions tenant defaults before
a founder application user exists, creates the Azure container, and finishes with organisation
status `pending` for manual approval.

Keep and reuse the working pieces—organisation worker, internal CRM setup, deterministic storage
container, status tracking, and best-effort research—but extract an actor-neutral provisioning
service. Public signup supplies `founder_user_id`; the admin path supplies its appropriate actor
context. Do not call the `/admin/organisations/create_organisation` HTTP route internally and do
not put a fake founder into `super_admins`.

### Failure model

Signup crosses systems that cannot share one database transaction. Reuse the durable
`signup_requests` state-machine design already documented in
`deferred-public-signup-design.md`; extend it to provider signups rather than writing a second
orchestrator. Required properties are:

- server-owned idempotency derived from the verified provider subject and durable draft;
- password-free durable storage and logs;
- committed milestones before every external boundary;
- bounded reconciliation after timeouts;
- deterministic provider-subject and Supabase identity lookup;
- one master transaction for organisation and founding user;
- safe retry of tenant-default creation.

## Flow B: current organisation-member invitation

### Authority and scope

- Only an authenticated, active organisation `admin` may call the invitation endpoint.
- The service repeats the organisation equality and `admin` role checks inside the business
  transaction; route authorization is not the only protection.
- Public invitation can create only `member`. It cannot grant `admin`, choose approval state, or
  target another organisation.
- The normalized invite email must match the organisation's scalar primary domain or its legacy
  configured domain list. Cross-domain and consumer-email invitations are not supported here.
- The selected authentication method is exactly `google_workspace` or `microsoft_entra`.

### Durable write and retry behavior

1. Acquire a transaction-scoped advisory lock for the normalized email.
2. Reject an email already owned by another user or organisation.
3. Create one approved, active application `member` with no Supabase user ID. The configured
   authentication method prevents password access while the provider identity is unlinked.
4. Provision the tenant CRM identity, then commit the master user and audit rows. If the master
   commit fails after tenant creation, delete only that newly created tenant CRM user.
5. Send the provider-specific invitation email after the durable commit.
6. Repeating the same invitation for the same unlinked member and provider reuses the user and
   retries email delivery; it does not create another member or CRM identity.

Email is currently best-effort rather than outbox-backed. A delivery failure does not roll back
the durable invitation. It is logged with organisation and user identifiers, and the admin can
repeat the same invitation to retry delivery.

## API contract

The endpoints below are implemented. All responses use the existing CopilotGTM response envelope
and stable machine-readable error codes.

### Signup

#### `PUT /users/auth/google/signup/draft`

#### `PUT /users/auth/microsoft/signup/draft`

Resolves one of `create_organisation`, `join_existing_organisation`, `invitation_required`,
`sign_in`, `resume_signup`, or `organisation_unavailable`. It returns read-only provider identity
context and never accepts an organisation ID, role, domain, or provider authority from the client.

The lookup prefers the unique scalar `primary_domain`. For legacy admin-created rows whose scalar
is null, it checks `domain[]`, repairs the scalar only when exactly one owner exists, and fails
closed with `SIGNUP_RECONCILIATION_REQUIRED` when legacy ownership is ambiguous.

#### `POST /users/auth/google/signup`

#### `POST /users/auth/microsoft/signup`

Finalises Google Workspace or Microsoft Entra organisation signup using the remotely verified
Supabase bearer session. Public and strongly rate-limited; idempotency is server-owned.

```json
{
  "organisation_name": "Acme"
}
```

Returns `202 Accepted` with `signup_request_id` and a non-secret status resource.

An existing-domain or `invitation_required` draft cannot be submitted to this organisation-
creation endpoint.

#### `POST /users/auth/google/signup/join`

#### `POST /users/auth/microsoft/signup/join`

Accepts only `{ "signup_request_id": "uuid" }`. The verified provider, subject, tenant, and
Supabase user ID must own the server-side request. The service rechecks the current verified domain
and email-domain membership, target organisation state, and policy under locks. Open policy creates
an active approved member; invite-only transitions once to `invitation_required` without creating
a user or mapping.

#### `GET /users/auth/signup-requests/{signup_request_id}`

Returns `draft`, `invitation_required`, `received`, `control_plane_created`,
`tenant_defaults_created`, `storage_created`, `active`, `reconciliation_required`, or terminal
failure status. Provider, provider subject, tenant, and Supabase user ID must own the request; a
UUID alone is not authorization. Organisation and user IDs are withheld until `active`, including
from an invite-only applicant who is not yet a member.

### Membership policy

#### `GET /users/preference/membership`

#### `PATCH /users/preference/membership`

Both require organisation admin authorization. `PATCH` accepts only:

```json
{ "domain_join_policy": "open" }
```

The other value is `invite_only`. Missing or invalid stored policy normalizes to `open`; invalid
request values fail Pydantic validation.

### Organisation-member invitation

#### `POST /users/organisation/invitations`

Requires organisation admin authorization and accepts only email, first name, last name, and
Google/Microsoft authentication method. Organisation, role, status, and approval are server-owned.
The existing `/users/auth/{provider}/complete` path links the invited email's verified provider
identity on first sign-in.

### Explicitly not implemented in this slice

Bulk/CSV invitations, cross-domain contractors, password invitations, invitation-token preview or
acceptance endpoints, expiry/revocation, and a transactional email outbox are future expansions.
They must not be described to the frontend as current API contracts.

## Stable frontend outcomes

| Code | Frontend action |
| --- | --- |
| `ACCOUNT_ALREADY_EXISTS` | Offer Sign in; do not reveal authentication details before identity verification. |
| `ACCOUNT_NOT_FOUND` | On Sign in, offer an explicit link to Sign up. |
| `ORGANISATION_ACCESS_REQUIRED` | The existing-domain draft cannot enter organisation creation; rerun preflight. |
| `SIGNUP_RECONCILIATION_REQUIRED` | Stop and contact support; domain ownership or committed state is ambiguous. |
| `ORGANISATION_INACTIVE` | Stop; the matched organisation cannot admit members. |
| `SIGNUP_FLOW_INVALID` | Rerun preflight; the request is not eligible for existing-organisation join. |
| `INVALID_COMPANY_EMAIL_DOMAIN` | Admin must use an email matching an organisation domain. |
| `USER_ALREADY_EXISTS` | The email cannot be invited into another user or organisation. |
| `PASSWORD_SIGN_IN_REQUIRED` | Show password sign-in. |
| `GOOGLE_SIGN_IN_REQUIRED` | Start Google sign-in. |
| `MICROSOFT_SIGN_IN_REQUIRED` | Start Microsoft sign-in. |
| `ACCOUNT_IN_OTHER_ORGANISATION` | Explain the V1 one-organisation limitation; contact support/admin. |

`invitation_required` is a successful preflight flow and durable signup status, not an error code.
Public responses remain enumeration-resistant. Existing-organisation details are available only
after an authenticated organisation-admin boundary; preflight never reveals its ID or name.

## Current implementation gaps

Self-service signup is combined into the unreleased master migration `012`, together with the
authentication-method fields. It adds durable `signup_requests`, makes normalised
`primary_domain` the organisation uniqueness key, and removes display-name uniqueness. Before
release, every target environment must confirm that `012` has not already been applied; otherwise
the migration must become a new additive revision instead of rewriting history. This change does
not apply migrations automatically.

| Area | Current implementation | Required target |
| --- | --- | --- |
| Public signup | Google Workspace and Microsoft Entra self-signup implemented | Rehearse and release through environment gates |
| First organisation user | Verified founder becomes the first organisation admin | Complete |
| Unknown OAuth user | Rejected on Sign in; allowed only on explicit Sign up | Complete for signup |
| Consumer Google/Microsoft account | Rejected | Remains rejected in V1 |
| Organisation uniqueness | Migration `012` uses unique normalized verified primary domain | Apply migration through release gates |
| Tenant defaults and storage | Founder-first shared provisioning with required container gate | Complete for signup |
| Organisation invitations | Admin creates an external-auth member directly and sends provider invitation | Durable token/outbox workflow may be added when cross-domain or bulk invitations are required |
| Bulk invitations | Not available | One request with 1-50 per-entry results |
| Delivery reliability | Direct best-effort email; repeating the same unlinked invitation retries delivery | Transactional outbox and delivery status are optional future hardening |
| Approval | Founder and admitted/invited member are active and approved; provider linking still gates access | Complete for current flow |
| Organisation roles | `admin` / `member` | Reuse organisation `admin`; keep platform super-admin separate and protect the last active admin |
| External invite domain | Must match an organisation domain | Keep same-domain validation for this direct-member invitation flow |

## Security and production gates

Provider identity proves the user controls one work account. It never grants an admin role.
Organisation membership policy decides only member admission; record-level authorization remains
separate.

- Signed OAuth `state` binds intent (`signin` or `signup`), provider, redirect, and anti-CSRF nonce.
- Provider session is remotely verified; email must be provider-verified.
- No organisation or admin role is created from a Sign in callback.
- Signup, same-domain join, and direct invitation creation are idempotent under duplicate and
  concurrent requests.
- Provider verification plus open membership policy may grant only `member`; domain equality
  never grants admin.
- Signup and invitation endpoints use the existing public/user write rate limits.
- Email is post-commit and best-effort. Failure cannot roll back membership; direct invitations
  can be repeated safely to retry delivery. A durable outbox is not implemented in this slice.
- Audit records cover signup, organisation creation, founding-admin assignment, automatic member
  admission, and direct member invitation.
- Suspended users and organisations fail closed on completion, refresh, `/me`, and protected APIs.
- No production rollout until migration compatibility, Supabase provider settings, redirect
  allowlists, email delivery, rollback, and the complete acceptance matrix pass in non-production.

## Release acceptance matrix

- New Google Workspace founder.
- New Microsoft Entra founder.
- Personal Gmail and personal Microsoft signup are rejected without creating records.
- Unknown provider identity through Sign in does not create anything.
- Existing password user tries Google and receives password guidance.
- Existing Google/Microsoft user tries password and forgot-password remains neutral.
- Existing email tries Sign up and is directed safely to Sign in.
- Same verified organisation domain joins only as `member` when policy is open.
- Invite-only admission creates no user/mapping/CRM identity and notifies admins once.
- Ambiguous legacy domain ownership fails closed; one legacy owner self-heals `primary_domain`.
- Admin policy read/update rejects members.
- Admin single invite works in both modes and rejects cross-domain email.
- Repeated unlinked invitation reuses one user and retries email delivery.
- Notification failure after membership commit does not turn the join into failure.
- Suspended user/organisation at signup completion, join, refresh, and `/me`.
- Retry after Supabase timeout, database rollback, tenant-default failure, and callback replay.

## Remaining delivery order

1. Run migration preflight and apply the combined migration in non-production.
2. Configure the dedicated Supabase Google and Microsoft login applications and redirect allowlists.
3. Run real Google Workspace and Microsoft Entra signup, duplicate-domain, retry, and email smoke tests.
4. Build separate frontend Sign up and Sign in routes on the shared authentication shell.
5. Release signup through normal environment gates with worker and reconciliation monitoring.
6. If product scope expands beyond same-domain direct members, separately design outbox delivery,
   bulk results, cross-domain authority, acceptance tokens, expiry/revocation, and the
   last-active-admin guard.

## External references

- HubSpot: [Set up your HubSpot account](https://knowledge.hubspot.com/get-started/set-up-your-account)
- HubSpot: [Add HubSpot users](https://knowledge.hubspot.com/account-management/add-hubspot-users)
- Salesforce: [Try Out Salesforce](https://help.salesforce.com/s/articleView?id=sf.getstart.htm&language=en_US&type=5)
- Salesforce: [Add a Single User](https://help.salesforce.com/s/articleView?id=sf.adding_new_users.htm&language=en_US&type=5)
