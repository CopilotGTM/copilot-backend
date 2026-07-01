"""Tests for Tasks API handler.

Focuses on:
- List endpoint with filtering (status, priority, task_type)
- Pagination with cursor-based navigation
- Task detail retrieval
- is_overdue flag computation
- Invalid input validation
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.api.presenters.crm.task_presenter import present_task_detail
from src.api.schemas.crm_task_models import CreateTaskRequest

from tests.api.routers.crm.conftest import create_task

# =============================================================================
# GET /tasks - Global Task List Scope Tests
# =============================================================================


def test_global_tasks_defaults_to_assigned_to_me(client: TestClient, tasks_dependency_overrides):
    service = tasks_dependency_overrides["service"]

    resp = client.get("/users/crm/tasks")

    assert resp.status_code == 200, resp.text
    assert service.list_tasks_calls
    call = service.list_tasks_calls[-1]
    assert call["scope"] == "assigned_to_me"
    assert call["current_user_id"] == tasks_dependency_overrides["user_ids"]["user_id"]
    assert call["current_user_email"] == "test@example.com"
    assert call["assigned_to_key"] is None


def test_global_tasks_rejects_user_scope_without_selected_assignee(
    client: TestClient, tasks_dependency_overrides
):
    resp = client.get("/users/crm/tasks", params={"scope": "assigned_to_user"})

    assert resp.status_code == 400, resp.text
    assert "assigned_to_key is required" in resp.json()["message"]


def test_global_tasks_all_scope_is_not_available(client: TestClient, tasks_dependency_overrides):
    resp = client.get("/users/crm/tasks", params={"scope": "all"})

    assert resp.status_code == 422, resp.text


def test_current_user_task_assignees(client: TestClient, tasks_dependency_overrides):
    resp = client.get("/users/crm/tasks/assignees/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assignees = body["data"]["assignees"]
    assert assignees[0]["assigned_to"]["filter_key"] == "email:test@example.com"
    assert assignees[0]["task_count"] == 3


def test_task_options_use_db_backed_statuses(client: TestClient, tasks_dependency_overrides):
    resp = client.get("/users/crm/tasks/options")

    assert resp.status_code == 200, resp.text
    values = {item["value"] for item in resp.json()["data"]["statuses"]}
    assert values == {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}


def test_create_task_request_rejects_status_outside_crm_task_constraint():
    try:
        CreateTaskRequest(subject="Follow up", status="WAITING")
    except ValidationError as exc:
        assert "status must be one of" in str(exc)
    else:
        raise AssertionError("WAITING must be rejected before TaskWriteService is called")


# =============================================================================
# GET /deals/{deal_id}/tasks - List Tasks Tests
# =============================================================================


def test_list_tasks_empty(client: TestClient, tasks_dependency_overrides):
    """Test listing tasks for deal with no tasks."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["items"] == []
    assert body["data"]["total_count"] == 0


def test_list_tasks_success(client: TestClient, tasks_dependency_overrides):
    """Test listing tasks returns items correctly."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add tasks
    for i in range(3):
        task = create_task(deal_id, org_id, subject=f"Task {i}")
        service.add_task(task)

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 3


def test_list_tasks_returns_clickable_deal_object(client: TestClient, tasks_dependency_overrides):
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    task = create_task(deal_id, org_id, subject="Follow up", related_name="Acme Enterprise")
    service.add_task(task)

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks")

    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["deal"] == {
        "deal_id": str(deal_id),
        "name": "Acme Enterprise",
    }
    assert item["deal_id"] == str(deal_id)
    assert item["deal_name"] == "Acme Enterprise"


def test_list_tasks_pagination_limit_1(client: TestClient, tasks_dependency_overrides):
    """Test pagination with limit=1 returns single item."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    for i in range(5):
        task = create_task(deal_id, org_id, subject=f"Task {i}")
        service.add_task(task)

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 1
    assert body["data"]["total_count"] == 5
    assert body["data"]["total_pages"] == 5


def test_list_tasks_pagination_limit_max(client: TestClient, tasks_dependency_overrides):
    """Test limit=100 (max) works correctly."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    task = create_task(deal_id, org_id)
    service.add_task(task)

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": 100})
    assert resp.status_code == 200, resp.text


def test_list_tasks_limit_exceeds_max(client: TestClient, tasks_dependency_overrides):
    """Test limit > 100 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": 101})
    assert resp.status_code == 422, resp.text


