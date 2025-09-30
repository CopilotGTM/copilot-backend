from __future__ import annotations

import tempfile
from pathlib import Path


def download_blob_to_temp(loop, storage, source) -> str:
    """Download a blob to a temporary file and return the file path.

    - `storage` must provide `get_blob_content(path)` coroutine.
    - `source` must provide `filename` and `storage_path` attributes.
    """
    suffix = Path(getattr(source, "filename", "document") or "document").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        file_content = loop.run_until_complete(
            storage.get_blob_content(source.storage_path)
        )
        temp_file.write(file_content)
        temp_file.flush()
        return temp_file.name

