"""Versioned user-agent definition and capability routes."""

from .capabilities_handler import agent_capabilities_router
from .definitions_handler import agent_definitions_router
from .templates_handler import agent_templates_router

__all__ = [
    "agent_capabilities_router",
    "agent_definitions_router",
    "agent_templates_router",
]
