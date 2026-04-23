"""Shared helpers for serializing normalized schema options."""

from __future__ import annotations

from typing import Any

from src.service.integrations.crm.schema.common.types import NormalizedSchemaOption


def serialize_options_payload(
    options: list[NormalizedSchemaOption],
    *,
    source: str,
) -> dict[str, Any]:
    """Shape normalized options into the JSON payload stored on custom fields."""

    return {
        "source": source,
        "values": [
            {
                "value": opt.value,
                "label": opt.label,
                "description": opt.description,
                "display_order": opt.display_order,
                "hidden": opt.hidden,
            }
            for opt in options
        ],
    }
