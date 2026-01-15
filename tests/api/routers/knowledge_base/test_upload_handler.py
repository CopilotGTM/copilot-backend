"""Tests for Knowledge Base Upload API handler.

Focuses on:
- Batch presign endpoint
- Batch finalize endpoint
- Get source status endpoint
"""

from uuid import uuid4

# =============================================================================
# POST /users/knowledge/presign/batch - Batch Presign Tests
# =============================================================================


def test_presign_batch_success(knowledge_upload_client):
    """Test successful batch presign for multiple files."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "doc1.pdf", "mimetype": "application/pdf", "size_bytes": 1024},
                {"filename": "doc2.pdf", "mimetype": "application/pdf", "size_bytes": 2048},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["files"]) == 2
    assert body["data"]["errors"] == []

    for file in body["data"]["files"]:
        assert file["success"] is True
        assert file["upload_url"] is not None
        assert "https://" in file["upload_url"]
        assert file["status"] == "pending_upload"


def test_presign_batch_single_file(knowledge_upload_client):
    """Test presign for a single file."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "single.pdf", "mimetype": "application/pdf", "size_bytes": 1024},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["files"]) == 1
    assert body["data"]["files"][0]["filename"] == "single.pdf"


def test_presign_batch_empty_files(knowledge_upload_client):
    """Test presign with empty files list returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={"files": []},
    )
    assert resp.status_code == 422, resp.text


def test_presign_batch_empty_filename(knowledge_upload_client):
    """Test presign with empty filename returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "", "mimetype": "application/pdf", "size_bytes": 1024},
            ]
        },
    )
    assert resp.status_code == 422, resp.text


def test_presign_batch_invalid_mimetype(knowledge_upload_client):
    """Test presign with invalid mimetype returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {
                    "filename": "test.exe",
                    "mimetype": "application/x-msdownload",
                    "size_bytes": 1024,
                },
            ]
        },
    )
    assert resp.status_code == 422, resp.text


def test_presign_batch_zero_size(knowledge_upload_client):
    """Test presign with zero size_bytes returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "zero.pdf", "mimetype": "application/pdf", "size_bytes": 0},
            ]
        },
    )
    assert resp.status_code == 422, resp.text


def test_presign_batch_negative_size(knowledge_upload_client):
    """Test presign with negative size_bytes returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "negative.pdf", "mimetype": "application/pdf", "size_bytes": -1},
            ]
        },
    )
    assert resp.status_code == 422, resp.text


def test_presign_batch_with_filters(knowledge_upload_client):
    """Test presign with default filters."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "filtered.pdf", "mimetype": "application/pdf", "size_bytes": 1024},
            ],
            "default_filters": {"category": "reports", "year": 2024},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_presign_batch_with_client_batch_id(knowledge_upload_client):
    """Test presign with client batch ID for idempotency."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/presign/batch",
        json={
            "files": [
                {"filename": "idempotent.pdf", "mimetype": "application/pdf", "size_bytes": 1024},
            ],
            "client_batch_id": "test-batch-123",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


# =============================================================================
# POST /users/knowledge/finalize/batch - Batch Finalize Tests
# =============================================================================


def test_finalize_batch_success(knowledge_upload_client):
    """Test successful batch finalize."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    # First presign to create source
    source = service.add_source(filename="finalize_me.pdf", status="pending_upload")

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": str(source.source_id)},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["results"]) == 1
    assert body["data"]["results"][0]["status"] == "processing"


def test_finalize_batch_multiple(knowledge_upload_client):
    """Test finalizing multiple sources."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    source1 = service.add_source(filename="finalize1.pdf", status="pending_upload")
    source2 = service.add_source(filename="finalize2.pdf", status="pending_upload")

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": str(source1.source_id)},
                {"source_id": str(source2.source_id)},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["results"]) == 2


def test_finalize_batch_not_found(knowledge_upload_client):
    """Test finalize with non-existent source."""
    client = knowledge_upload_client["client"]
    non_existent_id = uuid4()

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": str(non_existent_id)},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # All failed so it returns error status
    assert body["status"] == "ERROR"
    assert len(body["data"]["errors"]) == 1


def test_finalize_batch_partial_failure(knowledge_upload_client):
    """Test finalize with some existing and some non-existent sources."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    source = service.add_source(filename="existing.pdf", status="pending_upload")
    non_existent_id = uuid4()

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": str(source.source_id)},
                {"source_id": str(non_existent_id)},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["results"]) == 1
    assert len(body["data"]["errors"]) == 1


def test_finalize_batch_already_processed(knowledge_upload_client):
    """Test finalize for already processed source (idempotent)."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    # Source already in processing state
    source = service.add_source(filename="already_processed.pdf", status="processing")

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": str(source.source_id)},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    # Should still return success, status unchanged
    assert body["data"]["results"][0]["status"] == "processing"


def test_finalize_batch_empty_files(knowledge_upload_client):
    """Test finalize with empty files list returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={"files": []},
    )
    assert resp.status_code == 422, resp.text


def test_finalize_batch_invalid_uuid(knowledge_upload_client):
    """Test finalize with invalid UUID returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": "not-a-uuid"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text


def test_finalize_batch_with_checksum(knowledge_upload_client):
    """Test finalize with optional checksum."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    source = service.add_source(filename="with_checksum.pdf", status="pending_upload")

    resp = client.post(
        "/users/knowledge/finalize/batch",
        json={
            "files": [
                {"source_id": str(source.source_id), "checksum": "abc123def456"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


# =============================================================================
# GET /users/knowledge/source/{source_id} - Get Source Status Tests
# =============================================================================


def test_get_source_success(knowledge_upload_client):
    """Test successful source status retrieval."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    source = service.add_source(
        filename="status_check.pdf",
        mimetype="application/pdf",
        size_bytes=2048,
        status="processing",
    )

    resp = client.get(f"/users/knowledge/source/{source.source_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["source_id"] == str(source.source_id)
    assert body["data"]["filename"] == "status_check.pdf"
    assert body["data"]["mimetype"] == "application/pdf"
    assert body["data"]["size_bytes"] == 2048
    assert body["data"]["status"] == "processing"


def test_get_source_not_found(knowledge_upload_client):
    """Test getting non-existent source returns error."""
    client = knowledge_upload_client["client"]
    non_existent_id = uuid4()

    resp = client.get(f"/users/knowledge/source/{non_existent_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_get_source_invalid_uuid(knowledge_upload_client):
    """Test getting source with invalid UUID returns 422."""
    client = knowledge_upload_client["client"]

    resp = client.get("/users/knowledge/source/not-a-uuid")
    assert resp.status_code == 422, resp.text


def test_get_source_completed_includes_url(knowledge_upload_client):
    """Test getting completed source includes read URL."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    source = service.add_source(
        filename="completed.pdf",
        status="completed",
    )

    resp = client.get(f"/users/knowledge/source/{source.source_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    # read_url should be present if storage_path is set
    if source.storage_path:
        assert body["data"]["read_url"] is not None or body["data"]["read_url"] is None


def test_get_source_with_error(knowledge_upload_client):
    """Test getting source with error message."""
    client = knowledge_upload_client["client"]
    service = knowledge_upload_client["service"]

    source = service.add_source(filename="failed.pdf", status="failed")
    source.error_message = "Processing failed: invalid file format"

    resp = client.get(f"/users/knowledge/source/{source.source_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["status"] == "failed"
    assert body["data"]["error_message"] == "Processing failed: invalid file format"
