"""Small helpers for DB queries that bind large ID lists."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")

LOCAL_ID_QUERY_CHUNK_SIZE = 10_000
EXTERNAL_ID_QUERY_CHUNK_SIZE = 10_000


def iter_chunks(values: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
