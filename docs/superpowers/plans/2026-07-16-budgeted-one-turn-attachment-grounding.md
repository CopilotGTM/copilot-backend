# Budgeted One-Turn Attachment Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically ground attachments from the current user message, or from exactly the immediately previous user message when the current message has none, without allowing large documents or attachment batches to exhaust the model context.

**Architecture:** Keep raw files in organisation-scoped blob storage and manifests in chat history. WebChat and personal WhatsApp converge on `GeneralAgent`, so automatic grounding belongs there rather than in two channel-specific image loaders. Select at most three attachments deterministically from the latest two user turns, open them through the shared `read_chat_attachment` path before the model call, and enforce one shared 3,000-token text budget for the whole run. Preserve the tool for older or deeper reads; automatic grounding is a bounded convenience, not full-document retrieval.

**Tech Stack:** Python 3.11, Agno `count_text_tokens`, pypdf/python-docx/python-pptx/openpyxl, Azure organisation blob storage, pytest.

---

## Decisions and boundaries

- The latest user message wins when it has attachments.
- When the latest user message has none, only the immediately previous user message is eligible. Do not search further back.
- Automatically open at most `3` attachments per run. Remaining manifest entries stay available to the normal attachment tool.
- Automatically inject at most `3,000` text tokens across all selected documents, not 3,000 tokens per document.
- Keep the existing `12,000` character ceiling as an extraction safety bound. Token trimming happens after bounded extraction because token counting alone would still let parsers process an entire large document.
- Images use native vision input and count against the three-item cap. WebChat and WhatsApp defer loading to the shared agent path so each selected image is downloaded once.
- Audio is excluded from automatic grounding because WhatsApp already transcribes current voice/audio during channel input preparation.
- Apply the three-item cap before filtering audio, unsupported files, or legacy entries.
- Legacy manifests without MIME metadata remain explicitly tool-readable but are not automatically opened.
- The text budget includes wrappers, filenames, partial markers, and legacy attachment text already supplied to the run.
- A truncated document must be labeled as partial. The agent must not claim it read the complete file.
- This does not implement full-document semantic retrieval. `copilotgtm:` comments must name that ceiling and point to chunked retrieval as the upgrade path.

## Files

- Modify: `src/infra/config/configs.py` — automatic item and token budgets.
- Modify: `src/agents/utils.py` — deterministic one-turn selection and token-bounded prefix helper.
- Modify: `src/infra/media/chat_attachment_text.py` — stop document extraction once the character ceiling is reached.
- Modify: `src/agents/tools/communication/chat_attachments.py` — pass the extraction ceiling down and preserve truncation metadata.
- Modify: `src/workers/chat_worker/services/message_processing_service.py` — defer WebChat image loading to the shared agent.
- Modify: `src/workers/chat_worker/services/bot_message_processing_service.py` — defer WhatsApp image loading while preserving its existing audio transcription.
- Modify: `src/agents/workflows/general/agent.py` — ground selected images/documents for both channels under one shared budget.
- Modify: `tests/agents/test_utils.py`
- Modify: `tests/infra/media/test_chat_attachment_text.py`
- Modify: `tests/agents/tools/communication/test_chat_attachments.py`
- Modify: `tests/workers/chat_worker/services/test_message_processing_attachments.py`
- Modify: `tests/workers/chat_worker/services/test_bot_message_processing_service_messages.py`
- Modify: `tests/agents/test_general_agent_stream.py`


+> **Implementation correction:** Any later step that says WebChat should preload native images is superseded by the shared architecture above. Both WebChat and external-channel workers pass `images=None`; `GeneralAgent` opens selected current or carried images through `read_chat_attachment`.
---

### Task 1: Lock deterministic selection and budgets

**Files:**
- Modify: `src/infra/config/configs.py:1593-1595`
- Modify: `src/agents/utils.py:13-143`
- Test: `tests/agents/test_utils.py:177-249`

- [ ] **Step 1: Replace heuristic follow-up tests with deterministic one-turn tests**

