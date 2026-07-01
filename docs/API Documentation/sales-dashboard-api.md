# Sales Dashboard API

## Base URL

```text
/users/dashboard
```

## Overview

The Sales Dashboard is the application-provided dashboard rendered by backend code.
Default, unfiltered Sales Dashboard payloads are cached in the tenant
`org_*.dashboards` table as the `sales` dashboard snapshot. Filtered requests
and non-default primary segments still render live so temporary request filters
do not overwrite the default snapshot.

Saved user preferences live under `public.users.preferences.dashboard.main`.
Request query filters are temporary overrides and are not saved automatically.

The endpoint returns the complete 12-widget DeepMatrix backend contract. Some
slippage metrics are best-effort until detailed expected-close history is stored.

## Response Envelope

All endpoints return the standard API envelope:

```json
{
  "status": "SUCCESS",
  "message": "Human-readable message",
  "data": {}
}
```

## Get Sales Dashboard

```http
GET /users/dashboard/sales
```

Returns the authenticated user's application-provided Sales Dashboard.

### Authentication

Required.

### Query Parameters

| Name | Type | Notes |
| --- | --- | --- |
| `date_range` | string | `last_30_days`, `last_90_days`, `this_quarter`, `year_to_date` |
| `primary_segment` | string | `stage`, `pipeline`, `status`, `owner` |
| `from_date` | datetime | Explicit lower bound. Overrides the computed `date_range` lower bound. |
| `to_date` | datetime | Explicit upper bound. Overrides the computed `date_range` upper bound. |
| `pipeline_ids` | UUID list | Filters CRM deal widgets to selected pipelines. |
| `owner_ids` | UUID list | Filters CRM deal widgets to selected owners. |
| `include_widgets` | string list | Optional partial widget load. Empty means all currently supported widgets. |

### Widget IDs

| Widget ID | Purpose |
| --- | --- |
| `total_pipeline` | Open pipeline value and total filtered deals. |
| `active_deals` | Open deal count and overdue open deal count. |
| `extended_deals` | Deals with available expected-close extension counters. |
| `average_deal_age` | Average age of filtered deals. |
| `primary_distribution` | Count/value grouped by stage, pipeline, status, or owner. |
| `average_sales_cycle_by_value_bucket` | Average deal age grouped by configured value buckets. |
| `stuck_stages` | Stale open deals grouped by current stage. |
| `extension_frequency_by_stage` | Expected-close extension frequency grouped by stage. |
| `owner_slippage` | Expected-close extension frequency grouped by owner. |
| `stage_movement_flow` | Stage transition analytics for the selected time window. |
| `no_movement_deals` | Top open deals with no recent stage movement. |
| `slipped_overdue_deals` | Top slipped or overdue deals using available close-date fields. |

### Success Response

```json
{
  "status": "SUCCESS",
  "message": "Sales Dashboard retrieved successfully",
  "data": {
    "dashboard_id": "sales",
    "schema_version": 1,
    "title": "Sales Dashboard",
    "generated_at": "2026-05-06T12:00:00Z",
    "effective_filters": {
      "date_range": "last_90_days",
      "primary_segment": "stage",
      "from_date": "2026-02-05T12:00:00Z",
      "to_date": "2026-05-06T12:00:00Z",
      "pipeline_ids": [],
      "owner_ids": []
    },
    "sections": [
      {
        "section_id": "summary",
        "title": "Summary",
        "widgets": []
      },
      {
        "section_id": "segmentation",
        "title": "Segmentation",
        "widgets": []
      },
      {
        "section_id": "diagnostics",
        "title": "Diagnostics",
        "widgets": []
      },
      {
        "section_id": "movement",
        "title": "Stage Movement",
        "widgets": []
      },
      {
        "section_id": "deal_tables",
        "title": "Deal Tables",
        "widgets": []
      }
    ],
    "warnings": []
  }
}
```

## Get Preferences

```http
GET /users/dashboard/sales/preferences
```

Returns the authenticated user's saved Sales Dashboard preferences.

### Success Response

```json
{
  "status": "SUCCESS",
  "message": "Sales Dashboard preferences retrieved successfully",
  "data": {
    "filters": {
      "date_range": "last_90_days"
    },
    "layout_density": "comfortable",
    "collapsed_widget_ids": [],
    "hidden_widget_ids": []
  }
}
```

## Update Preferences

```http
PATCH /users/dashboard/sales/preferences
```

Updates only the authenticated user's Sales Dashboard preferences under
`public.users.preferences.dashboard.main`.

This endpoint must not write organisation-level dashboard preferences.

### Request Body

```json
{
  "filters": {
    "date_range": "last_90_days",
    "pipeline_ids": []
  },
  "layout_density": "comfortable",
  "collapsed_widget_ids": [],
  "hidden_widget_ids": ["stage_movement_flow"]
}
```

### Success Response

```json
{
  "status": "SUCCESS",
  "message": "Sales Dashboard preferences updated successfully",
  "data": {
    "filters": {
      "date_range": "last_90_days",
      "pipeline_ids": []
    },
    "layout_density": "comfortable",
    "collapsed_widget_ids": [],
    "hidden_widget_ids": ["stage_movement_flow"]
  }
}
```

