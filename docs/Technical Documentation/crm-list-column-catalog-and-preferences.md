# CRM List Columns and Saved Preferences

> Status: Proposed
> Scope: Deals, Contacts, and Accounts list pages
> Reading time: about 4 minutes

## Decision

CRM column metadata does **not** belong in the login response. Login should establish the
session and return user identity and access information. A user may never open a CRM page,
and column definitions can change independently when an administrator adds, renames, or
disables a custom field.

Use one reusable list-configuration contract, parameterized by entity. Do not create three
copies of the same API, and do not return every entity's configuration immediately after
login.

- `GET /users/crm/list-configuration/{entity}` returns the available columns plus that
  user's effective selection. `entity` is `deals`, `contacts`, or `accounts`.
- `PUT /users/preference/crm/lists/{entity}` saves the complete ordered selection and
  sorting for that user.
- The existing Deals, Contacts, and Accounts list APIs remain separate because their
  records, filters, permissions, and queries are different.

The frontend calls the configuration API when the relevant list page opens and may cache
the result for that session.

```text
Login -> identity/session only
Open Deals -> load Deals configuration -> load Deals rows
Change columns -> save Deals preference -> refresh rows if a new value is needed
```

## Why the current shape is insufficient

Today, `user_preferences` is included in login and `/users/auth/me`. Backend normalization
adds a fixed list of column keys for Contacts, Accounts, Deals, and Tasks. This has four
problems:

1. Authentication is coupled to CRM presentation.
2. A list of strings provides no label, data type, source, visibility rule, or rendering
   information.
3. The backend can save list sorting, but there is no API that saves a user's column
   selection.
4. Field support is uneven: Deals has a standard-plus-custom schema endpoint, while
   Contacts and Accounts expose fixed field metadata only in detail responses. Deal list
   rows also do not currently return custom-column values.

The fixed defaults are still useful, but they should become the default selection inside a
column catalogue rather than part of the authentication contract.

## Configuration contract

The read API composes two different concerns into one page-ready response:

- **Catalogue:** organisation-scoped standard and active custom fields.
- **Preference:** user-scoped visibility, order, and sorting.

Example:

```json
{
  "entity": "deals",
  "columns": [
    {
      "id": "name",
      "key": "name",
      "label": "Deal name",
      "data_type": "string",
      "source": "standard",
      "value_path": "name",
      "visible": true,
      "default_visible": true,
      "position": 0,
      "sortable": true,
      "hideable": false
    },
    {
      "id": "custom:8be09c1a-1c6e-4fc4-9f07-a8401aef1234",
      "key": "renewal_date",
      "label": "Renewal date",
      "data_type": "date",
      "source": "custom",
      "value_path": "custom_columns.renewal_date",
      "visible": false,
      "default_visible": false,
      "position": null,
      "sortable": false,
      "hideable": true
    }
  ],
  "sorting": {"column_id": "expected_close_date", "direction": "asc"}
}
```

Important rules:

- `id` is the value saved in preferences. Standard fields keep their existing stable key;
  custom fields use their immutable field ID so a label change does not break a saved view.
- `key` and `value_path` tell the frontend where the value is found; they are not accepted
  blindly from a write request.
- `visible` is the user's effective state. `default_visible` supports Reset to default.
- At least one identity column per entity, such as Deal name, Contact name, or Account name,
  is not hideable.
- Only columns that the corresponding list API can actually supply are included. Metadata
  must not advertise an unusable column.

Hidden column definitions should be returned so the user can choose them. Their value does
not need to be returned on every row. List APIs may keep their existing fixed fields during
the first implementation, but must expose selected custom values in a `custom_columns` map.
The frontend passes the visible column IDs with the list request; the backend resolves them
through the catalogue rather than trusting database paths from the client. Omitting them uses
the backend default selection. This keeps the catalogue complete without making every
paginated response unnecessarily large.

## Saving a preference

The write is a full, idempotent replacement of one list view, not a sequence of fragile
show/hide mutations:

```json
{
  "column_ids": [
    "name",
    "stage",
    "amount",
    "custom:8be09c1a-1c6e-4fc4-9f07-a8401aef1234"
  ],
  "sorting": {"column_id": "amount", "direction": "desc"}
}
```

Reuse the existing `public.users.preferences` JSONB document; a new table is not needed for
personal list settings. Keep the current per-entity shape so migration is small:

```json
{
  "crm": {
    "deals": {
      "list": {
        "columns": ["name", "stage", "amount"],
        "sort_by": "amount",
        "sort_order": "desc"
      }
    }
  }
}
```

The service must lock the user row, merge only the requested entity branch, and preserve all
other user preferences. Last write wins for this low-risk UI setting. If the product later
needs shared views, view history, or multiple named layouts, that is the point to introduce
a dedicated list-view table.

On every save, the backend must reject duplicates, unknown fields, inactive fields,
non-sortable sort keys, an empty selection, and selections above a bounded maximum. It must
also enforce the required identity column. The frontend is not a security boundary.

## Source of truth and lifecycle rules

Build one catalogue service from the field definitions already present in the backend:

- Deals: reuse the canonical deal schema service and its standard/custom definitions.
- Contacts and Accounts: move their existing fixed detail-field definitions behind the same
  catalogue interface instead of copying another hard-coded list.
- Custom fields: include active, non-deleted, organisation-scoped definitions only. Contact
  and Account custom fields should appear only after their storage and list-value paths are
  implemented; the current custom-field entity model does not yet support them.

When a custom field is renamed, its saved ID remains valid and the new label is shown. When
it is disabled or deleted, omit it from the effective selection; if no valid visible column
remains, fall back to the backend default. A later save removes stale IDs. All catalogue and
row queries remain tenant-scoped and use the caller's normal CRM access policy.

## Rollout

1. Add the shared catalogue service and the generic configuration read API.
2. Extend the existing CRM list preference service with the full-selection `PUT`; keep the
   current sorting-only endpoint temporarily for compatibility.
3. Add custom values to list rows, starting with Deals where the schema already exists, then
   Contacts and Accounts when their custom-field storage is supported.
4. Update each frontend list page to load its configuration on entry and save changes through
   the preference API.
5. After all clients migrate, remove CRM columns from login and `/users/auth/me` while leaving
   unrelated user preferences untouched.

This produces a clean boundary: authentication owns access, the catalogue owns what can be
shown, list APIs own record data, and user preferences own how each person chooses to see it.
