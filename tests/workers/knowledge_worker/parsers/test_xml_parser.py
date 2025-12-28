import pytest
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.parsers.xml_parser import XmlParser


@pytest.fixture
def xml_parser():
    return XmlParser(max_chunk_size=100)


@pytest.fixture
def context():
    return ProcessingContext(
        organisation_id="org-123",
        source_id="source-123",
        user_id="user-123",
        mimetype="application/xml",
        original_filename="test.xml",
    )


def test_supported_mimetypes():
    assert "application/xml" in XmlParser.supported_mimetypes()
    assert "text/xml" in XmlParser.supported_mimetypes()


def test_parse_small_xml(xml_parser, context, tmp_path):
    content = "<root><child>value</child></root>"
    file_path = tmp_path / "test.xml"
    file_path.write_text(content, encoding="utf-8")

    result = xml_parser.parse(file_path, context)

    assert len(result.chunks) == 1
    assert "<child>value</child>" in result.chunks[0].text
    assert result.metadata["root_tag"] == "root"


def test_parse_large_xml_splitting(context, tmp_path):
    parser = XmlParser(max_chunk_size=50)
    content = "<root><item>long_content_item_1</item><item>long_content_item_2</item></root>"
    file_path = tmp_path / "large.xml"
    file_path.write_text(content, encoding="utf-8")

    result = parser.parse(file_path, context)

    assert len(result.chunks) >= 2
    assert "item_1" in result.chunks[0].text
    assert "item_2" in result.chunks[1].text


def test_parse_invalid_xml_fallback(xml_parser, context, tmp_path):
    content = "<root><unclosed>"
    file_path = tmp_path / "invalid.xml"
    file_path.write_text(content, encoding="utf-8")

    result = xml_parser.parse(file_path, context)

    assert len(result.chunks) == 1
    assert result.chunks[0].text == content
    assert result.metadata["error"] == "invalid_xml"


def test_security_xxe_prevention(xml_parser, context, tmp_path):
    # XXE payload
    xxe_content = """<?xml version="1.0"?>
    <!DOCTYPE root [
        <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]>
    <root>&xxe;</root>
    """
    file_path = tmp_path / "xxe.xml"
    file_path.write_text(xxe_content, encoding="utf-8")

    # defusedxml should raise an error or handle it safely.
    # The parser catches errors and falls back to text, but we want to ensure it doesn't resolve the entity.
    # If it falls back to text, the text will contain the raw XML, which is fine as long as it's not resolved.

    result = xml_parser.parse(file_path, context)

    # Check that it didn't crash and didn't resolve the entity (we can't easily check resolution here without a real file,
    # but we can check if it fell back or parsed safely).
    # Since defusedxml raises an error on entity expansion by default (or forbids it),
    # we expect either an error in metadata or safe parsing.

    # If it parsed successfully (no error metadata), it means it ignored the entity or treated it as text.
    # If it failed, it should have error metadata.

    # defusedxml blocks the XXE attack by raising EntitiesForbidden.
    # The parser catches this and returns empty chunks safely.
    # This verifies that malicious XML is handled without crashing or resolving entities.
    assert len(result.chunks) == 0


def test_billion_laughs_attack(xml_parser, context, tmp_path):
    # Billion laughs attack
    content = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
     <!ENTITY lol "lol">
     <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
     <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
     <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <lolz>&lol3;</lolz>
    """
    file_path = tmp_path / "billion_laughs.xml"
    file_path.write_text(content, encoding="utf-8")

    # Should fail safely (likely fallback to text due to error)
    result = xml_parser.parse(file_path, context)

    # The parser catches the EntitiesForbidden error and returns empty chunks
    assert len(result.chunks) == 0
    # Or it might return an error in metadata if we checked that
    # if "error" in result.metadata:
    #     assert result.metadata["error"] == "invalid_xml"
