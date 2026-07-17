"""Regression coverage for the GSTIN deal custom-field maintenance script."""

from types import SimpleNamespace

import pytest
from scripts.maintenance.ensure_deal_gstin_custom_field import ensure_gstin_field


@pytest.mark.asyncio
async def test_ensure_gstin_field_is_idempotent_and_dry_run_safe() -> None:
    """An existing compatible field is reused and dry-run never creates one."""

    class Service:
        """Minimal custom-field service double recording attempted writes."""

        def __init__(self, fields):
            self.fields = fields
            self.created = []

        async def list_fields(self, **_kwargs):
            """Return the configured field definitions."""
            return self.fields

        async def create_field(self, request):
            """Record a creation request without touching a database."""
            self.created.append(request)
            return SimpleNamespace(key=request.key)

    existing = SimpleNamespace(
        key="gstin_tax_id",
        label="GSTIN / Tax ID",
        data_type="text",
        is_active=True,
    )
    existing_service = Service([existing])
    assert await ensure_gstin_field(existing_service, execute=True) == "exists"
    assert existing_service.created == []

    empty_service = Service([])
    assert await ensure_gstin_field(empty_service, execute=False) == "would_create"
    assert empty_service.created == []
