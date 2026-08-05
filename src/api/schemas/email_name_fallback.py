"""Response-only fallback for missing person names.

Person-facing GET DTOs inherit this model so callers receive a useful display
name without replacing the authoritative null stored in the database.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator
from src.service.crm.meetings.attendee_display import derive_name_from_email
from src.service.crm.utils.person import split_full_name


class EmailNameFallbackModel(BaseModel):
    """Derive display-only names when a person response has email but no name.

    Validation mutates only the Pydantic response object. ORM records and write
    request models never inherit this class, so inferred names are not persisted.
    """

    @model_validator(mode="after")
    def derive_missing_name_from_email(self) -> EmailNameFallbackModel:
        """Populate blank response name fields while preserving authoritative names."""
        fields = type(self).model_fields

        for email_field in fields:
            name_fields = self._name_fields_for_email(email_field, fields)
            if not name_fields or any(
                self._has_text(getattr(self, field)) for field in name_fields
            ):
                continue

            derived_name = derive_name_from_email(getattr(self, email_field))
            if not derived_name:
                continue

            first_name, last_name = split_full_name(derived_name)
            for name_field in name_fields:
                value = derived_name
                if name_field == "first_name":
                    value = first_name
                elif name_field == "last_name":
                    value = last_name
                setattr(self, name_field, value)

        return self

    @staticmethod
    def _name_fields_for_email(email_field: str, fields: dict[str, object]) -> tuple[str, ...]:
        # copilotgtm: Person DTOs use email/name or matching <prefix>_email/
        # <prefix>_name fields. Add an explicit mapping if a future DTO breaks this convention.
        if email_field == "email":
            return tuple(
                field
                for field in ("name", "full_name", "first_name", "last_name")
                if field in fields
            )
        if email_field.endswith("_email"):
            name_field = f"{email_field.removesuffix('_email')}_name"
            return (name_field,) if name_field in fields else ()
        return ()

    @staticmethod
    def _has_text(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())
