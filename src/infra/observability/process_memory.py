from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_STATM_PATH = Path("/proc/self/statm")
_BYTES_PER_MB = 1024 * 1024


def get_current_rss_bytes(statm_path: str | Path = _DEFAULT_STATM_PATH) -> int | None:
    try:
        fields = Path(statm_path).read_text(encoding="utf-8").split()
        rss_pages = int(fields[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))  # type: ignore[attr-defined]
    except Exception:
        return None

    if rss_pages < 0 or page_size <= 0:
        return None
    return rss_pages * page_size


def get_current_rss_mb(statm_path: str | Path = _DEFAULT_STATM_PATH) -> float | None:
    rss_bytes = get_current_rss_bytes(statm_path)
    if rss_bytes is None:
        return None
    return rss_bytes / _BYTES_PER_MB


__all__ = ["get_current_rss_bytes", "get_current_rss_mb"]
