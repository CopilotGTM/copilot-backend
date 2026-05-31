from __future__ import annotations

from typing import Protocol


class SalesforcePubSubSchemaClient(Protocol):
    async def get_schema(self, schema_id: str, *, metadata: tuple[tuple[str, str], ...]) -> str: ...


class SalesforcePubSubSchemaCache:
    def __init__(self, schema_client: SalesforcePubSubSchemaClient | None = None) -> None:
        self._schema_client = schema_client
        self._schemas: dict[str, str] = {}

    async def get_schema(self, schema_id: str, *, metadata: tuple[tuple[str, str], ...]) -> str:
        cached = self._schemas.get(schema_id)
        if cached is not None:
            return cached
        if self._schema_client is None:
            raise RuntimeError("Salesforce Pub/Sub schema client is not configured")
        schema = await self._schema_client.get_schema(schema_id, metadata=metadata)
        self._schemas[schema_id] = schema
        return schema
