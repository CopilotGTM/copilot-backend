"""Bound request bodies before public webhook handlers parse untrusted data."""

from __future__ import annotations

from fastapi import HTTPException, Request, status


async def read_bounded_request_body(
    request: Request,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    """Stream a request body and reject declared or actual sizes above the cap.

    Checking both values matters because ``Content-Length`` is caller-controlled
    and chunked requests omit it. Streaming avoids allocating the entire body
    before the application can enforce its public-endpoint limit.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            ) from exc
        if declared_size < 0 or declared_size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"{label} body is too large",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"{label} body is too large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["read_bounded_request_body"]
