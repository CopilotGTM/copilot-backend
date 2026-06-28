# Contact Email Domain Resolution Policy

## Goal

When a contact is created from an email, we may use the email domain to link or
create a CRM account. This is useful in sales workflows because a buyer's work
email often identifies the account.

But we must not create accounts from personal email providers such as Gmail,
Outlook, Yahoo, or iCloud. A contact with `person@gmail.com` is still a valid
contact, but `gmail.com` is not a valid sales account.

The policy should be conservative:

- If the domain looks like a company domain, use it.
- If the domain looks like a personal/free email provider, skip account
  resolution.
- If we are unsure, create the contact without an account link.

False negative is acceptable. False positive pollutes CRM data.

## Scope

This policy applies only to automatic account inference from contact email.

It does not change:

- manual account creation
- explicit account linking by `company_id`
- account search
- account update rules
- contact creation inputs

Contact creation still accepts only:

- `email`
- optional `name`
- optional `job_title`
- optional `linkedin_url`

## Sales Perspective

For sales teams, accounts should represent companies or buying organizations.

Good automatic account candidates:

- `jane@acme.com` -> `acme.com`
- `buyer@lemonedge.com` -> `lemonedge.com`
- `sam@northwindtraders.co.uk` -> `northwindtraders.co.uk`

Bad automatic account candidates:

- `jane@gmail.com`
- `sam@outlook.com`
- `buyer@yahoo.com`
- `nick@enter.gmail.com`
- `lead@abc.mail.yahoo.com`

The contact should still be created in all cases. We only skip the account
link/create step.

## Domain Resolution Rules

### 1. Parse Email

Normalize the email:

- trim whitespace
- lowercase
- require one valid domain after `@`

If the email is invalid or has no usable domain, return `None`.

### 2. Normalize Domain

Normalize the domain:

- lowercase
- remove `http://` or `https://` if present
- remove leading `www.`
- strip path, query string, and fragment if present

Examples:

| Input | Normalized |
|-------|------------|
| `ACME.COM` | `acme.com` |
| `https://www.acme.com/about` | `acme.com` |
| `buyer@Sub.Acme.com` | `sub.acme.com` |

### 3. Block Free Email Providers

Maintain a deny-list of common personal/free email providers.

Initial list:

- `gmail.com`
- `googlemail.com`
- `outlook.com`
- `hotmail.com`
- `live.com`
- `msn.com`
- `yahoo.com`
- `ymail.com`
- `icloud.com`
- `me.com`
- `mac.com`
- `aol.com`
- `protonmail.com`
- `proton.me`
- `zoho.com`
- `mail.com`
- `gmx.com`
- `gmx.net`

Important: block both the exact domain and subdomains.

Examples:

| Email domain | Result |
|--------------|--------|
| `gmail.com` | blocked |
| `enter.gmail.com` | blocked |
| `mail.outlook.com` | blocked |
| `abc.yahoo.com` | blocked |
| `acme.com` | allowed |

### 4. Return Safe Domain

If the domain passes the free-email check, return it as the candidate business
domain.

If it is blocked, return `None`.

## Account Resolution Flow

Contact creation calls the account-domain resolver.

If resolver returns a domain:

1. Search active accounts in the same organisation by that normalized domain.
2. If exactly one account exists, link contact to it.
3. If no account exists, create an account using:
   - `name = domain`
   - `domain = domain`
4. If multiple accounts exist, create the contact unlinked.

If resolver returns `None`:

- create the contact
- leave `company_id` and `company_name` empty

## Service Ownership

Domain safety policy should live in one place:

`AccountDomainResolutionService`

This service owns:

- email-domain extraction
- domain normalization
- free-email provider checks
- deciding whether a domain is safe for automatic account inference

`AccountWriteService` owns:

- account lookup
- account creation
- duplicate checks
- mutation events

`ContactWriteService` owns:

- contact creation
- applying returned account link to the contact

The contact writer should not contain provider lists or domain-policy logic.

## Future Additions

We can add these later without changing contact creation inputs:

- disposable email provider blocklist
- reserved/test domains such as `example.com`
- role mailbox checks such as `support@`, `info@`, `noreply@`
- internal company domain checks
- public suffix handling for better registrable-domain extraction
- logging when account inference is skipped

For now, the production-safe first step is simple:

> Do not create or link accounts from free/personal email providers, including
> their subdomains.

## Test Cases

Required tests:

- `jane@gmail.com` returns `None`
- `jane@enter.gmail.com` returns `None`
- `jane@outlook.com` returns `None`
- `jane@mail.outlook.com` returns `None`
- `jane@yahoo.com` returns `None`
- `jane@acme.com` returns `acme.com`
- `jane@sub.acme.com` returns `sub.acme.com`
- invalid email returns `None`

Contact creation tests:

- free-email contact is created without account link
- business-email contact resolves or creates account
