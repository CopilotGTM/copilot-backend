"""HubSpot deal webhook subscription names and property filters.

The broad property list tells HubSpot which deal changes should notify us, but
the webhook handler still fetches a full deal snapshot before writing. Keeping
that contract avoids partial patch semantics diverging from incremental sync.
"""

from __future__ import annotations

from src.service.integrations.crm.schema.hubspot.deals.mapping import HUBSPOT_DEAL_CANONICAL_FIELDS

DEAL_CREATION = "deal.creation"
DEAL_PROPERTY_CHANGE = "deal.propertyChange"
DEAL_DELETION = "deal.deletion"

DEAL_PROPERTY_CHANGE_PROPERTIES: tuple[str, ...] = tuple(
    sorted(
        set(HUBSPOT_DEAL_CANONICAL_FIELDS)
        | {
            "amount_in_home_currency",
            "closed_amount",
            "currency",
            "hs_acv",
            "hs_all_collaborator_owner_ids",
            "hs_all_deal_split_owner_ids",
            "hs_arr",
            "hs_closed_lost_date",
            "hs_closed_reason",
            "hs_closed_won_date",
            "hs_date_exited_current_stage",
            "hs_exchange_rate",
            "hs_expected_close_date",
            "hs_forecast_amount",
            "hs_forecast_probability",
            "hs_is_closed",
            "hs_is_closed_lost",
            "hs_is_closed_won",
            "hs_lead_source",
            "hs_mrr",
            "hs_object_url",
            "hs_projected_amount_in_home_currency",
            "hs_recurring_revenue_amount",
            "hs_recurring_revenue_deal_type",
            "hs_recurring_revenue_end_date",
            "hs_recurring_revenue_inactive_reason",
            "hs_tcv",
            "hs_time_in_current_stage",
            "lastactivitydate",
            "lead_source",
            "num_associated_companies",
        }
    )
)
DEAL_PROPERTY_CHANGE_PROPERTY_SET = frozenset(DEAL_PROPERTY_CHANGE_PROPERTIES)

DEAL_SUBSCRIPTIONS = (
    DEAL_CREATION,
    DEAL_PROPERTY_CHANGE,
    DEAL_DELETION,
)
