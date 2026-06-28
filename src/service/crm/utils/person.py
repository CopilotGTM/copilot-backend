from __future__ import annotations


def split_full_name(full_name: str) -> tuple[str, str | None]:
    """Split a display name into first name and optional remainder."""
    parts = full_name.strip().split(" ", 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else None
    return first, last
