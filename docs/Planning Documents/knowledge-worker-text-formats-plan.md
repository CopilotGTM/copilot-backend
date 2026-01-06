# Knowledge Worker Enhancement Plan: Text, JSON, and XML Support

## 1. Overview

The goal is to enhance the Knowledge Worker to better support simple text-based formats: `.txt`, `.json`, and `.xml`.
Currently, the system uses a generic `TextParser` for these formats, but the processing logic is suboptimal for structured data.

## 2. Current State Analysis

### 2.1. Existing Files

- **`src/workers/knowledge_worker/parsers/text_parser.py`**:
  - Implements `TextParser` class.
  - Registers support for:
    - `text/plain`
    - `text/markdown`
    - `text/csv`
    - `text/html`
    - `application/json`
    - `application/xml`
    - `text/xml`
  - **Issue**: It calls `ContentProcessor.clean_text(content)`.

- **`src/workers/knowledge_worker/utils/content_processor.py`**:
  - `clean_text` method:

    ```python
    # Remove excessive whitespace
    text = re.sub(r"\s+", " ", text)
    ```

  - **Impact**: This replaces all newlines and indentation with a single space.
    - **JSON**: Becomes a single line string. Valid, but hard for LLMs to read structure.
    - **XML**: Becomes a single line string. Hard to read.
    - **Markdown/CSV**: Structure is destroyed (tables/lists in markdown, rows in CSV are lost).

### 2.2. Registry

- **`src/workers/knowledge_worker/parsers/registry.py`**:
  - Handles registration of parsers. `TextParser` currently claims all the above MIME types.

## 3. Proposed Solution

To properly support these formats, we need to preserve their structure (newlines and indentation).

### 3.1. Strategy

We will split the responsibilities or enhance the `TextParser` to handle structure better. Given the distinct nature of JSON and XML, specialized parsers are recommended.

### 3.2. Implementation Steps

#### Step 1: Create `JsonParser`

- **File**: `src/workers/knowledge_worker/parsers/json_parser.py`
- **MIME Types**: `application/json`
- **Logic**:
  - Load content using `json.loads`.
  - Dump content using `json.dumps(data, indent=2)` to create a pretty-printed string.
  - This ensures the LLM receives a structured, readable JSON representation.
  - Do **not** use `ContentProcessor.clean_text` (or use a modified version that preserves newlines).

#### Step 2: Create `XmlParser`

- **File**: `src/workers/knowledge_worker/parsers/xml_parser.py`
- **MIME Types**: `application/xml`, `text/xml`
- **Logic**:
  - Parse XML (e.g., using `xml.dom.minidom` or `lxml`).
  - Pretty-print the XML string.
  - Alternatively, if only text is needed, extract text nodes while preserving some structure. However, for "Knowledge Base", keeping tags often helps context.
  - Ensure no image extraction is attempted (as per requirements).

#### Step 3: Update `TextParser`

- **File**: `src/workers/knowledge_worker/parsers/text_parser.py`
- **Changes**:
  - Remove `application/json`, `application/xml`, `text/xml` from `supported_mimetypes`.
  - **Improvement**: Consider relaxing `clean_text` for `text/plain` as well. Flattening paragraphs into a single line might not be ideal. A better approach for text is to normalize whitespace but keep paragraph breaks (e.g., replace `\n\n+` with `\n\n` and single `\n` with space, or just keep `\n`).

#### Step 4: Register New Parsers

- Ensure the new parsers are imported so the `@ParserRegistry.register` decorator runs. This is usually done in `src/workers/knowledge_worker/parsers/__init__.py` or by importing them in the service that initializes the registry.

## 4. Files to be Created/Modified

| File | Action | Description |
|------|--------|-------------|
| `src/workers/knowledge_worker/parsers/json_parser.py` | Create | New parser for JSON files. |
| `src/workers/knowledge_worker/parsers/xml_parser.py` | Create | New parser for XML files. |
| `src/workers/knowledge_worker/parsers/text_parser.py` | Modify | Remove JSON/XML support; improve text cleaning. |
| `src/workers/knowledge_worker/utils/content_processor.py` | Modify | (Optional) Add `clean_structured_text` or similar if needed. |

## 5. Testing

- Create test files for `.json` and `.xml`.
- Verify that the parsed chunks retain structure (newlines/indentation).
