# Self-Service Signup and Organisation Invitations

## Status

**Signup implementation complete in source; migration and provider rollout are not applied by
this change. Organisation invitations remain a separately documented future phase.**

The backend keeps invite-only sign-in for existing organisations and now exposes explicit public
self-service organisation signup. This document defines two supported product entry paths:

1. **Sign up** creates a new organisation and its first admin.
2. **Accept invitation** joins a user to an existing organisation.

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
| Sign in to an existing account | Authenticate only; never create a user or organisation. |
| Accept an invitation | Join the exact organisation and role named by the invitation. |
| Existing organisation admin invites teammates | Create one or up to 50 invitations and deliver them asynchronously. |

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
4. The UI pre-fills provider name/email and asks the user to confirm the required profile and
   organisation fields listed below.
5. The UI submits an idempotent signup request with the verified bearer session.
6. The UI shows durable provisioning status while the backend completes or reconciles the request.
7. On success, the founding user enters onboarding as organisation admin.

### Information collected before provisioning

Required:

- `first_name` and `last_name`, pre-filled from the provider but confirmed by the user;
- `organisation_name`;
- `primary_domain`, pre-filled from Google `hd` or the Microsoft verified email domain and not
  accepted when it differs from the verified provider identity;
- `country` from the existing allowed-country catalogue;
- acceptance of the Terms of Service and Privacy Policy versions.

Optional before provisioning:

- `job_title`;
- `sales_team_size`;
- `website_url`, which must resolve to the confirmed primary domain;
- `linkedin_url`.

Do not block signup on `agent_name`, company address, summary, logo, CRM selection, integrations,
or channel setup. Collect those in post-signup onboarding or derive them through the existing
best-effort organisation research job. The backend assigns `user_role=admin`,
`organisation_role="Administrator"`, authentication method, email, provider tenant, status, IDs, and all
approval fields; the public request cannot choose them.

### Backend

1. Remotely verify the Supabase session and extract the immutable provider subject, verified
   email, Workspace hosted domain or Entra tenant ID, name, and trusted avatar.
2. Reject an exact existing user with `ACCOUNT_ALREADY_EXISTS`; direct the user to Sign in.
3. Derive and normalize `primary_domain` from the verified provider claims. A matching existing
   organisation returns `ORGANISATION_ACCESS_REQUIRED`; it never auto-joins or auto-promotes.
4. Create one durable signup request before dispatching background provisioning. Supabase has
   already created the OAuth identity, so no password or second auth identity is created.
5. In one master transaction, reserve the domain and create the organisation, founding user,
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

If an existing organisation has a verified claim on the same custom domain, return
`ORGANISATION_ACCESS_REQUIRED`. The frontend should tell the user to ask an existing admin
for an invitation. V1 does not need public organisation discovery or automatic domain joining.
Personal email domains are not accepted for V1 public signup.

### Existing organisation and repeat-signup rules

The normalised verified `primary_domain`, not the organisation display name, determines whether
the organisation already exists:

| Situation | Result |
| --- | --- |
| Same provider subject already mapped | `ACCOUNT_ALREADY_EXISTS`; send to Sign in. |
| Same verified email already belongs to any user | `ACCOUNT_ALREADY_EXISTS`; enforce that user's configured method. |
| Same primary domain belongs to an organisation | `ORGANISATION_ACCESS_REQUIRED`; ask its admin for an invitation. |
| Same display name but a different verified primary domain | Allow a different organisation; names are not identity. |
| Previous signup request is still running | Return the same request and current status; do not enqueue another job. |
| Previous request failed after partial work | Resume from its last committed milestone using the same request. |
| Existing user requests another organisation | Reject in V1 with `ACCOUNT_IN_OTHER_ORGANISATION`. |

Before `control_plane_created`, a retry for the same provider subject and primary domain may update
non-authority profile fields such as name, title, team size, and URLs. After the organisation and
founder exist, organisation identity and user identity are immutable; retries only resume
provisioning. Changing the provider subject or primary domain starts a different intent and is
rejected rather than mutating partially created ownership.

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

- mandatory `Idempotency-Key`;
- password-free durable storage and logs;
- committed milestones before every external boundary;
- bounded reconciliation after timeouts;
- deterministic provider-subject and Supabase identity lookup;
- one master transaction for organisation and founding user;
- safe retry of tenant-default creation.

## Flow B: organisation invitation

### Who can invite