Replace the `pending_attachment_ids_for_followup` tests with:

```python
def test_automatic_attachment_ids_prefers_current_user_attachments() -> None:
    messages = [
        {
            "role": "user",
            "content": "first",
            "attachment_manifest": "[Attached files]\n- id: old-id",
        },
        {"role": "assistant", "content": "first response"},
        {
            "role": "user",
            "content": "second",
            "attachment_manifest": "[Attached files]\n- id: current-id",
        },
    ]

    assert automatic_attachment_ids(messages, max_items=3) == ["current-id"]


def test_automatic_attachment_ids_carries_previous_user_attachments_once() -> None:
    messages = [
        {
            "role": "user",
            "content": "first",
            "attachment_manifest": "[Attached files]\n- id: prior-id",
        },
        {"role": "assistant", "content": "first response"},
        {"role": "user", "content": "follow up", "attachment_manifest": None},
    ]

    assert automatic_attachment_ids(messages, max_items=3) == ["prior-id"]


def test_automatic_attachment_ids_expires_after_one_empty_user_turn() -> None:
    messages = [
        {
            "role": "user",
            "content": "first",
            "attachment_manifest": "[Attached files]\n- id: expired-id",
        },
        {"role": "assistant", "content": "first response"},
        {"role": "user", "content": "second", "attachment_manifest": None},
        {"role": "assistant", "content": "second response"},
        {"role": "user", "content": "third", "attachment_manifest": None},
    ]

    assert automatic_attachment_ids(messages, max_items=3) == []


def test_automatic_attachment_ids_caps_the_batch() -> None:
    messages = [
        {
            "role": "user",
            "content": "compare these",
            "attachment_manifest": (
                "[Attached files]\n- id: one\n- id: two\n- id: three\n- id: four"
            ),
        }
    ]

    assert automatic_attachment_ids(messages, max_items=3) == ["one", "two", "three"]
```

Update the import in the test from `pending_attachment_ids_for_followup` to `automatic_attachment_ids`.

- [ ] **Step 2: Run the selector tests and verify they fail**

Run:

```powershell
uv run pytest tests/agents/test_utils.py -k automatic_attachment_ids -v
```

Expected: collection/import failure because `automatic_attachment_ids` does not exist.

- [ ] **Step 3: Add configuration for one shared automatic budget**

Add under `ChatFileUploadConfig`:

```python
    # Automatic grounding is deliberately smaller than the upload limits. Older or
    # larger attachment content remains accessible through read_chat_attachment.
    ATTACHMENT_AUTO_READ_MAX_ITEMS: int = int(
        os.getenv("CHAT_ATTACHMENT_AUTO_READ_MAX_ITEMS", "3")
    )
    ATTACHMENT_AUTO_READ_MAX_TOKENS: int = int(
        os.getenv("CHAT_ATTACHMENT_AUTO_READ_MAX_TOKENS", "3000")
    )
```

- [ ] **Step 4: Replace the text/assistant keyword heuristic with the latest-two-user rule**

Delete `_ATTACHMENT_SETUP_TEXT`, `_ATTACHMENT_ASSISTANT_TERMS`, `_SHORT_CONTINUATION_MAX_WORDS`, `_is_attachment_setup_text`, `_is_short_continuation_text`, `_assistant_kept_attachment_context`, and `pending_attachment_ids_for_followup` from `src/agents/utils.py`. Add:

```python
def automatic_attachment_ids(
    message_list: Sequence[ChatMessage | dict[str, Any]],
    *,
    max_items: int,
) -> list[str]:
    """Select current attachments or carry the previous user turn exactly once.

    The rule is deterministic so vague language such as "it" cannot accidentally
    reopen an older file. Manifests keep older attachments visible to the normal
    read_chat_attachment tool without paying to open them on every request.
    """
    user_manifests: list[str | None] = []
    for message in reversed(message_list):
        if _message_role(message) != "user":
            continue
        manifest = message.get("attachment_manifest") if isinstance(message, dict) else None
        user_manifests.append(manifest)
        if len(user_manifests) == 2:
            break

    if not user_manifests:
        return []

    current_ids = extract_attachment_ids_from_manifest(user_manifests[0])
    selected = current_ids or (
        extract_attachment_ids_from_manifest(user_manifests[1])
        if len(user_manifests) == 2
        else []
    )
    return selected[: max(max_items, 0)]
```

