"""Organization CRM schema integration for automation trigger paths."""

from __future__ import annotations

from uuid import uuid4

from src.domain.models.organisation_tables import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
)
from src.service.automation.triggers.catalog import build_trigger_catalog
from src.service.automation.triggers.crm_schema import merge_deal_custom_fields


def custom_field(
    key: str,
    data_type: CRMCustomFieldDataType,
    *,
    options: dict | None = None,
    formula: dict | None = None,
) -> CRMCustomFieldDefinition:
    """Build the custom field fixture used by the surrounding regression cases."""
    return CRMCustomFieldDefinition(
        organisation_id=uuid4(),
        key=key,
        label=key.replace("_", " ").title(),
        data_type=data_type,
        options=options or {},
        formula=formula,
    )


def test_custom_deal_fields_extend_catalog_with_typed_paths_and_options() -> None:
    """Verify the contract that custom deal fields extend catalog with typed paths and options."""
    region = custom_field(
        "region",
        CRMCustomFieldDataType.SELECT,
        options={
            "values": [
                {"value": "apac", "label": "APAC"},
                {"value": "retired", "active": False},
            ]
        },
    )
    score = custom_field("fit_score", CRMCustomFieldDataType.NUMBER)

    snapshot = merge_deal_custom_fields([region, score])
    catalog = build_trigger_catalog(
        [],
        registry=snapshot.registry,
        custom_option_sets=snapshot.option_sets,
    )
    definitions = {
        trigger.trigger_key: trigger
        for category in catalog.categories
        for resource in category.resources
        for trigger in resource.triggers
    }
    created = definitions["crm.deal.created"]
    fields = {field.path: field for field in created.fields}
    region_source = f"custom_field:{region.field_id}"

    assert fields["record.custom_properties.region"].value_type == "enum"
    assert fields["record.custom_properties.region"].option_source == region_source
    assert fields["record.custom_properties.fit_score"].value_type == "decimal"
    assert [(item.value, item.label) for item in catalog.option_sets.enums[region_source]] == [
        ("apac", "APAC")
    ]


def test_untyped_formula_is_not_filterable_but_declared_formula_is_typed() -> None:
    """Verify the contract that untyped formula is not filterable but declared formula is typed."""
    untyped = custom_field("mystery", CRMCustomFieldDataType.FORMULA)
    typed = custom_field(
        "computed_score",
        CRMCustomFieldDataType.FORMULA,
        formula={"result_type": "number"},
    )

    snapshot = merge_deal_custom_fields([untyped, typed])
    paths = snapshot.registry.event("crm.deal.updated").fields

    assert "record.custom_properties.mystery" not in paths
    assert paths["record.custom_properties.computed_score"].value_type.value == "decimal"
