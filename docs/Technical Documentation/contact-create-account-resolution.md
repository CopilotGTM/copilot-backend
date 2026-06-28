# Contact Create Account Resolution

## Goal

When a contact is created with an email, use the email domain to attach the contact
to an account automatically.

Keep the logic simple:

- Contact creation owns contact fields.
- Account domain resolution owns domain safety checks.
- Account write service owns account lookup and account creation.
- The contact writer only asks the account service for the account to attach.

## Create Contact Inputs

The create-contact command remains:

- `email` required
- `name` optional
- `job_title` optional
- `linkedin_url` optional

No account parameters are added back to the API/tool contract. The caller should
not send `company_id`, `account_id`, `account_name`, or `account_domain`.

## Account Service Contract

`AccountWriteService` exposes the small account-resolution method:

```python
async def resolve_or_create_account_for_contact_email(
    self,
    email: str,
    *,
    context: AccountWriteContext | None = None,
) -> CRMCompany | None:
    ...
```

Behavior:

1. Ask `AccountDomainResolutionService` for a safe business domain from the email.
2. If the email has no usable or safe business domain, return `None`.
3. Search active accounts in the same organisation by normalized domain.
4. If exactly one account exists, return it.
5. If no account exists, create an internal CRM account:
   - `name = domain`
   - `domain = domain`
   - same org-scoped session
   - caller still owns commit/rollback
6. If multiple accounts exist with the same domain, return `None` for now and do
   not block contact creation.

The multiple-match case should be observable with logging later, but it should
not make contact creation fail.

## Contact Writer Flow

After duplicate contact check and before creating the `CRMContact` row:

1. Call `AccountWriteService.resolve_or_create_account_for_contact_email(email)`.
2. If it returns an account:
   - set `contact.company_id = account.company_id`
   - set `contact.company_name = account.name`
3. If it returns `None`:
   - create the contact without account fields

The contact writer should not directly:

- normalize domains
- decide whether a domain is safe for account inference
- query accounts by domain
- create account rows
- decide account duplicate policy

## Transaction Behavior

The contact and any newly created account are written in the same org-scoped
session. The outer caller owns transaction control:

- API commits after contact creation succeeds.
- Tool commits after contact creation succeeds.
- Failures roll back both the contact and any new account staged in the same
  transaction.

## Expected Outcomes

| Input email | Existing account state | Contact result |
|-------------|------------------------|----------------|
| `jane@acme.com` | One account with `domain = acme.com` | Contact linked to that account |
| `jane@acme.com` | No account for `acme.com` | Account `acme.com` created and contact linked |
| `jane@acme.com` | Multiple accounts with `domain = acme.com` | Contact created unlinked |
| `jane@gmail.com` | Any | Contact created unlinked |
| `jane@enter.gmail.com` | Any | Contact created unlinked |
| `jane@test.hackme.com` | No account for `test.hackme.com` | Account `test.hackme.com` created and contact linked |
| invalid/no domain | Any | Contact created unlinked |

## Tests Added

- Creating a contact links to an existing single-domain account.
- Creating a contact creates and links a new account when no domain account exists.
- Free/personal email domains and their subdomains do not trigger account lookup or creation.
- Business subdomains are preserved as the account domain.
- Creating a contact stays unlinked when multiple domain accounts exist.
- Creating a contact stays unlinked when email has no usable domain.
- Account lookup/create assertions live around account service behavior; contact
  writer tests only verify it consumes the returned account.
