"""
Unit tests for JsonExtractor class used by GeneralAgent.

These tests cover the `extract` and related helper methods
to ensure robust parsing of various JSON formats from LLM responses.
"""

import json

import pytest
from src.agents.common.json_extractor import JsonExtractor


class TestJsonExtraction:
    """Test suite for JSON extraction from LLM responses."""

    @pytest.fixture
    def extractor(self):
        """Create a JsonExtractor instance for testing."""
        return JsonExtractor()

    # =========================================================================
    # Sentinel Format Tests (⟦AEP:1⟧ ... ⟦/AEP⟧)
    # =========================================================================

    def test_extract_sentinel_with_fenced_json(self, extractor):
        """Test extraction from sentinel block with fenced JSON."""
        text = """Here is my answer to your question.

⟦AEP:1⟧
```json
{"version": "aep/1", "confidence_score": "high", "suggested_questions": ["Q1", "Q2"]}
```
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"
        assert parsed["confidence_score"] == "high"
        assert len(parsed["suggested_questions"]) == 2

    def test_extract_sentinel_without_fence(self, extractor):
        """Test extraction from sentinel block without fenced code."""
        text = """Answer here.

⟦AEP:1⟧
{"version": "aep/1", "confidence_score": "medium"}
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"
        assert parsed["confidence_score"] == "medium"

    def test_extract_sentinel_with_nested_objects(self, extractor):
        """Test extraction with nested JSON objects in sentinel."""
        text = """Response.

⟦AEP:1⟧
```json
{
  "version": "aep/1",
  "confidence_score": "high",
  "sources": [
    {"label": "Doc 1", "url": "https://example.com/1"},
    {"label": "Doc 2", "url": "https://example.com/2"}
  ],
  "switch_agent": {"to": "RFP_SHEET", "reason": "User needs RFP help"}
}
```
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"
        assert len(parsed["sources"]) == 2
        assert parsed["switch_agent"]["to"] == "RFP_SHEET"

    def test_extract_sentinel_missing_end(self, extractor):
        """Test that missing end sentinel falls back to other methods."""
        text = """Answer.

⟦AEP:1⟧
{"version": "aep/1"}
"""
        # Should still extract via bracket matching
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"

    def test_extract_sentinel_missing_start(self, extractor):
        """Test that missing start sentinel falls back to other methods."""
        text = """Answer.

{"version": "aep/1"}
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"

    # =========================================================================
    # Fenced Code Block Tests
    # =========================================================================

    def test_extract_fenced_json_with_hint(self, extractor):
        """Test extraction from fenced code block with json language hint."""
        text = """Here is the answer.

```json
{"version": "aep/1", "confidence_score": "low"}
```"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["confidence_score"] == "low"

    def test_extract_fenced_json_neutral_confidence(self, extractor):
        """Test extraction with neutral confidence score for greetings/pleasantries."""
        text = """Hello! How can I assist you today?

```json
{"version": "aep/1", "confidence_score": "neutral"}
```"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["confidence_score"] == "neutral"

    def test_extract_fenced_without_hint(self, extractor):
        """Test extraction from fenced code block without language hint."""
        text = """Answer text.

```
{"version": "aep/1", "confidence_score": "high"}
```"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["confidence_score"] == "high"

    def test_extract_fenced_multiline_json(self, extractor):
        """Test extraction of multiline JSON from fenced block."""
        text = """Response.

