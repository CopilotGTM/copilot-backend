"""Tests for Knowledge Base Access API handler.

Focuses on:
- List sources endpoint with filters and pagination
- Download URL generation endpoint
- Source statistics endpoint
- Batch delete endpoint
"""

from uuid import uuid4

# =============================================================================
# GET /users/knowledge/sources - List Sources Tests
# =============================================================================


def test_list_sources_empty(knowledge_access_client):
    """Test listing sources with no sources returns empty list."""
    client = knowledge_access_client["client"]

    resp = client.get("/users/knowledge/sources")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["items"] == []
    assert body["data"]["cursor"] is None


def test_list_sources_success(knowledge_access_client):
    """Test listing sources returns sources correctly."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    # Add some sources
    service.add_source(filename="document1.pdf", status="completed")
    service.add_source(filename="document2.pdf", status="processing")

    resp = client.get("/users/knowledge/sources")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["items"]) == 2

    filenames = [item["filename"] for item in body["data"]["items"]]
    assert "document1.pdf" in filenames
    assert "document2.pdf" in filenames


def test_list_sources_pagination(knowledge_access_client):
    """Test listing sources with limit parameter."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    # Add multiple sources
    for i in range(5):
        service.add_source(filename=f"doc{i}.pdf", status="completed")

    resp = client.get("/users/knowledge/sources", params={"limit": 3})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["items"]) == 3


