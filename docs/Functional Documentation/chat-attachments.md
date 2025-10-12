Chat Attachments: Upload, Status & Download (Business)

Audience: Business users and PMs

What this feature does
- Add files to a chat as attachments (images, PDFs, docs).
- Uploads go directly from your browser to secure cloud storage.
- See live processing status (uploaded, processing, completed, failed).
- Download the original file with short-lived, secure links.

Typical workflow (quick overview)
1) Select files to upload
   - The app requests secure, time-limited upload links for each file.
2) Your browser uploads directly to storage
   - Faster and more reliable for large files (the server doesn't proxy the bytes).
3) Finalize to start processing
   - After each file finishes, the app tells the server to begin processing (checksum and thumbnails for images/PDFs).
4) Watch live status
   - The page streams real-time updates (no refresh needed). Completed files become available to download.

Flow (high level)
```mermaid
flowchart LR
  A[Select files] --> B[Server issues secure upload links]
  B --> C[Browser uploads directly to cloud]
  C --> D[Finalize: tell server upload done]
  D --> E[Background processing]
  E --> F[Live status stream]
  F --> G[Completed: ready to download]
```

What you'll see
- Status badges: pending_upload, uploaded, processing, completed, failed.
- Thumbnails for images/PDFs (when available) to quickly recognize files.
- Download button that generates a short-lived link on demand.

Limits and rules
- File types: Controlled by environment; defaults include common office types, images, and PDFs.
- File size: Enforced by environment limit (e.g., up to 50MB by default).
- Link expiry: Upload links last ~15 minutes; download links typically ~30-120 minutes.
- Security: Every action is limited to your organisation and your own uploads. Links expire automatically.

Errors and recovery
- Partial success: In batch uploads, one failure doesn't block others.
- Finalize per file: Each file finalizes independently and reports its own result.
- If you close the tab: Processing continues; come back to see updated status or regenerate a download link.

FAQ
- Can my teammates see my attachments? Access follows your organisation's policies; server-side checks restrict access by organisation and user.
- Do links expire? Yes. Upload and download links are short-lived for security.
- Do I have to wait on the page? No. Processing runs in the background; the page streams updates when open.

Related docs
- API details: docs/API Documentation/chat-attachments-api.md
- Chat messaging (using attachments in messages): docs/API Documentation/chat-api.md