- [ ] **Step 5: Run the selector tests**

Run:

```powershell
uv run pytest tests/agents/test_utils.py -k automatic_attachment_ids -v
```

Expected: four tests pass.

- [ ] **Step 6: Commit the deterministic policy**

```powershell
git add src/infra/config/configs.py src/agents/utils.py tests/agents/test_utils.py
git commit -m "feat: define one-turn attachment grounding policy"
```

---

### Task 2: Stop large document extraction at the safety boundary

**Files:**
- Modify: `src/infra/media/chat_attachment_text.py:32-111`
- Modify: `src/agents/tools/communication/chat_attachments.py:261-304`
- Test: `tests/infra/media/test_chat_attachment_text.py:143-176`
- Test: `tests/agents/tools/communication/test_chat_attachments.py:315-430`

- [ ] **Step 1: Write a PDF regression proving later pages are not parsed**

Add:

```python
def test_extract_text_from_pdf_stops_at_max_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    extracted_pages: list[int] = []

    class _FakePage:
        def __init__(self, number: int):
            self.number = number

        def extract_text(self):
            extracted_pages.append(self.number)
            return f"page-{self.number}-" + ("x" * 20)

    class _FakeReader:
        def __init__(self, _stream):
            self.pages = [_FakePage(1), _FakePage(2), _FakePage(3)]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)

    result = extract_text_from_bytes(
        b"%PDF-fake",
        "application/pdf",
        "large.pdf",
        max_chars=25,
    )

    assert len(result) == 25
    assert extracted_pages == [1]
```

- [ ] **Step 2: Write a tool regression proving the limit is passed into extraction**

In `test_read_chat_attachment_truncates_long_content`, replace the extraction stub with one that captures `max_chars`:

```python
    seen: dict[str, int | None] = {}

    def fake_extract(content, mimetype, filename, *, max_chars=None):
        seen["max_chars"] = max_chars
        return "x" * 100

    monkeypatch.setattr(
        "src.agents.tools.communication.chat_attachments.extract_text_from_bytes",
        fake_extract,
    )

    result = await read_chat_attachment(str(attachment_id), context=context, max_chars=10)

    assert seen == {"max_chars": 10}
    assert result["content"] == "x" * 10
    assert result["truncated"] is True
```

- [ ] **Step 3: Run both regressions and verify they fail**

```powershell
uv run pytest tests/infra/media/test_chat_attachment_text.py::test_extract_text_from_pdf_stops_at_max_chars tests/agents/tools/communication/test_chat_attachments.py::test_read_chat_attachment_truncates_long_content -v
```

Expected: failures because the extractor has no `max_chars` parameter.

- [ ] **Step 4: Add bounded accumulation to the shared extractor**

Change the signature and add the local accumulator at the start of `extract_text_from_bytes`:

```python
def extract_text_from_bytes(
    content: bytes,
    mimetype: str,
    _filename: str,
    *,
    max_chars: int | None = None,
) -> str:
    """Extract at most max_chars of readable text from one in-memory attachment.

    Chat uploads are capped at 50 MB, but parsing every page/row before truncating
    wastes worker time and memory. Each format loop stops as soon as this bounded
    prefix is full.
    """
    normalized = (mimetype or "").lower()
    parts: list[str] = []
    used_chars = 0

    def append_part(value: str, separator: str = "\n\n") -> bool:
        nonlocal used_chars
        if not value:
            return True
        addition = (separator if parts else "") + value
        if max_chars is None:
            parts.append(addition)
            used_chars += len(addition)
            return True

        remaining = max_chars - used_chars
        if remaining <= 0:
            return False
        parts.append(addition[:remaining])
        used_chars += min(len(addition), remaining)
        return len(addition) <= remaining
```

