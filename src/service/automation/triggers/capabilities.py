"""Capabilities that are unsafe to replay as part of a complete agent run."""

from src.agents.tools.registry import MUTATING_TOOLS

RETRY_UNSAFE_AUTOMATION_TOOLS = frozenset(item.value for item in MUTATING_TOOLS)

__all__ = ["RETRY_UNSAFE_AUTOMATION_TOOLS"]
