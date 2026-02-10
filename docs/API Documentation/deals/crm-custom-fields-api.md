# CRM Custom Fields API

Base prefix: `/users/crm`

## Overview

Custom field definitions are organisation-scoped and currently support `entity=deal`.
These definitions are used by frontend forms and CSV import mapping.

## Endpoints

### 1. List custom fields

- **Method**: `GET`
- **Path**: `/custom-fields`
- **Query params**:
  - `entity` (`deal`, default `deal`)
  - `include_inactive` (`true|false`, default `false`)

Example:

```bash
curl -X GET "http://localhost:8000/users/crm/custom-fields?entity=deal" \
  -H "Authorization: Bearer <token>"
```

### 2. Create custom field

- **Method**: `POST`
- **Path**: `/custom-fields`

Example:

```json
{
  "entity": "deal",
  "key": "custom_our_revenue",
  "label": "Our Revenue",
  "data_type": "number",
  "required": false,
  "options": {},
  "validation": {},
  "formula": null
}
```

Notes:
- `key` is normalized and stored without `custom_` prefix.
- `data_type=select|multiselect` requires `options.values` list.
- `data_type=formula` requires a `formula` object.
- Supported scalar `data_type` values include `text`, `number`, `integer`, `boolean`, `date`, `datetime`, and `currency`.

### 3. Update custom field

- **Method**: `PATCH`
- **Path**: `/custom-fields/{field_id}`
- **Body**: any subset of mutable fields:
  - `label`, `required`, `is_active`, `display_order`, `description`,
  - `options`, `validation`, `formula`

### 4. Archive custom field

- **Method**: `DELETE`
- **Path**: `/custom-fields/{field_id}`

Behavior:
- Soft delete (`is_deleted=true`, `is_active=false`)
- Predefined fields are protected from delete.

### 5. Validate formula

- **Method**: `POST`
- **Path**: `/custom-fields/validate-formula`

Example:

```json
{
  "entity": "deal",
  "formula": {
    "op": "add",
    "args": [
      { "field": "our_revenue" },
      { "field": "services_revenue" }
    ]
  }
}
```

Supported formula ops:
- `add`, `sub`, `mul`, `div`, `concat`, `coalesce`, `if`

Response includes resolved `dependencies`.
