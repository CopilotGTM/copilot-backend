from __future__ import annotations

from typing import Optional


def plan_image_path_for_source(
    *,
    user_id: str,
    source_id: Optional[str],
    source_blob_path: Optional[str],
    page_number: Optional[int],
    index: int,
) -> str:
    """Plan a deterministic blob path for an extracted image.

    If the original document blob path is available, place images in a sibling
    `images/` folder next to it. Otherwise, fall back to a knowledgebase path
    grouped by user and source id.
    """
    page_part = page_number if page_number is not None else "u"
    sid = source_id or "unknown"

    if source_blob_path and "/" in source_blob_path:
        base_dir = source_blob_path.rsplit("/", 1)[0]
        return f"{base_dir}/images/p{page_part}_{index}.png"

    return f"knowledgebase/{user_id}/{sid}/images/p{page_part}_{index}.png"
