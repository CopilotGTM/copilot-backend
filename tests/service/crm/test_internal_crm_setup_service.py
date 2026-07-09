from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.crm.internal.internal_crm_setup_service import (
    DEFAULT_STAGES,
    InternalCRMSetupService,
)


class _NoRowsResult:
    def scalars(self) -> _NoRowsResult:
        return self

    def first(self) -> None:
        return None


class _RowsResult:
    def __init__(
        self, *, first: object | None = None, all_rows: list[object] | None = None
    ) -> None:
        self._first = first
        self._all_rows = all_rows or []

    def scalars(self) -> _RowsResult:
        return self

    def first(self) -> object | None:
        return self._first

    def all(self) -> list[object]:
        return self._all_rows


class _SetupSession:
    def __init__(self, results: list[object] | None = None) -> None:
        self.added: list[object] = []
        self.flushed = 0
        self._results = results or []

    async def execute(self, _stmt: object) -> object:
        if self._results:
            return self._results.pop(0)
        return _NoRowsResult()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def refresh(self, _obj: object) -> None:
        return None


@pytest.mark.asyncio
async def test_setup_default_pipeline_normalizes_existing_usability_metadata() -> None:
    pipeline = type(
        "Pipeline",
        (),
        {
            "pipeline_id": uuid4(),
            "is_active": False,
            "properties": {"source": "internal_crm"},
        },
    )()
    stages = [
        type(
            "Stage",
            (),
            {
                "stage_key": stage_def["stage_key"],
                "name": stage_def["name"],
                "stage_state": stage_def["stage_state"],
            },
        )()
        for stage_def in DEFAULT_STAGES
    ]
    session = _SetupSession(
        results=[
            _RowsResult(first=pipeline),
            _RowsResult(all_rows=stages),
        ]
    )
    service = InternalCRMSetupService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    returned_pipeline, returned_stages = await service._ensure_default_pipeline(uuid4())  # noqa: SLF001

    assert returned_pipeline is pipeline
    assert returned_stages == stages
    assert pipeline.is_active is True
    assert pipeline.properties["usable_for_internal_deals"] is True