```json
{
  "version": "aep/1",
  "confidence_score": "medium",
  "suggested_questions": [
    "What about pricing?",
    "Can you explain the architecture?"
  ]
}
```"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["suggested_questions"]) == 2

    def test_extract_fenced_incomplete_block(self, extractor):
        """Test handling of incomplete fenced block (only one fence)."""
        text = """Answer.

```json
{"version": "aep/1"}
"""
        # Falls back to bracket matching
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"

    # =========================================================================
    # Bracket Matching Tests
    # =========================================================================

    def test_extract_raw_json_simple(self, extractor):
        """Test extraction of raw JSON without any wrapper."""
        text = """Here is the info: {"version": "aep/1", "confidence_score": "high"} end."""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"

    def test_extract_raw_json_nested(self, extractor):
        """Test extraction of nested JSON objects."""
        text = """Data: {"outer": {"inner": {"deep": "value"}}, "version": "aep/1"} done."""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["outer"]["inner"]["deep"] == "value"

    def test_extract_json_with_escaped_quotes(self, extractor):
        """Test extraction handles escaped quotes in strings."""
        text = """Result: {"message": "He said \\"hello\\"", "version": "aep/1"} end."""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["message"] == 'He said "hello"'

    def test_extract_json_with_braces_in_strings(self, extractor):
        """Test extraction handles braces inside string values."""
        text = """Info: {"code": "function() { return {}; }", "version": "aep/1"} done."""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert "{" in parsed["code"]
        assert parsed["version"] == "aep/1"

    def test_extract_json_with_unicode(self, extractor):
        """Test extraction handles unicode characters."""
        text = """Response: {"message": "Hello 世界 🌍", "version": "aep/1"} end."""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert "世界" in parsed["message"]
        assert "🌍" in parsed["message"]

    def test_extract_json_with_special_chars(self, extractor):
        """Test extraction handles special characters."""
        text = """Data: {"url": "https://example.com/path?q=1&b=2", "version": "aep/1"} done."""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert "?" in parsed["url"]
        assert "&" in parsed["url"]

    # =========================================================================
    # Edge Cases and Error Handling
    # =========================================================================

    def test_extract_empty_string(self, extractor):
        """Test extraction from empty string returns None."""
        result = extractor.extract("")
        assert result is None

    def test_extract_none_input(self, extractor):
        """Test extraction from None-like empty input."""
        result = extractor.extract("")
        assert result is None

    def test_extract_no_json_present(self, extractor):
        """Test extraction when no JSON is present."""
        text = "This is just plain text without any JSON content."
        result = extractor.extract(text)
        assert result is None

    def test_extract_malformed_json(self, extractor):
        """Test extraction of malformed JSON (missing closing brace)."""
        text = """Data: {"version": "aep/1", "incomplete": true end."""
        result = extractor.extract(text)
        # Should return None since brackets don't match
        assert result is None

    def test_extract_only_opening_brace(self, extractor):
        """Test handling of text with only opening brace."""
        text = "Start { but never close"
        result = extractor.extract(text)
        assert result is None

    def test_extract_multiple_json_objects(self, extractor):
        """Test that first valid JSON object is extracted."""
        text = """First: {"id": 1} Second: {"id": 2}"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["id"] == 1  # Should get the first one

    def test_extract_json_array_not_object(self, extractor):
        """Test that JSON arrays are not extracted (we expect objects)."""
        text = """Data: [1, 2, 3] end."""
        result = extractor.extract(text)
        # Current implementation looks for { so arrays won't match
        assert result is None

    def test_extract_deeply_nested_json(self, extractor):
        """Test extraction of deeply nested JSON."""
        nested = {
            "level1": {"level2": {"level3": {"level4": {"value": "deep"}}}},
            "version": "aep/1",
        }
        text = f"Data: {json.dumps(nested)} end."
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["level1"]["level2"]["level3"]["level4"]["value"] == "deep"

    # =========================================================================
    # Real-world LLM Response Patterns
    # =========================================================================

    def test_extract_typical_llm_response(self, extractor):
        """Test extraction from a typical LLM response pattern."""
        text = """Based on the information from our internal knowledge base and web search, here are my recommendations:

## Key Points
1. Consider implementing a microservices architecture
2. Use Kubernetes for orchestration
3. Implement proper monitoring with Prometheus

These recommendations align with industry best practices for scalable systems.