- `admin` with user-management permission: can invite `member` users. Granting `admin` to an
  invitee requires the stronger admin-management permission.
- `member`: cannot invite.
- `platform_super_admin`: operational override only, fully audited.

The organisation may never be left without an active `admin`.

### Single and bulk invite

The same API accepts 1-50 invitation entries. A CSV upload is a frontend convenience that parses
into this request; the backend does not need a separate CSV parser.

Each entry contains:

- normalized email;
- role (`member` or permitted `admin`);
- authentication method (`password`, `google`, or `microsoft_entra`);
- optional organisation/job title metadata.

The backend validates every entry and returns per-entry results. One invalid or duplicate email
must not discard the other valid invitations. Duplicate pending invitations are idempotent and
may be resent rather than recreated.

An explicit invitation is sufficient organisation authority. A contractor or agency user does
not need to share the organisation's email domain. Exact invited email, verified provider
identity, invitation state, and organisation policy are the checks that matter.

### Delivery and acceptance

1. Commit the invitation and an email-outbox event atomically.
2. A worker sends a provider-specific invitation email and records delivery attempts.
3. The email link opens `/accept-invite` with an opaque, single-use token. Store only its hash.
4. The user authenticates with the invitation's required method.
5. The backend verifies exact normalized email, provider/method, token, expiry, organisation
   state, and invitation state.
6. Acceptance creates/activates the user and membership exactly once, marks the invitation
   accepted, and revokes the token.

Invitation email failure must not be reported as successful delivery. The admin UI shows
`queued`, `sent`, `delivered` when available, `failed`, `accepted`, `expired`, or `revoked`, and
offers an idempotent resend action. Invitation expiry should default to seven days.

## API contract

The signup endpoints below are implemented. Invitation endpoints remain target contracts for a
later phase. All responses use the existing CopilotGTM response envelope and stable
machine-readable error codes.

### Signup

#### `POST /users/auth/google/signup`

#### `POST /users/auth/microsoft/signup`

Starts Google Workspace or Microsoft Entra organisation signup using the remotely verified
Supabase bearer session. Public, strongly rate-limited, and requires `Idempotency-Key`.

```json
{
  "first_name": "Jane",
  "last_name": "Doe",
  "job_title": "VP Sales",
  "organisation_name": "Acme",
  "primary_domain": "acme.com",
  "country": "US",
  "sales_team_size": 20,
  "website_url": "https://acme.com",
  "linkedin_url": "https://www.linkedin.com/company/acme",
  "terms_version": "2026-08-02",
  "privacy_version": "2026-08-02"
}
```

Returns `202 Accepted` with `signup_request_id` and a non-secret status resource.

#### `GET /users/auth/signup-requests/{signup_request_id}`

Returns `received`, `control_plane_created`, `tenant_defaults_created`, `storage_created`,
`active`, `reconciliation_required`, or terminal failure status. The remotely verified founding
session must own the request; a UUID alone is not authorization.

### Invitation management

#### `POST /organisations/{organisation_id}/invitation-batches`

Creates 1-50 invitations. Requires organisation admin authorization and
`Idempotency-Key`.

```json
{
  "invitations": [
    {
      "email": "alex@acme.com",
      "role": "member",
      "authentication_method": "google_workspace"
    },
    {
      "email": "sam@agency.example",
      "role": "member",
      "authentication_method": "microsoft_entra"
    }
  ]
}
```

Returns `201 Created` for the durable batch, including counts and per-entry validation results.
Email delivery continues through the outbox worker.

#### `GET /organisations/{organisation_id}/invitation-batches/{batch_id}`

Returns per-invitation processing and delivery state.

#### `GET /organisations/{organisation_id}/invitations`

Lists invitations with pagination and status filters.

#### `POST /organisations/{organisation_id}/invitations/{invitation_id}/resend`

Queues another delivery for a still-valid pending invitation. Rate-limited and idempotent.

#### `DELETE /organisations/{organisation_id}/invitations/{invitation_id}`

Revokes a pending invitation and invalidates its token.

### Invitation acceptance

#### `POST /users/auth/invitations/preview`

Accepts the opaque token in the JSON body and returns non-sensitive organisation name, invited
email mask, role, required authentication method, and expiry. Putting the token in the request
body prevents API access logs from recording it.

#### `POST /users/auth/invitations/accept`

Requires the verified password/provider session and the opaque invitation token in the body.
Returns the active user profile and organisation bootstrap data.

