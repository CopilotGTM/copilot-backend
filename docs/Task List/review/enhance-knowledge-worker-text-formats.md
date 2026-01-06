# Enhance Knowledge Worker for Text, JSON, and XML Support

## Overview

Currently, the knowledge worker is optimized for PDFs. We need to enhance it to support simple text-based formats: `.txt`, `.json`, and `.xml`.
There is already a `TextParser` in `src/workers/knowledge_worker/parsers/text_parser.py` which handles plain text.
We need to ensure this parser (or new ones) can handle JSON and XML formats effectively.

## Requirements

1. **Text Files (.txt)**: Ensure `TextParser` correctly handles plain text files.
2. **JSON Files (.json)**: Support parsing of JSON files. This might involve treating them as plain text or parsing the JSON structure to extract relevant text.
3. **XML Files (.xml)**: Support parsing of XML files.
    * **Note**: For XML, we should **skip** image extraction for now. Treat it as text-based content.

## Relevant Files

The LLM should look at the following files to implement this task:

* [src/workers/knowledge_worker/parsers/text_parser.py](src/workers/knowledge_worker/parsers/text_parser.py) - Existing text parser.
* [src/workers/knowledge_worker/parsers/registry.py](src/workers/knowledge_worker/parsers/registry.py) - Registry for parsers.
* [src/workers/knowledge_worker/parsers/protocol.py](src/workers/knowledge_worker/parsers/protocol.py) - Protocol definition for parsers.

## Instructions

1. Analyze `src/workers/knowledge_worker/parsers/text_parser.py`.
2. Update `TextParser` or create new parsers (e.g., `JsonParser`, `XmlParser`) if specific handling is needed.
3. Register the appropriate MIME types (e.g., `text/plain`, `application/json`, `application/xml`, `text/xml`) in the `supported_mimetypes` method of the parser(s).
4. Ensure that the implementation adheres to the `IDocumentParser` protocol.
5. Verify that XML parsing does not attempt to extract images.

## Completion

Once this task is completed, please move this file to the `docs/Task List/review/` folder.
