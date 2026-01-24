# CRM Settings API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-01-22

---

## Overview

Settings APIs provide dropdown options for deal forms. These endpoints return static/configurable values and are designed for caching.

---

## 1. Get Currencies

Returns available currencies for deal amount selection.

### Endpoint

```
GET /users/crm/settings/currencies
```

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Currencies retrieved successfully",
  "data": {
    "items": [
      {
        "code": "USD",
        "name": "US Dollar",
        "symbol": "$"
      },
      {
        "code": "INR",
        "name": "Indian Rupee",
        "symbol": "₹"
      }
    ],
    "default_code": "USD"
  }
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `items` | array | List of currency options |
| `items[].code` | string | ISO 4217 currency code (3 chars) |
| `items[].name` | string | Currency display name |
| `items[].symbol` | string | Currency symbol |
| `default_code` | string | Default currency code |

### Frontend Usage

```typescript
// Fetch currencies on modal open
const { data } = await crmApiClient.get('/settings/currencies');
const currencies = data.data.items;
const defaultCurrency = data.data.default_code;

// Build dropdown options
const currencyOptions = currencies.map(c => ({
  value: c.code,
  label: `${c.code} - ${c.name}`,
  symbol: c.symbol
}));
```

---

## 2. Get All Deal Form Options

Returns all dropdown options in a single API call. Use this to minimize network requests when opening deal forms.

### Endpoint

```
GET /users/crm/settings/deal-options
```

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal form options retrieved successfully",
  "data": {
    "currencies": {
      "items": [
        { "code": "USD", "name": "US Dollar", "symbol": "$" },
        { "code": "INR", "name": "Indian Rupee", "symbol": "₹" }
      ],
      "default_code": "USD"
    },
    "deal_types": {
      "items": [
        { "value": "New Business", "label": "New Business", "description": "First-time customer deal" },
        { "value": "Renewal", "label": "Renewal", "description": "Existing customer renewal" },
        { "value": "Expansion", "label": "Expansion", "description": "Expanding existing contract" },
        { "value": "Upsell", "label": "Upsell", "description": "Upgrading to higher tier" }
      ],
      "default_value": null
    },
    "priorities": {
      "items": [
        { "value": "High", "label": "High", "description": "Urgent, needs immediate attention" },
        { "value": "Medium", "label": "Medium", "description": "Normal priority" },
        { "value": "Low", "label": "Low", "description": "Can be addressed later" }
      ],
      "default_value": "Medium"
    },
    "lead_sources": {
      "items": [
        { "value": "Inbound", "label": "Inbound", "description": "Customer reached out" },
        { "value": "Outbound", "label": "Outbound", "description": "Sales outreach" },
        { "value": "Referral", "label": "Referral", "description": "Referred by existing customer" },
        { "value": "Partner", "label": "Partner", "description": "Partner referral" },
        { "value": "Event", "label": "Event", "description": "Trade show or conference" },
        { "value": "Website", "label": "Website", "description": "Website form submission" },
        { "value": "Other", "label": "Other", "description": "Other source" }
      ],
      "default_value": null
    },
    "forecast_categories": {
      "items": [
        { "value": "Pipeline", "label": "Pipeline", "description": "In pipeline, not yet qualified" },
        { "value": "Best Case", "label": "Best Case", "description": "Possible but not committed" },
        { "value": "Commit", "label": "Commit", "description": "Highly likely to close" },
        { "value": "Closed", "label": "Closed", "description": "Deal is closed" }
      ],
      "default_value": "Pipeline"
    }
  }
}
```

### Response Structure

| Field | Type | Description |
|-------|------|-------------|
| `currencies` | object | Currency options with default |
| `deal_types` | object | Deal type options |
| `priorities` | object | Priority options with default |
| `lead_sources` | object | Lead source options |
| `forecast_categories` | object | Forecast category options (Phase 3) |

### Dropdown Option Fields

| Field | Type | Description |
|-------|------|-------------|
| `value` | string | Value to store in database |
| `label` | string | Display label for UI |
| `description` | string | Optional tooltip/help text |

### Frontend Usage

```typescript
// Fetch all options once and cache
const { data } = await crmApiClient.get('/settings/deal-options');
const options = data.data;

// Use in form
<select>
  {options.deal_types.items.map(opt => (
    <option key={opt.value} value={opt.value}>{opt.label}</option>
  ))}
</select>
```

### Caching Recommendation

These options rarely change. Recommended cache settings:
- **staleTime**: 5 minutes
- **gcTime**: 30 minutes

```typescript
const { data } = useQuery({
  queryKey: ['crm_deal_options'],
  queryFn: () => crmApiClient.get('/settings/deal-options'),
  staleTime: 5 * 60 * 1000,  // 5 minutes
  gcTime: 30 * 60 * 1000,    // 30 minutes
});
```

---

## TypeScript Types

```typescript
// Currency types
interface CurrencyOption {
  code: string;      // "USD", "INR"
  name: string;      // "US Dollar", "Indian Rupee"
  symbol: string;    // "$", "₹"
}

interface CurrencyListResponse {
  items: CurrencyOption[];
  default_code: string;
}

// Dropdown types
interface DropdownOption {
  value: string;
  label: string;
  description?: string;
}

interface DropdownOptionsResponse {
  items: DropdownOption[];
  default_value: string | null;
}

// Full response
interface DealFormOptionsResponse {
  currencies: CurrencyListResponse;
  deal_types: DropdownOptionsResponse;
  priorities: DropdownOptionsResponse;
  lead_sources: DropdownOptionsResponse;
  forecast_categories: DropdownOptionsResponse;
}
```

---

## Error Responses

| Status | Description |
|--------|-------------|
| 401 | Unauthorized - Invalid or missing token |
| 429 | Rate limited - Too many requests |
| 500 | Internal server error |

---

## Related Documents

- `crm-deals-api.md` - Deals CRUD API documentation
- `crm-users-api.md` - User search API documentation
