# HubSpot Deal Webhook Flow

## Core Idea

Use HubSpot webhooks only as **change notifications**. Do not write data directly from the webhook payload. Use `portalId` and `objectId` from the webhook, then fetch the full deal from HubSpot and write through our existing HubSpot deal sync logic.

## Best Flow

```text
HubSpot deal changes
  -> HubSpot sends webhook to /users/integrations/hubspot/webhook
  -> API verifies HubSpot signature
  -> API dedupes event
  -> API queues Celery task
  -> Worker maps portalId to our HubSpot integration
  -> Worker fetches full deal by objectId
  -> Existing deal normalizer/writers upsert our CRM tables
```

## First Events To Enable

| HubSpot event | Our action |
| --- | --- |
| `deal.creation` | Fetch full deal and upsert. |
| `deal.propertyChange` | Fetch full deal and upsert, only for selected properties. |
| `deal.deletion` | Soft-delete local deal by `organisation_id + integration_id + external_id`. |

## Property Updates To Track First

- `dealname`
- `amount`
- `dealstage`
- `pipeline`
- `closedate`
- `hubspot_owner_id`
- `dealtype`
- `hs_priority`
- `hs_forecast_amount`
- `hs_forecast_probability`
- `hs_deal_stage_probability`

## Important Rules

- Webhook payload is not the full deal. Always fetch the full deal.
- Return fast from the API; do HubSpot fetch/write in worker.
- Verify HubSpot signature before processing.
- Handle duplicates; HubSpot can retry.
- Do not rely on event order.
- Existing users do **not** need to reintegrate if we only use already-granted `crm.objects.deals.read`.
- Users need reintegration only if we add new scopes.

## Activation

1. Deploy backend support.
2. Add HubSpot subscriptions in the legacy app.
3. Activate `deal.creation` and test.
4. Activate one `deal.propertyChange` property and test.
5. Activate `deal.deletion` and test.
6. Enable remaining selected properties.

## Sources

- https://developers.hubspot.com/docs/api-reference/legacy/webhooks/guide
- https://developers.hubspot.com/docs/apps/legacy-apps/authentication/validating-requests