⟦AEP:1⟧
```json
{
  "version": "aep/1",
  "confidence_score": "high",
  "suggested_questions": [
    "Would you like more details on Kubernetes setup?",
    "Should I compare this with serverless alternatives?"
  ],
  "sources": [
    {"label": "Internal Architecture Guide", "url": "kb://org/123/doc/456#page=12"},
    {"label": "Kubernetes Best Practices", "url": "https://kubernetes.io/docs/"}
  ]
}
```
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["version"] == "aep/1"
        assert parsed["confidence_score"] == "high"
        assert len(parsed["suggested_questions"]) == 2
        assert len(parsed["sources"]) == 2

    def test_extract_with_ask_field(self, extractor):
        """Test extraction with ask field for user prompts."""
        text = """I need some clarification before proceeding.

⟦AEP:1⟧
```json
{
  "version": "aep/1",
  "confidence_score": "medium",
  "ask": {
    "prompt": "Which deployment environment are you targeting?",
    "fields": [
      {
        "name": "environment",
        "kind": "single_select",
        "options": ["Production", "Staging", "Development"],
        "required": true
      }
    ]
  }
}
```
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["ask"]["prompt"] == "Which deployment environment are you targeting?"
        assert len(parsed["ask"]["fields"]) == 1
        assert parsed["ask"]["fields"][0]["kind"] == "single_select"

    def test_extract_with_agent_switch(self, extractor):
        """Test extraction with agent switch directive."""
        text = """This looks like an RFP-related request.

⟦AEP:1⟧
```json
{
  "version": "aep/1",
  "confidence_score": "high",
  "switch_agent": {
    "to": "RFP_SHEET",
    "reason": "User is asking about RFP responses",
    "confidence": 0.95
  }
}
```
⟦/AEP⟧"""
        result = extractor.extract(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["switch_agent"]["to"] == "RFP_SHEET"
        assert parsed["switch_agent"]["confidence"] == 0.95


class TestCleanLanguageHint:
    """Tests for the _clean_language_hint helper method."""

    @pytest.fixture
    def extractor(self):
        return JsonExtractor()

    def test_clean_json_hint(self, extractor):
        """Test removal of json language hint."""
        content = 'json\n{"key": "value"}'
        result = extractor._clean_language_hint(content)
        assert result == '{"key": "value"}'

    def test_clean_javascript_hint(self, extractor):
        """Test removal of javascript language hint."""
        content = 'javascript\n{"key": "value"}'
        result = extractor._clean_language_hint(content)
        assert result == '{"key": "value"}'

    def test_clean_no_hint(self, extractor):
        """Test content without language hint."""
        content = '{"key": "value"}'
        result = extractor._clean_language_hint(content)
        assert result == '{"key": "value"}'

    def test_clean_unknown_hint_preserved(self, extractor):
        """Test that unknown hints are not removed."""
        content = 'python\n{"key": "value"}'
        result = extractor._clean_language_hint(content)
        # python is not in the hints list, so full content returned
        assert "python" in result

    def test_clean_empty_content(self, extractor):
        """Test handling of empty content."""
        result = extractor._clean_language_hint("")
        assert result == ""

    def test_clean_whitespace_handling(self, extractor):
        """Test whitespace is properly trimmed."""
        content = '  json  \n  {"key": "value"}  '
        result = extractor._clean_language_hint(content)
        assert result == '{"key": "value"}'


class TestExtractFromBrackets:
    """Tests for the _extract_from_brackets method."""

    @pytest.fixture
    def extractor(self):
        return JsonExtractor()

    def test_no_opening_brace(self, extractor):
        """Test when no opening brace exists."""
        result = extractor._extract_from_brackets("no json here")
        assert result is None

    def test_simple_object(self, extractor):
        """Test simple object extraction."""
        result = extractor._extract_from_brackets('prefix {"a": 1} suffix')
        assert result == '{"a": 1}'

    def test_nested_objects(self, extractor):
        """Test nested object extraction."""
        result = extractor._extract_from_brackets('{"outer": {"inner": 1}}')
        assert result is not None
        parsed = json.loads(result)
        assert parsed["outer"]["inner"] == 1

    def test_string_with_braces(self, extractor):
        """Test that braces in strings don't confuse the parser."""
        text = '{"code": "if (x) { return y; }"}'
        result = extractor._extract_from_brackets(text)
        assert result is not None
        parsed = json.loads(result)
        assert "return y" in parsed["code"]
