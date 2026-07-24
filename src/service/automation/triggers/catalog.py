"""Backend-owned catalog for trigger-builder rendering.

Only definitions with a real source contract belong here. Adding display metadata without an event
producer would let users save automation that can never match.
"""

from __future__ import annotations

from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCatalog,
    AutomationTriggerOptionSets,
    TriggerCatalogCategory,
    TriggerCatalogDefinition,
    TriggerCatalogField,
    TriggerCatalogPipelineOption,
)
from src.domain.models.organisation_tables import AutomationTriggerType


def build_trigger_catalog(
    pipelines: list[TriggerCatalogPipelineOption],
) -> AutomationTriggerCatalog:
    """Build the exact trigger definitions and organization options supported by V1."""

    return AutomationTriggerCatalog(
        categories=[
            TriggerCatalogCategory(
                category_key="schedule",
                display_name="Schedule",
                description="Run an agent once or on a recurring local-time schedule.",
                triggers=[
                    TriggerCatalogDefinition(
                        trigger_key="schedule",
                        display_name="Scheduled time",
                        description="Runs at the selected time in the selected IANA timezone.",
                        trigger_type=AutomationTriggerType.SCHEDULE,
                        source_domain=None,
                        event_type=None,
                        default_trigger_config={
                            "timezone": "UTC",
                            "starts_at": None,
                            "recurrence": "none",
                        },
                        default_scope_config={"entity": "deal", "statuses": ["open"]},
                        fields=[
                            TriggerCatalogField(
                                field_key="timezone",
                                display_name="Timezone",
                                description="IANA timezone used to preserve recurring local time.",
                                input_type="timezone",
                                value_type="string",
                                allows_any=False,
                            ),
                            TriggerCatalogField(
                                field_key="starts_at",
                                display_name="Starts at",
                                description="Timezone-aware first occurrence.",
                                input_type="datetime",
                                value_type="date_time",
                                allows_any=False,
                            ),
                            TriggerCatalogField(
                                field_key="recurrence",
                                display_name="Repeats",
                                description="One-time or supported recurring cadence.",
                                input_type="select",
                                value_type="string",
                                options=[
                                    "none",
                                    "daily",
                                    "weekdays",
                                    "weekly",
                                    "biweekly",
                                    "monthly",
                                ],
                                allows_any=False,
                            ),
                        ],
                    )
                ],
            ),
            TriggerCatalogCategory(
                category_key="crm",
                display_name="CRM",
                description="Run an agent from durable semantic CRM events.",
                triggers=[
                    TriggerCatalogDefinition(
                        trigger_key="crm.deal.created",
                        display_name="Deal created",
                        description="Runs when a new owned deal is created.",
                        trigger_type=AutomationTriggerType.EVENT,
                        source_domain="crm",
                        event_type="crm.deal.created",
                        default_filter_config={
                            "schema_key": "crm.deal.created.v1",
                            "pipeline_ids": [],
                        },
                        fields=[
                            TriggerCatalogField(
                                field_key="pipeline_ids",
                                display_name="Pipeline",
                                description="Empty means any pipeline; otherwise select organization pipelines.",
                                input_type="pipeline_multiselect",
                                value_type="uuid_array",
                                option_source="pipelines",
                            )
                        ],
                    ),
                    TriggerCatalogDefinition(
                        trigger_key="crm.deal.stage_changed",
                        display_name="Deal stage changed",
                        description="Runs when an owned deal changes stage or reaches won/lost.",
                        trigger_type=AutomationTriggerType.EVENT,
                        source_domain="crm",
                        event_type="crm.deal.stage_changed",
                        default_filter_config={
                            "schema_key": "crm.deal.stage_changed.v1",
                            "pipeline_ids": [],
                            "stage_change_mode": "any_change",
                            "from_stage_ids": [],
                            "to_stage_ids": [],
                            "outcome": "any",
                        },
                        fields=[
                            TriggerCatalogField(
                                field_key="pipeline_ids",
                                display_name="Pipeline",
                                description="Empty means any pipeline; otherwise select owned organization pipelines.",
                                input_type="pipeline_multiselect",
                                value_type="uuid_array",
                                option_source="pipelines",
                            ),
                            TriggerCatalogField(
                                field_key="stage_change_mode",
                                display_name="Change",
                                description="Choose any movement, a won/lost outcome, or one-pipeline stages.",
                                input_type="select",
                                value_type="string",
                                options=["any_change", "outcome", "specific_stage"],
                                allows_any=False,
                            ),
                            TriggerCatalogField(
                                field_key="outcome",
                                display_name="Outcome",
                                description="Won or lost when outcome mode is selected.",
                                input_type="select",
                                value_type="string",
                                options=["any", "won", "lost"],
                            ),
                            TriggerCatalogField(
                                field_key="from_stage_ids",
                                display_name="From stage",
                                description="Optional previous stages for one selected pipeline.",
                                input_type="stage_multiselect",
                                value_type="uuid_array",
                                option_source="pipeline_stages",
                                depends_on="pipeline_ids",
                            ),
                            TriggerCatalogField(
                                field_key="to_stage_ids",
                                display_name="To stage",
                                description="Optional destination stages for one selected pipeline.",
                                input_type="stage_multiselect",
                                value_type="uuid_array",
                                option_source="pipeline_stages",
                                depends_on="pipeline_ids",
                            ),
                        ],
                    ),
                    TriggerCatalogDefinition(
                        trigger_key="crm.contact.created",
                        display_name="Contact created",
                        description="Runs when a new owned contact is created.",
                        trigger_type=AutomationTriggerType.EVENT,
                        source_domain="crm",
                        event_type="crm.contact.created",
                        default_filter_config={"schema_key": "crm.contact.created.v1"},
                    ),
                    TriggerCatalogDefinition(
                        trigger_key="crm.contact.deleted",
                        display_name="Contact deleted",
                        description="Runs when an owned contact is deleted.",
                        trigger_type=AutomationTriggerType.EVENT,
                        source_domain="crm",
                        event_type="crm.contact.deleted",
                        default_filter_config={"schema_key": "crm.contact.deleted.v1"},
                    ),
                    TriggerCatalogDefinition(
                        trigger_key="crm.account.created",
                        display_name="Account created",
                        description="Runs when a new owned account is created.",
                        trigger_type=AutomationTriggerType.EVENT,
                        source_domain="crm",
                        event_type="crm.account.created",
                        default_filter_config={"schema_key": "crm.account.created.v1"},
                    ),
                    TriggerCatalogDefinition(
                        trigger_key="crm.account.deleted",
                        display_name="Account deleted",
                        description="Runs when an owned account is deleted.",
                        trigger_type=AutomationTriggerType.EVENT,
                        source_domain="crm",
                        event_type="crm.account.deleted",
                        default_filter_config={"schema_key": "crm.account.deleted.v1"},
                    ),
                ],
            ),
        ],
        option_sets=AutomationTriggerOptionSets(pipelines=pipelines),
    )
