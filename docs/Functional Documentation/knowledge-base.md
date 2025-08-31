Knowledge Base: Uploads, Status & Access (Business)

Audience: Business users and PMs

What this feature does
- Upload documents to your organisation’s knowledge base.
- See real‑time status as files are processed (e.g., uploaded, processing, completed, failed).
- Browse your uploads with filters and quick stats; generate secure download links.
- Request deletion of a document; cleanup runs in the background.

Key concepts
- Source: One uploaded document tracked end‑to‑end (filename, size, status, error, thumbnails).
- Status lifecycle: pending_upload → uploaded → processing/extracting → completed (or failed).
- Secure links: The app issues short‑lived links for upload/download — safe to share inside your org, and they expire automatically.

Typical workflow (5‑minute overview)
1) Select files to upload
   - The app asks the server for secure upload links (one per file). Partial errors do not block the rest.
2) Your browser uploads directly to cloud storage
   - This keeps uploads fast and reliable — the server doesn’t proxy large files.
3) Finalize to start processing
   - After each file finishes uploading, the app tells the server to begin processing. Background jobs compute checksums, generate thumbnails, and extract text.
4) Watch live progress
   - The page streams real‑time updates (Server‑Sent Events). You’ll see statuses update without refreshing.
5) Use it
   - Open the list to filter, search by filename, view stats, download originals, or request deletion.

Flow (high level)
```mermaid
flowchart LR
  A[You select files] --> B[Server issues secure upload links]
  B --> C[Browser uploads directly to cloud]
  C --> D[Finalize: tell server upload done]
  D --> E[Background processing starts]
  E --> F[Live status stream to your screen]
  F --> G[Completed: ready to download/use]
```

What you’ll see in the UI
- Status badges: pending_upload, uploaded, processing/extracting, completed, failed.
- Thumbnails (when available) for quick recognition.
- Filters: by status, by type (file/integration), and quick search by filename.
- Stats: total documents, counts by status and by type.

Limits and rules
- File types: Configured by your environment (defaults include PDF and text).
- File size: Enforced by environment limits (default up to 100MB for knowledge uploads).
- Batch size: Up to 50 files per batch request.
- Security: All actions are scoped to your organisation and your user. Links expire automatically.

Errors and recovery
- Partial success: If some files fail to get an upload link, others still proceed.
- Finalize per file: Each file reports success or an error reason; failures don’t block other files.
- Deletion: A delete request is accepted and processed asynchronously; once complete, the file disappears from lists.

FAQ
- Can others in my org see my uploads? Your organisation’s permissions apply. This service strictly limits access to your org and your user by default.
- Do links expire? Yes. Upload and download links are short‑lived for safety.
- What happens if I close my browser? Reopen the page — processing and deletion continue on the server; status resumes from where it left off.

