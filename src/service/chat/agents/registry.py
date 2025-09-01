from __future__ import annotations

from typing import Type

from src.domain.models.organisation_tables.chat_session_models import ChatType
from .base import Agent
from .general.agent import GeneralAgent


def resolve_agent(chat_type: ChatType | str) -> Type[Agent]:
    if isinstance(chat_type, str):
        try:
            chat_type = ChatType(chat_type)
        except Exception:
            chat_type = ChatType.GENERAL_CHAT

    match chat_type:
        case ChatType.GENERAL_CHAT:
            return GeneralAgent
        case _:
            # default fallback
            return GeneralAgent
