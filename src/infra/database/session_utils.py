from __future__ import annotations

from typing import Any, Type, TypeVar, Optional

from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

T = TypeVar("T")


def is_async(session: Any) -> bool:
    return isinstance(session, SAAsyncSession)


async def flush(session: Any) -> None:
    if is_async(session):
        await session.flush()
    else:
        session.flush()


async def refresh(session: Any, obj: Any) -> None:
    if is_async(session):
        await session.refresh(obj)
    else:
        session.refresh(obj)


async def rollback(session: Any) -> None:
    if is_async(session):
        await session.rollback()
    else:
        session.rollback()


async def get(session: Any, model: Type[T], ident: Any) -> Optional[T]:
    if is_async(session):
        return await session.get(model, ident)
    return session.get(model, ident)


async def commit(session: Any) -> None:
    if is_async(session):
        await session.commit()
    else:
        session.commit()

