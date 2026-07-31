"""Validate schema names used by organisation migrations and cutover tooling."""

from __future__ import annotations

import re
from uuid import UUID

from src.infra.utils.resource_naming import get_schema_name

SCHEMA_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_migration_schema_name(schema_name: str) -> str:
    """Accept one unqualified lowercase PostgreSQL schema identifier."""

    if not SCHEMA_NAME_PATTERN.fullmatch(schema_name):
        raise ValueError(
            "schema must be a lowercase PostgreSQL identifier of at most 63 characters"
        )
    return schema_name


def legacy_schema_organisation_id(schema_name: str) -> UUID:
    """Return the UUID encoded by an exact canonical source-schema name."""

    if not schema_name.startswith("org_"):
        raise ValueError("legacy organisation schema is not canonical")
    try:
        organisation_id = UUID(schema_name.removeprefix("org_").replace("_", "-"))
    except ValueError as exc:
        raise ValueError("legacy organisation schema is not canonical") from exc
    if get_schema_name(organisation_id) != schema_name:
        raise ValueError("legacy organisation schema is not canonical")
    return organisation_id
