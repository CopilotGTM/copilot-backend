import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.parsers.text_parser import TextParser


@pytest.fixture
def parser():
    return TextParser()


@pytest.fixture
def context():
    return Mock(spec=ProcessingContext, source_id="test_source")


def test_parse_plain_text(parser, context):
    content = "Line 1\nLine 2\nLine 3"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(content)
        f_path = Path(f.name)

    try:
        result = parser.parse(f_path, context)
        assert len(result.chunks) == 1
        assert result.chunks[0].text == content
        assert result.metadata["chunk_count"] == 1
    finally:
        f_path.unlink()
    content = "col1,col2\nval1,val2"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(content)
        f_path = Path(f.name)

    try:
        result = parser.parse(f_path, context)
        assert len(result.chunks) == 1
        assert result.chunks[0].text == content
    finally:
        f_path.unlink()


def test_empty_file(parser, context):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        pass
        f_path = Path(f.name)

    try:
        result = parser.parse(f_path, context)
        assert len(result.chunks) == 0
    finally:
        f_path.unlink()
