from __future__ import annotations

from typing import Optional


def plan_image_path_for_source(
    *,
    user_id: str,
    source_id: Optional[str],
    source_blob_path: Optional[str],
    page_no: Optional[int],
    idx: str,
) -> str:
    """Plan a deterministic blob path for an extracted image.

    If the original document blob path is available, place images in a sibling
    `images/` folder next to it. Otherwise, fall back to a knowledgebase path
    grouped by user and source id.
    
    Args:
        user_id: User ID
        source_id: Source ID  
        source_blob_path: Original document blob path
        page_no: Page number (optional)
        idx: Image index/identifier (e.g., "chunk_1_img_0", "table_0")
    
    Returns:
        Blob path for the image
    """
    page_part = page_no if page_no is not None else "u"
    sid = source_id or "unknown"

    if source_blob_path and "/" in source_blob_path:
        base_dir = source_blob_path.rsplit("/", 1)[0]
        return f"{base_dir}/images/p{page_part}_{idx}.png"

    return f"knowledgebase/{user_id}/{sid}/images/p{page_part}_{idx}.png"


def plan_table_image_path_for_source(
    *,
    user_id: str,
    source_id: Optional[str],
    source_blob_path: Optional[str],
    page_no: Optional[int],
    table_idx: int,
) -> str:
    """Plan a deterministic blob path for an extracted table image.
    
    Tables get stored in a separate 'tables/' folder to organize content by type.
    
    Args:
        user_id: User ID
        source_id: Source ID
        source_blob_path: Original document blob path
        page_no: Page number where table is located
        table_idx: Table index within the chunk/document
        
    Returns:
        Blob path for the table image
    """
    page_part = page_no if page_no is not None else "u"
    sid = source_id or "unknown"

    if source_blob_path and "/" in source_blob_path:
        base_dir = source_blob_path.rsplit("/", 1)[0]
        return f"{base_dir}/tables/p{page_part}_table_{table_idx}.png"

    return f"knowledgebase/{user_id}/{sid}/tables/p{page_part}_table_{table_idx}.png"
