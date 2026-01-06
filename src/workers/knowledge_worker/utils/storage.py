from __future__ import annotations

import tempfile
from pathlib import Path

from src.infra.utils.async_runner import run_async


def download_blob_to_temp(storage, source) -> str:
    """Download a blob to a temporary file and return the file path.

    Uses a persistent thread-local event loop via run_async() to prevent
    memory leaks from creating new event loops per task.

    - `storage` must provide `get_blob_content(path)` coroutine.
    - `source` must provide `filename` and `storage_path` attributes.
    """
    suffix = Path(getattr(source, "filename", "document") or "document").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        file_content = run_async(storage.get_blob_content(source.storage_path))
        temp_file.write(file_content)
        temp_file.flush()
        return temp_file.name