For PDF, replace the page list with early stopping:

```python
    if normalized == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        for page in reader.pages:
            text = page.extract_text()
            if text and not append_part(text):
                break
        return "".join(parts)
```

Replace the DOCX branch with:

```python
    if normalized == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from docx import Document

        doc = Document(io.BytesIO(content))
        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            if text and not append_part(text):
                break
        return "".join(parts)
```

Replace the PPTX branch with:

```python
    if normalized == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(io.BytesIO(content))
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_parts: list[str] = [f"--- Slide {slide_num} ---"]
            for shape in slide.shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    for child in shape.shapes:
                        if hasattr(child, "text") and child.text.strip():
                            slide_parts.append(child.text.strip())
                elif shape.has_table:
                    for row in shape.table.rows:
                        row_text = "\t".join(cell.text.strip() for cell in row.cells)
                        if row_text.strip():
                            slide_parts.append(row_text)
                elif hasattr(shape, "text") and shape.text.strip():
                    slide_parts.append(shape.text.strip())
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and notes.text.strip():
                    slide_parts.append(f"[Notes] {notes.text.strip()}")
            if len(slide_parts) > 1 and not append_part("\n".join(slide_parts), "\n"):
                break
        return "".join(parts)
```

Replace the XLSX branch with:

```python
    if normalized == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                sheet_started = False
                for row in worksheet.iter_rows(values_only=True):
                    values = [str(value).strip() if value is not None else "" for value in row]
                    if not any(values):
                        continue
                    if not sheet_started:
                        if not append_part(f"--- Sheet: {sheet_name} ---", "\n"):
                            return "".join(parts)
                        sheet_started = True
                    if not append_part("\t".join(values), "\n"):
                        return "".join(parts)
        finally:
            workbook.close()
        return "".join(parts)
```

For text-like formats, decode and slice directly:

```python
        decoded = content.decode("utf-8", errors="replace")
        return decoded if max_chars is None else decoded[:max_chars]
```

Keep all existing separators and output labels unchanged so existing callers/tests remain compatible.

- [ ] **Step 5: Pass the character ceiling into extraction and combine truncation flags**

In `read_chat_attachment`:

```python
        else:
            extracted = extract_text_from_bytes(
                content_bytes,
                mimetype,
                attachment.filename,
                max_chars=max_chars,
            )

        extraction_hit_limit = len(extracted) >= max_chars
        content, truncated_after_extraction = _truncate_text(extracted, max_chars)
        return {
            "attachment_id": str(attachment.attachment_id),
            "filename": attachment.filename,
            "mimetype": attachment.mimetype,
            "content_type": content_type,
            "content": content,
            "truncated": extraction_hit_limit or truncated_after_extraction,
        }
```

This is intentionally conservative: exactly filling the limit is reported as partial because the parser stopped without proving end-of-file.

Update every existing `extract_text_from_bytes` monkeypatch in `tests/agents/tools/communication/test_chat_attachments.py` to accept `*, max_chars=None`; otherwise the new keyword argument will break unrelated PDF/audio fixture paths.

- [ ] **Step 6: Run the focused extractor/tool tests**

```powershell
uv run pytest tests/infra/media/test_chat_attachment_text.py tests/agents/tools/communication/test_chat_attachments.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit bounded extraction**

```powershell
git add src/infra/media/chat_attachment_text.py src/agents/tools/communication/chat_attachments.py tests/infra/media/test_chat_attachment_text.py tests/agents/tools/communication/test_chat_attachments.py
git commit -m "fix: bound chat attachment extraction"
```

---

### Task 3: Enforce one token budget while grounding current or carried attachments

**Files:**
- Modify: `src/agents/utils.py`
- Modify: `src/workers/chat_worker/services/message_processing_service.py:858-882`
- Modify: `src/agents/workflows/general/agent.py:96-223`
- Test: `tests/workers/chat_worker/services/test_message_processing_attachments.py`
- Test: `tests/agents/test_general_agent_stream.py:151-223`

- [ ] **Step 1: Add token-prefix tests**

Add to `tests/agents/test_utils.py`:

```python
def test_truncate_to_token_budget_returns_prefix_and_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agents.utils.count_text_tokens",
        lambda text, _model_id: len(text),
    )

    content, truncated, used_tokens = truncate_to_token_budget("abcdefgh", 5, "model")

    assert content == "abcde"
    assert truncated is True
    assert used_tokens == 5