def test_list_tasks_limit_zero(client: TestClient, tasks_dependency_overrides):
    """Test limit=0 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": 0})
    assert resp.status_code == 422, resp.text


def test_list_tasks_limit_negative(client: TestClient, tasks_dependency_overrides):
    """Test limit=-1 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": -1})
    assert resp.status_code == 422, resp.text


def test_list_tasks_filter_by_status(client: TestClient, tasks_dependency_overrides):
    """Test filtering by single status."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_task(create_task(deal_id, org_id, status="NOT_STARTED"))
    service.add_task(create_task(deal_id, org_id, status="IN_PROGRESS"))
    service.add_task(create_task(deal_id, org_id, status="COMPLETED"))

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"statuses": ["IN_PROGRESS"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["status"] == "IN_PROGRESS"


def test_list_tasks_filter_by_multiple_statuses(client: TestClient, tasks_dependency_overrides):
    """Test filtering by multiple statuses."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_task(create_task(deal_id, org_id, status="NOT_STARTED"))
    service.add_task(create_task(deal_id, org_id, status="IN_PROGRESS"))
    service.add_task(create_task(deal_id, org_id, status="COMPLETED"))

    resp = client.get(
        f"/users/crm/deals/{deal_id}/tasks",
        params={"statuses": ["NOT_STARTED", "IN_PROGRESS"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 2


def test_list_tasks_filter_by_priority(client: TestClient, tasks_dependency_overrides):
    """Test filtering by priority."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_task(create_task(deal_id, org_id, priority="LOW"))
    service.add_task(create_task(deal_id, org_id, priority="HIGH"))
    service.add_task(create_task(deal_id, org_id, priority="URGENT"))

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"priorities": ["HIGH"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["priority"] == "HIGH"


def test_list_tasks_filter_by_task_type(client: TestClient, tasks_dependency_overrides):
    """Test filtering by TASK type."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_task(create_task(deal_id, org_id, activity_type="TASK"))
    service.add_task(create_task(deal_id, org_id, activity_type="TODO"))

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"task_types": ["TASK"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["task_type"] == "TASK"


def test_list_tasks_filter_non_task_types_returns_empty(
    client: TestClient, tasks_dependency_overrides
):
    """Test filtering by non-TASK/TODO types returns empty list (not error)."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_task(create_task(deal_id, org_id, activity_type="TASK"))

    # Request EMAIL type - should return empty, not error
    resp = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"task_types": ["EMAIL"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 0


def test_list_tasks_page_pagination(client: TestClient, tasks_dependency_overrides):
    """Test multi-page pagination works."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add 5 tasks
    for i in range(5):
        task = create_task(deal_id, org_id, subject=f"Task {i}")
        service.add_task(task)

    # Get first page (limit 2)
    resp1 = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": 2})
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert len(body1["data"]["items"]) == 2
    assert body1["data"]["total_count"] == 5
    assert body1["data"]["total_pages"] == 3

    # Get second page
    resp2 = client.get(f"/users/crm/deals/{deal_id}/tasks", params={"limit": 2, "page": 2})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["data"]["items"]) == 2

    # Ensure different items
    page1_ids = {item["task_id"] for item in body1["data"]["items"]}
    page2_ids = {item["task_id"] for item in body2["data"]["items"]}
    assert page1_ids.isdisjoint(page2_ids), "Pages should contain different items"


def test_list_tasks_is_overdue_computed(client: TestClient, tasks_dependency_overrides):
    """Test is_overdue flag is computed correctly."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Overdue task (due date in the past, not completed)
    overdue_task = create_task(
        deal_id,
        org_id,
        subject="Overdue Task",
        status="NOT_STARTED",
        due_date=datetime.now(UTC) - timedelta(days=1),
    )
    service.add_task(overdue_task)

    # Not overdue (due date in the future)
    future_task = create_task(
        deal_id,
        org_id,
        subject="Future Task",
        status="NOT_STARTED",
        due_date=datetime.now(UTC) + timedelta(days=1),
    )
    service.add_task(future_task)

    # Not overdue (completed, even if due date in past)
    completed_task = create_task(
        deal_id,
        org_id,
        subject="Completed Task",
        status="COMPLETED",
        due_date=datetime.now(UTC) - timedelta(days=1),
    )
    service.add_task(completed_task)

    resp = client.get(f"/users/crm/deals/{deal_id}/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    items = {item["subject"]: item for item in body["data"]["items"]}
    assert items["Overdue Task"]["is_overdue"] is True
    assert items["Future Task"]["is_overdue"] is False
    assert items["Completed Task"]["is_overdue"] is False


def test_list_tasks_multiple_deals_isolation(client: TestClient, tasks_dependency_overrides):
    """Test tasks are isolated per deal."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_1 = uuid4()
    deal_2 = uuid4()

    # Add tasks to deal_1
    for i in range(3):
        service.add_task(create_task(deal_1, org_id, subject=f"Deal1 Task {i}"))

    # Add task to deal_2
    service.add_task(create_task(deal_2, org_id, subject="Deal2 Task"))

    resp1 = client.get(f"/users/crm/deals/{deal_1}/tasks")
    assert len(resp1.json()["data"]["items"]) == 3

    resp2 = client.get(f"/users/crm/deals/{deal_2}/tasks")
    assert len(resp2.json()["data"]["items"]) == 1


# =============================================================================
# GET /tasks/{task_id} - Get Task Detail Tests
# =============================================================================


def test_get_task_success(client: TestClient, tasks_dependency_overrides):
    """Test getting a task detail."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    task = create_task(deal_id, org_id, subject="My Task")
    service.add_task(task)

    resp = client.get(f"/users/crm/tasks/{task.activity_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["subject"] == "My Task"
    assert body["data"]["task_id"] == str(task.activity_id)


def test_task_response_maps_crm_task_source_fields(client: TestClient, tasks_dependency_overrides):
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    task = create_task(
        deal_id,
        org_id,
        subject="Source table title",
        body="Source table description",
        due_date=datetime.now(UTC) + timedelta(days=1),
        properties={
            "source": "MANUAL",
            "assigned_to_public_user_id": str(tasks_dependency_overrides["user_ids"]["user_id"]),
        },
    )
    service.add_task(task)

    resp = client.get(f"/users/crm/tasks/{task.task_id}")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["task_id"] == str(task.task_id)
    assert data["subject"] == "Source table title"
    assert data["body"] == "Source table description"
    assert data["source"] == "MANUAL"


@pytest.mark.asyncio
async def test_task_response_reads_all_day_due_metadata():
    org_id = uuid4()
    deal_id = uuid4()
    task = create_task(
        deal_id,
        org_id,
        subject="All day task",
        due_date=datetime(2026, 3, 24, tzinfo=UTC),
        due_at=None,
        is_all_day=True,
        properties={
            "source": "MANUAL",
            "due_timezone": "Asia/Kolkata",
            "due_local_date": "2026-03-24",
        },
    )
    detail = present_task_detail(task)

    data = detail.model_dump(mode="json")
    assert data["due_date"] == "2026-03-24T00:00:00Z"
    assert data["due_local_date"] == "2026-03-24"
    assert data["due_timezone"] == "Asia/Kolkata"
    assert data["is_all_day"] is True


def test_get_task_not_found(client: TestClient, tasks_dependency_overrides):
    """Test getting non-existent task returns 404."""
    non_existent_id = uuid4()

    resp = client.get(f"/users/crm/tasks/{non_existent_id}")
    assert resp.status_code == 404, resp.text


def test_get_task_includes_all_fields(client: TestClient, tasks_dependency_overrides):
    """Test task detail response includes all expected fields."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    task = create_task(
        deal_id,
        org_id,
        subject="Complete Task",
        body="Task description",
        status="IN_PROGRESS",
        priority="HIGH",
        owner_name="John Doe",
        owner_email="john@example.com",
    )
    service.add_task(task)

    resp = client.get(f"/users/crm/tasks/{task.activity_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    # Check key fields exist
    assert "task_id" in data
    assert "external_id" not in data
    assert "subject" in data
    assert "body" in data
    assert "status" in data
    assert "priority" in data
    assert "owner_name" in data
    assert "owner_email" in data
    assert "created_at" in data
    assert "updated_at" in data
    assert "is_overdue" in data


def test_get_task_is_overdue_computed(client: TestClient, tasks_dependency_overrides):
    """Test is_overdue flag is computed correctly on detail."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    overdue_task = create_task(
        deal_id,
        org_id,
        status="NOT_STARTED",
        due_date=datetime.now(UTC) - timedelta(days=1),
    )
    service.add_task(overdue_task)

    resp = client.get(f"/users/crm/tasks/{overdue_task.activity_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_overdue"] is True


def test_get_task_deleted_not_returned(client: TestClient, tasks_dependency_overrides):
    """Test deleted tasks are not returned."""
    service = tasks_dependency_overrides["service"]
    org_id = tasks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    deleted_task = create_task(deal_id, org_id, is_deleted=True)
    service.add_task(deleted_task)

    resp = client.get(f"/users/crm/tasks/{deleted_task.activity_id}")
    assert resp.status_code == 404, resp.text
