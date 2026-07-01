from __future__ import annotations

from typing import Any


def serialize_external_links(external_links: list | None) -> list[dict] | None:
    """Serialize external links to JSON-friendly dicts."""
    if external_links is None:
        return None

    serialized: list[dict] = []
    for link in external_links:
        if isinstance(link, dict):
            serialized.append(link)
            continue

        added_at = getattr(link, "added_at", None)
        serialized.append(
            {
                "platform": link.platform,
                "url": link.url,
                "label": link.label,
                "added_at": added_at.isoformat() if added_at else None,
            }
        )

    return serialized


def extract_linkedin_url(external_links: list[dict[str, Any]] | None) -> str | None:
    """Keep LinkedIn first-class without dropping the full external link list."""
    for link in external_links or []:
        platform = str(link.get("platform") or link.get("type") or "").lower()
        url = link.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        if platform == "linkedin" or "linkedin." in url.lower():
            return url.strip()
    return None