## Stable frontend outcomes

| Code | Frontend action |
| --- | --- |
| `ACCOUNT_ALREADY_EXISTS` | Offer Sign in; do not reveal authentication details before identity verification. |
| `ACCOUNT_NOT_FOUND` | On Sign in, offer an explicit link to Sign up. |
| `ORGANISATION_ACCESS_REQUIRED` | Ask the user to obtain an invitation from an existing admin. |
| `INVITATION_REQUIRED` | The identity is known but has no valid invitation for this organisation. |
| `INVITATION_EXPIRED` | Offer admin resend/contact action. |
| `INVITATION_REVOKED` | Stop; contact organisation admin. |
| `INVITATION_EMAIL_MISMATCH` | Clear provider session and authenticate as the invited email. |
| `PASSWORD_SIGN_IN_REQUIRED` | Show password sign-in. |
| `GOOGLE_SIGN_IN_REQUIRED` | Start Google sign-in. |
| `MICROSOFT_SIGN_IN_REQUIRED` | Start Microsoft sign-in. |
| `ACCOUNT_IN_OTHER_ORGANISATION` | Explain the V1 one-organisation limitation; contact support/admin. |

Public responses must remain enumeration-resistant. More specific account or invitation details
may be returned only after a verified identity or valid invitation token establishes authority.

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
| Organisation invitations | Admin creates a user directly | Durable invitation records with acceptance tokens |
| Bulk invitations | Not available | One request with 1-50 per-entry results |
| Delivery reliability | Direct best-effort email | Transactional outbox, retries, status, resend |
| Approval | User may remain pending after creation | Founder auto-approved; invitee activated on valid acceptance |
| Organisation roles | `admin` / `member` | Reuse organisation `admin`; keep platform super-admin separate and protect the last active admin |
| External invite domain | Must match organisation domain | Exact invitation email is authoritative; cross-domain invite allowed |

## Security and production gates

- Signed OAuth `state` binds intent (`signin`, `signup`, or `accept_invite`), provider, redirect,
  and anti-CSRF nonce.
- Provider session is remotely verified; email must be provider-verified.
- No organisation or admin role is created from a Sign in callback.
- Signup and invitation acceptance are idempotent under duplicate and concurrent requests.
- Domain equality never grants membership or admin rights.
- Invitation tokens are high-entropy, hashed at rest, single-use, expiring, revocable, and never
  logged.
- Signup and invitation endpoints have IP, identity, organisation, and resend rate limits.
- Email uses a durable outbox with bounded retries and observable terminal failure.
- Audit records cover signup, organisation creation, founding-admin assignment, invitation
  create/send/resend/revoke/accept, method mismatch, and rejected linking.
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
- Same verified organisation domain cannot auto-join or auto-promote another user.
- Admin single invite and 50-user mixed-result bulk invite.
- Cross-domain contractor invitation.
- Wrong-email, wrong-provider, expired, revoked, replayed, and concurrent invitation acceptance.
- Email send failure, retry, resend, and terminal delivery visibility.
- Prevention of deleting, suspending, or demoting the last active organisation admin.
- Suspended user/organisation at signup completion, invite acceptance, refresh, and `/me`.
- Retry after Supabase timeout, database rollback, tenant-default failure, and callback replay.

## Remaining delivery order

1. Run migration preflight and apply the combined migration in non-production.
2. Configure the dedicated Supabase Google and Microsoft login applications and redirect allowlists.
3. Run real Google Workspace and Microsoft Entra signup, duplicate-domain, retry, and email smoke tests.
4. Build separate frontend Sign up and Sign in routes on the shared authentication shell.
5. Release signup through normal environment gates with worker and reconciliation monitoring.
6. Design and implement the invitation subsystem as a separate change, including outbox delivery,
   bulk results, acceptance tokens, and the last-active-admin guard.

## External references

- HubSpot: [Set up your HubSpot account](https://knowledge.hubspot.com/get-started/set-up-your-account)
- HubSpot: [Add HubSpot users](https://knowledge.hubspot.com/account-management/add-hubspot-users)
- Salesforce: [Try Out Salesforce](https://help.salesforce.com/s/articleView?id=sf.getstart.htm&language=en_US&type=5)
- Salesforce: [Add a Single User](https://help.salesforce.com/s/articleView?id=sf.adding_new_users.htm&language=en_US&type=5)
