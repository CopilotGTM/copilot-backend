from unittest.mock import MagicMock, patch

import pytest
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.parsers.docling_parser import DoclingParser


@pytest.fixture
def docling_parser():
    return DoclingParser()


@pytest.fixture
def context():
    return ProcessingContext(
        organisation_id="org-123",
        source_id="source-123",
        user_id="user-123",
        mimetype="application/pdf",
        original_filename="test.pdf",
    )


def test_supported_mimetypes():
    mimetypes = DoclingParser.supported_mimetypes()
    assert "application/pdf" in mimetypes
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in mimetypes
    assert "application/vnd.openxmlformats-officedocument.presentationml.presentation" in mimetypes
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in mimetypes
    assert "text/html" in mimetypes
    assert "image/png" in mimetypes


@patch("src.workers.knowledge_worker.parsers.docling_parser.get_hybrid_chunker")
@patch("src.workers.knowledge_worker.parsers.docling_parser.get_doc_converter")
def test_parse_pdf(mock_get_converter, mock_get_chunker, docling_parser, context, tmp_path):
    # Mock the converter
    mock_converter = mock_get_converter.return_value
    mock_doc = MagicMock()
    mock_converter.convert.return_value = MagicMock(document=mock_doc)

    # Mock the chunker
    mock_chunker = mock_get_chunker.return_value
    mock_chunk = MagicMock()
    mock_chunk.text = "# PDF Content"
    mock_chunk.meta.headings = ["Header"]
    mock_chunker.chunk.return_value = [mock_chunk]
    mock_chunker.contextualize.return_value = "# PDF Content"

    file_path = tmp_path / "test.pdf"
    file_path.touch()

    result = docling_parser.parse(file_path, context)

    mock_converter.convert.assert_called_once_with(str(file_path))
    assert len(result.chunks) == 1
    assert result.chunks[0].text == "# PDF Content"
    assert "docling_doc" in result.metadata


@patch("src.workers.knowledge_worker.parsers.docling_parser.get_hybrid_chunker")
@patch("src.workers.knowledge_worker.parsers.docling_parser.get_doc_converter")
def test_parse_docx(mock_get_converter, mock_get_chunker, docling_parser, tmp_path):
    context = ProcessingContext(
        organisation_id="org",
        source_id="src",
        user_id="usr",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        original_filename="test.docx",
    )

    mock_converter = mock_get_converter.return_value
    mock_doc = MagicMock()
    mock_converter.convert.return_value = MagicMock(document=mock_doc)

    mock_chunker = mock_get_chunker.return_value
    mock_chunk = MagicMock()
    mock_chunk.text = "# Word Content"
    mock_chunk.meta.headings = []
    mock_chunker.chunk.return_value = [mock_chunk]
    mock_chunker.contextualize.return_value = "# Word Content"

    file_path = tmp_path / "test.docx"
    file_path.touch()

    result = docling_parser.parse(file_path, context)

    assert result.chunks[0].text == "# Word Content"
    assert "docling_doc" in result.metadata


@patch("src.workers.knowledge_worker.parsers.docling_parser.get_hybrid_chunker")
@patch("src.workers.knowledge_worker.parsers.docling_parser.get_doc_converter")
def test_parse_pptx(mock_get_converter, mock_get_chunker, docling_parser, tmp_path):
    context = ProcessingContext(
        organisation_id="org",
        source_id="src",
        user_id="usr",
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        original_filename="test.pptx",
    )

    mock_converter = mock_get_converter.return_value
    mock_doc = MagicMock()
    mock_converter.convert.return_value = MagicMock(document=mock_doc)

    mock_chunker = mock_get_chunker.return_value
    mock_chunk = MagicMock()
    mock_chunk.text = "# Slide Content"
    mock_chunk.meta.headings = []
    mock_chunker.chunk.return_value = [mock_chunk]
    mock_chunker.contextualize.return_value = "# Slide Content"

    file_path = tmp_path / "test.pptx"
    file_path.touch()

    result = docling_parser.parse(file_path, context)

    assert result.chunks[0].text == "# Slide Content"
    assert "docling_doc" in result.metadata


@patch("src.workers.knowledge_worker.parsers.docling_parser.get_hybrid_chunker")
@patch("src.workers.knowledge_worker.parsers.docling_parser.get_doc_converter")
def test_parse_xlsx(mock_get_converter, mock_get_chunker, docling_parser, tmp_path):
    context = ProcessingContext(
        organisation_id="org",
        source_id="src",
        user_id="usr",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_filename="test.xlsx",
    )

    mock_converter = mock_get_converter.return_value
    mock_doc = MagicMock()
    mock_converter.convert.return_value = MagicMock(document=mock_doc)

    mock_chunker = mock_get_chunker.return_value
    mock_chunk = MagicMock()
    mock_chunk.text = "| Col1 | Col2 |\n|---|---|\n| Val1 | Val2 |"
    mock_chunk.meta.headings = []
    mock_chunker.chunk.return_value = [mock_chunk]
    mock_chunker.contextualize.return_value = "| Col1 | Col2 |\n|---|---|\n| Val1 | Val2 |"

    file_path = tmp_path / "test.xlsx"
    file_path.touch()

    result = docling_parser.parse(file_path, context)

    assert "| Col1 | Col2 |" in result.chunks[0].text
    assert "docling_doc" in result.metadata


@patch("src.workers.knowledge_worker.parsers.docling_parser.get_doc_converter")
def test_parse_error_handling(mock_get_converter, docling_parser, context, tmp_path):
    mock_converter = mock_get_converter.return_value
    mock_converter.convert.side_effect = Exception("Conversion failed")

    file_path = tmp_path / "error.pdf"
    file_path.touch()

    result = docling_parser.parse(file_path, context)

    assert len(result.chunks) == 0
    # The parser logs the error but returns empty content