def test_truncate_to_token_budget_keeps_small_content(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.agents.utils.count_text_tokens",
        lambda text, _model_id: len(text),
    )

    assert truncate_to_token_budget("abc", 5, "model") == ("abc", False, 3)
```

- [ ] **Step 2: Implement token-bounded prefixing with Agno's installed tokenizer**

Import `count_text_tokens` from `agno.utils.tokens` and add:

```python
def truncate_to_token_budget(
    content: str,
    max_tokens: int,
    model_id: str,
) -> tuple[str, bool, int]:
    """Return the longest text prefix that fits the model-specific token budget."""
    if max_tokens <= 0:
        return "", bool(content), 0

    token_count = count_text_tokens(content, model_id)
    if token_count <= max_tokens:
        return content, False, token_count

    low, high = 0, len(content)
    while low < high:
        midpoint = (low + high + 1) // 2
        if count_text_tokens(content[:midpoint], model_id) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1

    prefix = content[:low]
    return prefix, True, count_text_tokens(prefix, model_id)
```

This reuses Agno/tiktoken already installed by the agent stack; add no dependency.

- [ ] **Step 3: Give current native images stable IDs and cap them**

In `_resolve_attachments`, apply the cap before filtering so every attachment type shares the same first-three boundary:

```python
        selected_attachments = attachments[
            : chat_file_upload_config.ATTACHMENT_AUTO_READ_MAX_ITEMS
        ]
        image_attachments = [
            attachment
            for attachment in selected_attachments
            if is_image_mimetype(attachment.mimetype)
        ]
```

```python
                    images.append(
                        AgnoImage(
                            id=str(attachment.attachment_id),
                            content=content,
                            format=(attachment.mimetype or "image/png").split("/")[-1],
                        )
                    )
```

Import `chat_file_upload_config` in the worker module if it is not already imported. Extend `test_resolve_attachments_loads_multiple_images` to provide four images and assert that only the first three paths are read and each returned image carries the corresponding attachment ID. Add a mixed `[document, image, image, image]` case proving the fourth attachment is not loaded; filtering before capping would violate the shared three-item contract.

- [ ] **Step 4: Replace pending grounding with automatic budgeted grounding tests**

Rename the tests to `_ground_automatic_attachments`. Add a two-document budget regression:

```python
@pytest.mark.asyncio
async def test_ground_automatic_attachments_shares_one_token_budget(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})
    agent._model_id = "test-model"

    async def fake_read(attachment_id, *, context, max_chars=12000):
        return {
            "filename": f"{attachment_id}.txt",
            "content_type": "text",
            "content": "x" * 20,
            "truncated": False,
        }

    monkeypatch.setattr(
        "src.agents.workflows.general.agent.read_chat_attachment",
        fake_read,
    )
    monkeypatch.setattr(
        "src.agents.workflows.general.agent.truncate_to_token_budget",
        lambda text, budget, model: (text[:budget], len(text) > budget, min(len(text), budget)),
    )
    monkeypatch.setattr(
        chat_file_upload_config,
        "ATTACHMENT_AUTO_READ_MAX_TOKENS",
        12,
    )

    messages = [{
        "role": "user",
        "content": "compare",
        "attachment_manifest": "[Attached files]\n- id: one\n- id: two",
    }]

    _images, attachment_text = await agent._ground_automatic_attachments(
        message_list=messages,
        images=None,
        attachment_text=None,
    )

    assert attachment_text.count("x") == 12
    assert "partial automatic read" in attachment_text