def test_list_sources_filter_status(knowledge_access_client):
    """Test listing sources with status filter."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    service.add_source(filename="completed.pdf", status="completed")
    service.add_source(filename="processing.pdf", status="processing")
    service.add_source(filename="failed.pdf", status="failed")

    resp = client.get("/users/knowledge/sources", params={"statuses": ["completed"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["filename"] == "completed.pdf"


def test_list_sources_filter_type(knowledge_access_client):
    """Test listing sources with source type filter."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    service.add_source(filename="file.pdf", source_type="file", status="completed")
    service.add_source(filename="integration.pdf", source_type="integration", status="completed")

    resp = client.get("/users/knowledge/sources", params={"source_types": ["file"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["filename"] == "file.pdf"


def test_list_sources_search(knowledge_access_client):
    """Test listing sources with search query."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    service.add_source(filename="quarterly_report.pdf", status="completed")
    service.add_source(filename="annual_summary.pdf", status="completed")
    service.add_source(filename="report_2024.pdf", status="completed")

    resp = client.get("/users/knowledge/sources", params={"q": "report"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["items"]) == 2


def test_list_sources_limit_min_boundary(knowledge_access_client):
    """Test listing sources with limit=1 (minimum)."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    service.add_source(filename="doc1.pdf", status="completed")
    service.add_source(filename="doc2.pdf", status="completed")

    resp = client.get("/users/knowledge/sources", params={"limit": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["items"]) == 1


def test_list_sources_limit_below_min(knowledge_access_client):
    """Test listing sources with limit=0 (below minimum) returns 422."""
    client = knowledge_access_client["client"]

    resp = client.get("/users/knowledge/sources", params={"limit": 0})
    assert resp.status_code == 422, resp.text


def test_list_sources_limit_above_max(knowledge_access_client):
    """Test listing sources with limit=101 (above maximum) returns 422."""
    client = knowledge_access_client["client"]

    resp = client.get("/users/knowledge/sources", params={"limit": 101})
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /users/knowledge/source/{source_id}/download - Download URL Tests
# =============================================================================


def test_download_source_success(knowledge_access_client):
    """Test successful download URL generation."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="download_me.pdf", status="completed")

    resp = client.get(f"/users/knowledge/source/{source.source_id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["source_id"] == str(source.source_id)
    assert "https://" in body["data"]["download_url"]
    assert body["data"]["expires_in_seconds"] == 30 * 60  # Default 30 minutes


def test_download_source_custom_expires(knowledge_access_client):
    """Test download URL with custom expiry time."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="custom_expires.pdf", status="completed")

    resp = client.get(
        f"/users/knowledge/source/{source.source_id}/download",
        params={"expires_minutes": 15},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["expires_in_seconds"] == 15 * 60


def test_download_source_not_found(knowledge_access_client):
    """Test download URL for non-existent source returns error."""
    client = knowledge_access_client["client"]
    non_existent_id = uuid4()

    resp = client.get(f"/users/knowledge/source/{non_existent_id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_download_source_expires_min_boundary(knowledge_access_client):
    """Test download URL with expires_minutes=1 (minimum) works."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="min_expires.pdf", status="completed")

    resp = client.get(
        f"/users/knowledge/source/{source.source_id}/download",
        params={"expires_minutes": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["expires_in_seconds"] == 60


def test_download_source_expires_max_boundary(knowledge_access_client):
    """Test download URL with expires_minutes=30 (maximum) works."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="max_expires.pdf", status="completed")

    resp = client.get(
        f"/users/knowledge/source/{source.source_id}/download",
        params={"expires_minutes": 30},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["expires_in_seconds"] == 30 * 60


def test_download_source_expires_below_min(knowledge_access_client):
    """Test download URL with expires_minutes=0 (below minimum) returns 422."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="below_min.pdf", status="completed")

    resp = client.get(
        f"/users/knowledge/source/{source.source_id}/download",
        params={"expires_minutes": 0},
    )
    assert resp.status_code == 422, resp.text


def test_download_source_expires_above_max(knowledge_access_client):
    """Test download URL with expires_minutes=31 (above maximum) returns 422."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="above_max.pdf", status="completed")

    resp = client.get(
        f"/users/knowledge/source/{source.source_id}/download",
        params={"expires_minutes": 31},
    )
    assert resp.status_code == 422, resp.text


def test_download_source_invalid_uuid(knowledge_access_client):
    """Test download URL with invalid UUID format returns 422."""
    client = knowledge_access_client["client"]

    resp = client.get("/users/knowledge/source/not-a-uuid/download")
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /users/knowledge/sources/stats - Statistics Tests
# =============================================================================


def test_stats_success(knowledge_access_client):
    """Test successful statistics retrieval."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    service.add_source(filename="completed1.pdf", status="completed", source_type="file")
    service.add_source(filename="completed2.pdf", status="completed", source_type="file")
    service.add_source(filename="processing.pdf", status="processing", source_type="integration")
    service.add_source(filename="failed.pdf", status="failed", source_type="file")

    resp = client.get("/users/knowledge/sources/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["total"] == 4
    assert body["data"]["by_status"]["completed"] == 2
    assert body["data"]["by_status"]["processing"] == 1
    assert body["data"]["by_status"]["failed"] == 1
    assert body["data"]["by_type"]["file"] == 3
    assert body["data"]["by_type"]["integration"] == 1
    assert body["data"]["active_count"] == 1  # Only processing is active
    assert body["data"]["recent_failures_24h"] == 1


def test_stats_empty(knowledge_access_client):
    """Test statistics for empty org returns zeros."""
    client = knowledge_access_client["client"]

    resp = client.get("/users/knowledge/sources/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["total"] == 0
    assert body["data"]["by_status"] == {}
    assert body["data"]["by_type"] == {}
    assert body["data"]["active_count"] == 0


# =============================================================================
# DELETE /users/knowledge/sources - Batch Delete Tests
# =============================================================================


def test_batch_delete_success(knowledge_access_client):
    """Test successful batch deletion of sources."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source1 = service.add_source(filename="delete1.pdf", status="completed")
    source2 = service.add_source(filename="delete2.pdf", status="completed")

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": [str(source1.source_id), str(source2.source_id)]},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["results"]) == 2
    assert body["data"]["errors"] == []


def test_batch_delete_single_source(knowledge_access_client):
    """Test deleting a single source."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="delete_single.pdf", status="completed")

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": [str(source.source_id)]},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["results"]) == 1
    assert body["data"]["results"][0]["status"] == "deleting"


def test_batch_delete_not_found(knowledge_access_client):
    """Test deleting non-existent source returns error.

    Note: Endpoint always returns 202 Accepted, error status in body.
    """
    client = knowledge_access_client["client"]
    non_existent_id = uuid4()

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": [str(non_existent_id)]},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "failed" in body["message"].lower()


def test_batch_delete_partial_failure(knowledge_access_client):
    """Test batch delete with some existing and some non-existent sources."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="existing.pdf", status="completed")
    non_existent_id = uuid4()

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": [str(source.source_id), str(non_existent_id)]},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["results"]) == 1
    assert len(body["data"]["errors"]) == 1


def test_batch_delete_already_deleted(knowledge_access_client):
    """Test deleting already deleted source returns already_deleted status."""
    client = knowledge_access_client["client"]
    service = knowledge_access_client["service"]

    source = service.add_source(filename="already_deleted.pdf", status="completed")
    source.is_deleted = True

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": [str(source.source_id)]},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    # already_deleted is treated as a successful result, not an error
    assert len(body["data"]["results"]) == 1
    assert body["data"]["results"][0]["status"] == "already_deleted"


def test_batch_delete_empty_list(knowledge_access_client):
    """Test batch delete with empty list returns 422."""
    client = knowledge_access_client["client"]

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": []},
    )
    assert resp.status_code == 422, resp.text


def test_batch_delete_invalid_uuid(knowledge_access_client):
    """Test batch delete with invalid UUID format returns 422."""
    client = knowledge_access_client["client"]

    resp = client.request(
        "DELETE",
        "/users/knowledge/sources",
        json={"source_ids": ["not-a-uuid"]},
    )
    assert resp.status_code == 422, resp.text
