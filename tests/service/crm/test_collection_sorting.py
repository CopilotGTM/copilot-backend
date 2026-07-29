"""Query-contract checks for globally paginated CRM collection sorting."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.crm.accounts.read_service import AccountReadService
from src.service.crm.contacts.read_service import ContactReadService
from src.service.crm.deals.read_service import DealReadService
from src.service.crm.tasks.contracts import TaskReadServiceError
from src.service.crm.tasks.read_service import TaskReadService


class _EmptyResult:
    def scalar(self) -> int:
        return 0

    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list:
        return []


class _FakeSession:
    def __init__(self) -> None:
        self.executed_statements: list[object] = []
        self.scalar_statements: list[object] = []

    async def execute(self, statement) -> _EmptyResult:
        self.executed_statements.append(statement)
        return _EmptyResult()

    async def scalar(self, statement) -> int:
        self.scalar_statements.append(statement)
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sql_fragment"),
    [
        ("name", "crm_deals.name"),
        ("stage", "crm_deals.stage_label"),
        ("amount", "crm_deals.amount"),
        ("expected_close_date", "crm_deals.expected_close_date"),
        ("owner", "crm_deals.owner_name"),
        ("last_activity_at", "crm_deals.last_activity_at"),
    ],
)
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
async def test_deal_sorting_is_applied_before_pagination(
    sort_by: str,
    sql_fragment: str,
    sort_order: str,
) -> None:
    session = _FakeSession()

    await DealReadService(session, uuid4()).list_deals(  # type: ignore[arg-type]
        sort_by=sort_by,
        sort_order=sort_order,
        page=3,
    )

    sql = str(session.executed_statements[-1]).lower()
    assert sql_fragment in sql
    assert f"{sort_order} nulls last" in sql
    assert "crm_deals.deal_id asc" in sql
    assert sql.index("order by") < sql.index("limit")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sql_fragment"),
    [
        ("name", "crm_companies.name"),
        ("lifecycle_stage", "crm_companies.lifecycle_stage"),
        ("owner", "crm_companies.owner_name"),
        ("annual_revenue", "crm_companies.annual_revenue"),
        ("number_of_employees", "crm_companies.number_of_employees"),
        ("created_at", "crm_companies.created_at"),
    ],
)
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
async def test_account_sorting_is_applied_before_pagination(
    sort_by: str,
    sql_fragment: str,
    sort_order: str,
) -> None:
    session = _FakeSession()

    await AccountReadService(session, uuid4()).list_accounts(  # type: ignore[arg-type]
        sort_by=sort_by,
        sort_order=sort_order,
        page=3,
    )

    sql = str(session.executed_statements[-1]).lower()
    assert sql_fragment in sql
    assert f"{sort_order} nulls last" in sql
    assert "crm_companies.company_id asc" in sql
    assert sql.index("order by") < sql.index("limit")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sql_fragment"),
    [
        ("full_name", "crm_contacts.full_name"),
        ("owner", "crm_contacts.owner_name"),
        ("last_contacted_at", "crm_contacts.last_contacted_at"),
        ("last_activity_at", "crm_contacts.last_activity_at"),
    ],
)
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
async def test_contact_sorting_is_applied_before_pagination(
    sort_by: str,
    sql_fragment: str,
    sort_order: str,
) -> None:
    session = _FakeSession()

    await ContactReadService(session, uuid4()).list_contacts(  # type: ignore[arg-type]
        sort_by=sort_by,
        sort_order=sort_order,
        page=3,
    )

    sql = str(session.executed_statements[-1]).lower()
    assert sql_fragment in sql
    assert f"{sort_order} nulls last" in sql
    assert "crm_contacts.contact_id asc" in sql
    assert sql.index("order by") < sql.index("limit")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sql_fragment"),
    [
        ("subject", "lower(crm_tasks.title)"),
        ("due_date", "coalesce(crm_tasks.due_at"),
        ("priority", "crm_tasks.priority"),
        ("status", "crm_tasks.status"),
        ("assignee", "crm_tasks.assigned_to_name"),
        ("created_at", "crm_tasks.created_at"),
    ],
)
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
async def test_task_sorting_is_applied_after_search_and_before_pagination(
    sort_by: str,
    sql_fragment: str,
    sort_order: str,
) -> None:
    session = _FakeSession()

    await TaskReadService(session, uuid4()).list_tasks(  # type: ignore[arg-type]
        scope="assigned_to_me",
        current_user_id=uuid4(),
        q="Exotel",
        sort_by=sort_by,
        sort_order=sort_order,
        page=3,
    )

    sql = str(session.executed_statements[-1]).lower()
    assert "crm_tasks.title" in sql
    assert "crm_deals.name" in sql
    assert "crm_tasks.assigned_to_name" in sql
    assert sql_fragment in sql
    assert f"{sort_order} nulls last" in sql
    assert "crm_tasks.task_id asc" in sql
    assert sql.index("order by") < sql.index("limit")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("q", "message"),
    [
        ("x", "at least 2 characters"),
        ("x" * 101, "at most 100 characters"),
    ],
)
async def test_task_search_rejects_invalid_length_before_querying(
    q: str,
    message: str,
) -> None:
    session = _FakeSession()

    with pytest.raises(TaskReadServiceError, match=message):
        await TaskReadService(session, uuid4()).list_tasks(  # type: ignore[arg-type]
            current_user_id=uuid4(),
            q=q,
        )

    assert session.executed_statements == []
    assert session.scalar_statements == []