```

Also retain coverage that a current image already present in `images` is not read again:

```python
@pytest.mark.asyncio
async def test_ground_automatic_attachments_does_not_reopen_current_image(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})
    agent._model_id = "test-model"
    image = Image(id="image-id", content=b"image", format="png")

    async def fail_read(*args, **kwargs):
        raise AssertionError("current image was already loaded by the worker")

    monkeypatch.setattr(
        "src.agents.workflows.general.agent.read_chat_attachment",
        fail_read,
    )

    images, _text = await agent._ground_automatic_attachments(
        message_list=[{
            "role": "user",
            "content": "hello",
            "attachment_manifest": "[Attached files]\n- id: image-id",
        }],
        images=[image],
        attachment_text=None,
    )

    assert images == [image]
```

Import `chat_file_upload_config` from `src.infra.config.configs` in `tests/agents/test_general_agent_stream.py` for the budget override.


- [ ] **Step 5: Save the configured model ID and replace `_ground_pending_attachments`**

In `GeneralAgent.__init__`, after loading config:

```python
        self._model_id = config.model
```

Replace `_ground_pending_attachments` with:

```python
    async def _ground_automatic_attachments(
        self,
        *,
        message_list: Sequence[ChatMessage | dict[str, Any]],
        images: Sequence[Any] | None,
        attachment_text: str | None,
    ) -> tuple[list[Any] | None, str | None]:
        """Open current or one-turn-carried attachments under one run budget.

        Current images are loaded by the worker and identified by attachment ID;
        this method skips those IDs so native media is never downloaded twice.
        Document text is a bounded prefix, not proof that the complete file was read.
        """
        selected_ids = automatic_attachment_ids(
            message_list,
            max_items=chat_file_upload_config.ATTACHMENT_AUTO_READ_MAX_ITEMS,
        )
        grounded_images = list(images or [])
        already_loaded_ids = {
            str(image.id) for image in grounded_images if getattr(image, "id", None)
        }
        selected_ids = [item for item in selected_ids if item not in already_loaded_ids]
        if not selected_ids:
            return grounded_images or None, attachment_text

        remaining_tokens = chat_file_upload_config.ATTACHMENT_AUTO_READ_MAX_TOKENS
        text_parts: list[str] = [attachment_text] if attachment_text else []

        for index, attachment_id in enumerate(selected_ids):
            if remaining_tokens <= 0:
                break

            remaining_items = len(selected_ids) - index
            fair_token_share = max(remaining_tokens // remaining_items, 1)
            max_chars = min(
                chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS,
                fair_token_share * 4,
            )
            result = await read_chat_attachment(
                attachment_id,
                context=self.context,
                max_chars=max_chars,
            )

            if isinstance(result, AgnoToolResult):
                grounded_images.extend(result.images or [])
                if result.content:
                    text_parts.append(str(result.content))
                continue

            if not isinstance(result, dict):
                continue

            filename = result.get("filename") or attachment_id
            content = str(result.get("content") or "")
            if not content:
                continue

            content, token_truncated, used_tokens = truncate_to_token_budget(
                content,
                fair_token_share,
                self._model_id,
            )
            remaining_tokens -= used_tokens
            was_truncated = bool(result.get("truncated")) or token_truncated
            suffix = (
                "\n[Partial automatic read: more content remains in this attachment.]"
                if was_truncated
                else ""
            )
            content_type = result.get("content_type") or "text"
            text_parts.append(
                f"--- Grounded attachment ({content_type}): {filename} ---\n{content}{suffix}"
            )

        # copilotgtm: Automatic grounding intentionally reads only a bounded prefix.
        # Add chunk retrieval instead of raising this budget when full-document Q&A is required.
        logger.info(
            "GeneralAgent automatically grounded attachments: ids=%d images=%d text_tokens_used=%d",
            len(selected_ids),
            len(grounded_images),
            chat_file_upload_config.ATTACHMENT_AUTO_READ_MAX_TOKENS - remaining_tokens,
        )
        joined_text = "\n\n".join(text_parts)
        return grounded_images or None, joined_text or None
```

Update `run_stream` to call `_ground_automatic_attachments`. Import:

```python
from src.agents.utils import (
    automatic_attachment_ids,
    prepare_messages,
    truncate_to_token_budget,
)
from src.infra.config.configs import chat_file_upload_config
```

- [ ] **Step 6: Run the grounding tests**

```powershell
uv run pytest tests/agents/test_utils.py tests/agents/test_general_agent_stream.py tests/workers/chat_worker/services/test_message_processing_attachments.py -q
```

Expected: all tests pass, including current-image deduplication, one-turn carry, item cap, and shared token budget.

- [ ] **Step 7: Run the Python comment scanner on touched backend files**

```powershell
uv run python .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/agents/utils.py src/infra/media/chat_attachment_text.py src/agents/tools/communication/chat_attachments.py src/agents/workflows/general/agent.py src/workers/chat_worker/services/message_processing_service.py
```

Expected: no new comment/docstring smells in changed code. Existing unrelated findings must be reported rather than silently expanded into this change.

- [ ] **Step 8: Commit automatic grounding**

```powershell
git add src/agents/utils.py src/workers/chat_worker/services/message_processing_service.py src/agents/workflows/general/agent.py tests/agents/test_utils.py tests/agents/test_general_agent_stream.py tests/workers/chat_worker/services/test_message_processing_attachments.py
git commit -m "feat: auto-ground attachments within a token budget"
```

---

### Task 4: Full verification and observability check

**Files:**
- No new files.

- [ ] **Step 1: Run the complete focused attachment suite**

```powershell
uv run pytest tests/agents/test_utils.py tests/agents/test_general_agent_stream.py tests/agents/tools/communication/test_chat_attachments.py tests/infra/media/test_chat_attachment_text.py tests/workers/chat_worker/services/test_message_processing_attachments.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run Ruff on every changed Python file**

```powershell
uv run ruff check src/infra/config/configs.py src/agents/utils.py src/infra/media/chat_attachment_text.py src/agents/tools/communication/chat_attachments.py src/workers/chat_worker/services/message_processing_service.py src/agents/workflows/general/agent.py tests/agents/test_utils.py tests/infra/media/test_chat_attachment_text.py tests/agents/tools/communication/test_chat_attachments.py tests/workers/chat_worker/services/test_message_processing_attachments.py tests/agents/test_general_agent_stream.py
```

Expected: no lint errors.

- [ ] **Step 3: Verify the exact three-turn contracts**

Run the selector and grounding tests with `-v` and confirm these cases are named in output:

```powershell
uv run pytest tests/agents/test_utils.py tests/agents/test_general_agent_stream.py -k "automatic_attachment or ground_automatic" -v
```

Expected contracts:

```text
message 1 + A -> A selected
message 2 + B -> B selected, A ignored
message 3 without attachment -> B selected once
message 4 without attachment -> nothing selected
large document -> bounded prefix with partial-read marker
more than three attachments -> first three selected; others remain tool-readable
```

- [ ] **Step 4: Inspect the final diff for scope**

```powershell
git diff --check
git status --short
git diff --stat HEAD~3..HEAD
```

Expected: only the files listed in this plan changed; the pre-existing untracked `docs/Technical Documentation/chat_bots/unified-conversation-architecture.md` remains untouched.

## Acceptance criteria

- Current-turn images are still native vision input.
- Current-turn PDFs/documents are automatically read without a tool decision.
- If the new turn has no attachments, attachments from exactly the previous user turn are automatically read once.
- An attachment is never carried through two consecutive attachment-free user turns.
- No run automatically opens more than three attachments.
- All automatically injected document text together stays at or below 3,000 estimated model tokens.
- PDF/DOCX/PPTX/XLSX extraction stops at the configured character ceiling instead of building the entire extracted document first.
- Partial reads are explicitly labeled.
- Older content remains available through `read_chat_attachment`.
- No new dependency, table, migration, queue, or background job is introduced.
