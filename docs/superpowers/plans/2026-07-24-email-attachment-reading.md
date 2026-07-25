# Email Attachment Reading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agents discover every meaningful attachment on an authorized email or thread and read one exact attachment at a time without silently truncating its content.

**Architecture:** Keep `get_email_details` as the message/thread and attachment-manifest tool. Add one `read_email_attachment` tool backed by a focused service that reuses the existing Gmail/Outlook attachment downloaders and shared file parsers. Authorization remains the existing owned-mailbox or visible-deal boundary, and each read validates that the requested attachment belongs to the authorized message before downloading it.

**Tech Stack:** Python, SQLAlchemy/SQLModel, Agno tools, Gmail API, Microsoft Graph, httpx, pytest.

---

### Task 1: Lock the manifest contract

**Files:**
- Modify: `tests/agents/tools/communication/test_get_email_details.py`
- Modify: `src/agents/tools/communication/get_email_details.py`

- [x] **Step 1: Write failing formatting tests**

Add tests proving each attachment includes `attachment_id`,
`internet_message_id`, `filename`, `mime_type`, `size_bytes`, and `readable`.
Add a thread test proving the selected message is repeated but unique attachment
instances are identified by `(internet_message_id, attachment_id)`.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/agents/tools/communication/test_get_email_details.py -q
```

Expected: the new assertions fail because current output omits attachment IDs
and owning message IDs.

- [x] **Step 3: Implement the minimal manifest formatter**

Normalize existing attachment metadata keys without downloading content:

```python
{
    "attachment_id": attachment.get("attachment_id"),
    "internet_message_id": email.internet_message_id,
    "filename": attachment.get("filename") or attachment.get("name"),
    "mime_type": attachment.get("mime_type"),
    "size_bytes": attachment.get("size_bytes", attachment.get("size")),
    "readable": bool(attachment.get("attachment_id")),
}
```

- [x] **Step 4: Run the focused tests and verify GREEN**

Run the Task 1 pytest command and require zero failures.

### Task 2: Add an authorized provider attachment service

**Files:**
- Create: `src/agents/tools/communication/email_attachments.py`
- Create: `tests/agents/tools/communication/test_email_attachments.py`

- [x] **Step 1: Write failing service tests**

Cover:

- mailbox and deal scope are mutually exclusive;
- authorization failure returns `email_not_found`;
- attachment IDs are validated against the selected provider message;
- one call downloads one attachment;
- unsupported MIME types return `unsupported_attachment_type`;
- extracted text returns `offset`, `returned_characters`, `truncated`, and
  `next_offset`;
- a second offset returns the next non-overlapping chunk.

- [x] **Step 2: Run the new test module and verify RED**

Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/agents/tools/communication/test_email_attachments.py -q
```

Expected: import failure because the new reader does not exist.

- [x] **Step 3: Implement the minimum reader**

The module will:

1. call `get_email_details(..., include_body=False)` to enforce the existing
   mailbox/deal record boundary;
2. load the exact org-scoped `Email` and `KnowledgeIntegration` needed for the
   provider call, then release the database session;
3. list provider attachments using `list_gmail_attachments` or
   `list_outlook_attachments`;
4. validate `attachment_id` against that list;
5. download with `download_gmail_attachment` or
   `download_outlook_attachment`;
6. extract supported text using `extract_text_from_bytes`;
7. return exactly one bounded chunk and an explicit continuation cursor.

Image bytes will use the same neutral `ToolResult` image adapter pattern as
`read_chat_attachment`.

- [x] **Step 4: Run the new tests and verify GREEN**

Run the Task 2 pytest command and require zero failures.

### Task 3: Hydrate manifests and register the tool

**Files:**
- Modify: `src/agents/tools/communication/get_email_details.py`
- Modify: `src/agents/tools/communication/__init__.py`
- Modify: `src/agents/tools/registry.py`
- Modify: `tests/service/agents/test_channel_tool_registry.py`
- Modify: `tests/agents/tools/communication/test_get_email_details.py`

- [x] **Step 1: Write failing registry and manifest-hydration tests**

Prove `GeneralAgent` receives `READ_EMAIL_ATTACHMENT` and that
`get_email_details` can hydrate attachment metadata for each returned message
without flattening thread ownership.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/agents/tools/communication/test_get_email_details.py tests/service/agents/test_channel_tool_registry.py -q
```

Expected: failures because the enum/factory and hydration path do not exist.

- [x] **Step 3: Add registry wiring and bounded hydration**

Add `READ_EMAIL_ATTACHMENT` beside the existing email-read tools, export its
factory, register it for the General Agent, and make `get_email_details`
populate manifests for the selected message and each returned thread message.
Provider I/O must run after the database session is released.

- [x] **Step 4: Run focused tests and verify GREEN**

Run the Task 3 pytest command and require zero failures.

### Task 4: Verify the complete attachment flow

**Files:**
- Check all modified files

- [x] **Step 1: Run the complete focused suite**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/agents/tools/communication/test_get_email_details.py tests/agents/tools/communication/test_email_attachments.py tests/workers/integrations_worker/providers/email/test_attachment_downloader.py tests/service/agents/test_channel_tool_registry.py -q
```

- [x] **Step 2: Run lint/type-adjacent checks on touched files**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/agents/tools/communication/email_attachments.py src/agents/tools/communication/get_email_details.py tests/agents/tools/communication/test_email_attachments.py tests/agents/tools/communication/test_get_email_details.py
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/agents/tools/communication/email_attachments.py src/agents/tools/communication/get_email_details.py
git diff --check
```

- [x] **Step 3: Audit the agreed rules**

Confirm from tests and code that:

- one tool call reads one attachment;
- `include_thread=False` exposes only the selected message manifest;
- `include_thread=True` preserves per-message ownership;
- `(internet_message_id, attachment_id)` is the unique instance key;
- unauthorized or unsupported reads are explicit;
- truncation always supplies `next_offset`;
- no unrelated worktree files changed.
