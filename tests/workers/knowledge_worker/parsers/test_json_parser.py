import json
from pathlib import Path

import pytest
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.parsers.json_parser import JsonParser


@pytest.fixture
def json_parser():
    return JsonParser(max_chunk_size=100)  # Small chunk size for testing splitting


@pytest.fixture
def context():
    return ProcessingContext(
        organisation_id="org-123",
        source_id="source-123",
        user_id="user-123",
        mimetype="application/json",
        original_filename="test.json",
    )


def test_supported_mimetypes():
    assert "application/json" in JsonParser.supported_mimetypes()


def test_parse_small_json_object(json_parser, context, tmp_path):
    data = {"key": "value", "num": 123}
    file_path = tmp_path / "test.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")

    result = json_parser.parse(file_path, context)

    assert len(result.chunks) == 1
    assert '"key": "value"' in result.chunks[0].text
    assert result.metadata["json_type"] == "object"


def test_parse_large_json_object_splitting(context, tmp_path):
    # Create a parser with very small chunk size to force splitting
    parser = JsonParser(max_chunk_size=20)

    data = {"key1": "value1_is_long_enough_to_split", "key2": "value2_is_also_long_enough"}
    file_path = tmp_path / "large.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")

    result = parser.parse(file_path, context)

    # Should be split into at least 2 chunks (one for each key)
    assert len(result.chunks) >= 2
    assert "key1" in result.chunks[0].text
    assert "key2" in result.chunks[1].text


def test_parse_json_array(json_parser, context, tmp_path):
    data = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]
    file_path = tmp_path / "array.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")

    result = json_parser.parse(file_path, context)

    assert len(result.chunks) == 1  # Should fit in 100 chars
    assert result.metadata["json_type"] == "array"


def test_parse_large_json_array_splitting(context, tmp_path):
    parser = JsonParser(max_chunk_size=20)
    data = ["item1_long_enough", "item2_long_enough"]
    file_path = tmp_path / "large_array.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")

    result = parser.parse(file_path, context)

    assert len(result.chunks) >= 2
    assert "item1" in result.chunks[0].text
    assert "item2" in result.chunks[1].text


def test_parse_invalid_json_fallback(json_parser, context, tmp_path):
    content = "{invalid_json: true"
    file_path = tmp_path / "invalid.json"
    file_path.write_text(content, encoding="utf-8")

    result = json_parser.parse(file_path, context)

    assert len(result.chunks) == 1
    assert result.chunks[0].text == content
    assert result.metadata["error"] == "invalid_json"


def test_file_not_found(json_parser, context):
    result = json_parser.parse(Path("non_existent.json"), context)
    assert len(result.chunks) == 0
