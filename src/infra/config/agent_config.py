"""Agent configuration for AI agents across the application.

This module centralizes all agent-related configuration including model assignments,
API versions, and other agent-specific settings. Configuration can be overridden
via environment variables.

Usage:
    from src.infra.config.agent_config import agent_config
    # Prefer full config for runtime-safe initialization:
    # get_config exposes model, debug mode, and timeout as one immutable object.
    config = agent_config.get_config("deal_task_agent")

    # Existing tuple access is kept for call sites that only need model+version.
    model, api_version = agent_config.get_model("summary_agent")
    config = agent_config.get_config("deal_summary_agent")
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentModelConfig:
    """Configuration for an agent's LLM model.

    Attributes:
        model: Azure OpenAI deployment/model ID (e.g., "gpt-5", "gpt-5-mini")
        api_version: Azure OpenAI API version
        debug_mode: Whether to enable debug logging for agent
        timeout_seconds: Maximum time to wait for agent response
        retries: Number of retries before giving up
    """

    model: str
    api_version: str
    debug_mode: bool
    timeout_seconds: int
    retries: int = 2


class AgentConfig:
    """Configuration for AI agents.

    Model Guidelines:
    - gpt-5: Full capability model for complex analysis (deal summary, briefing, risk extraction)
    - gpt-5-mini: Faster/cheaper model for simpler tasks (meeting summary, stakeholder analysis)

    All settings can be overridden via environment variables:
    - AGENT_<NAME>_MODEL: Model ID (e.g., AGENT_SUMMARY_MODEL=gpt-5)
    - AGENT_<NAME>_DEBUG: Debug mode (e.g., AGENT_SUMMARY_DEBUG=true)
    - AGENT_<NAME>_TIMEOUT: Timeout in seconds (e.g., AGENT_SUMMARY_TIMEOUT=120)
    """

    # Default API version for all agents
    DEFAULT_API_VERSION: str = os.getenv("AGENT_DEFAULT_API_VERSION", "2024-12-01-preview")

    # ==========================================================================
    # MEETING-LEVEL AGENTS
    # ==========================================================================

    # Summary Agent - generates meeting summaries from transcripts
    SUMMARY_MODEL: str = os.getenv("AGENT_SUMMARY_MODEL", "gpt-5.2")
    SUMMARY_DEBUG: bool = os.getenv("AGENT_SUMMARY_DEBUG", "false").lower() == "true"
    SUMMARY_TIMEOUT: int = int(os.getenv("AGENT_SUMMARY_TIMEOUT", "600"))

    # Briefing Agent - generates meeting preparation briefings
    BRIEFING_MODEL: str = os.getenv("AGENT_BRIEFING_MODEL", "gpt-5.2")
    BRIEFING_DEBUG: bool = os.getenv("AGENT_BRIEFING_DEBUG", "false").lower() == "true"
    BRIEFING_TIMEOUT: int = int(os.getenv("AGENT_BRIEFING_TIMEOUT", "600"))

    # ==========================================================================
    # DEAL-LEVEL AGENTS
    # ==========================================================================

    # Deal Summary Agent - generates comprehensive deal summaries
    DEAL_SUMMARY_MODEL: str = os.getenv("AGENT_DEAL_SUMMARY_MODEL", "gpt-5.2")
    DEAL_SUMMARY_DEBUG: bool = os.getenv("AGENT_DEAL_SUMMARY_DEBUG", "false").lower() == "true"
    DEAL_SUMMARY_TIMEOUT: int = int(os.getenv("AGENT_DEAL_SUMMARY_TIMEOUT", "600"))

    # Risk Blocker Extraction Agent - extracts risks and blockers from deal communications
    RISK_BLOCKER_MODEL: str = os.getenv("AGENT_RISK_BLOCKER_MODEL", "gpt-5.2")
    RISK_BLOCKER_DEBUG: bool = os.getenv("AGENT_RISK_BLOCKER_DEBUG", "false").lower() == "true"
    RISK_BLOCKER_TIMEOUT: int = int(os.getenv("AGENT_RISK_BLOCKER_TIMEOUT", "600"))

    # Stakeholder Analysis Agent - analyzes stakeholder insights from transcripts
    STAKEHOLDER_MODEL: str = os.getenv("AGENT_STAKEHOLDER_MODEL", "gpt-5.4")
    STAKEHOLDER_DEBUG: bool = os.getenv("AGENT_STAKEHOLDER_DEBUG", "false").lower() == "true"
    STAKEHOLDER_TIMEOUT: int = int(os.getenv("AGENT_STAKEHOLDER_TIMEOUT", "600"))

    # Deal Memory Agent - builds/updates persistent deal memory documents
    DEAL_MEMORY_MODEL: str = os.getenv("AGENT_DEAL_MEMORY_MODEL", "gpt-5.4")
    DEAL_MEMORY_DEBUG: bool = os.getenv("AGENT_DEAL_MEMORY_DEBUG", "false").lower() == "true"
    DEAL_MEMORY_TIMEOUT: int = int(os.getenv("AGENT_DEAL_MEMORY_TIMEOUT", "600"))

    # Deal Task Agent - creates/completes deal tasks from meeting summaries
    DEAL_TASK_MODEL: str = os.getenv("AGENT_DEAL_TASK_MODEL", "gpt-5.4")
    DEAL_TASK_DEBUG: bool = os.getenv("AGENT_DEAL_TASK_DEBUG", "false").lower() == "true"
    DEAL_TASK_TIMEOUT: int = int(os.getenv("AGENT_DEAL_TASK_TIMEOUT", "600"))

    # Deal Update Agent - updates internal CRM deal state through scoped tools
    DEAL_UPDATE_MODEL: str = os.getenv("AGENT_DEAL_UPDATE_MODEL", "gpt-5.2")
    DEAL_UPDATE_DEBUG: bool = os.getenv("AGENT_DEAL_UPDATE_DEBUG", "false").lower() == "true"
    DEAL_UPDATE_TIMEOUT: int = int(os.getenv("AGENT_DEAL_UPDATE_TIMEOUT", "600"))

    # Stakeholder Research Agent — researches individual stakeholders via Apollo + LinkedIn

    # Contact Research Summary Agent — turns provider enrichment into short UI copy
    CONTACT_RESEARCH_SUMMARY_MODEL: str = os.getenv(
        "AGENT_CONTACT_RESEARCH_SUMMARY_MODEL", "gpt-5-mini"
    )
    CONTACT_RESEARCH_SUMMARY_DEBUG: bool = (
        os.getenv("AGENT_CONTACT_RESEARCH_SUMMARY_DEBUG", "false").lower() == "true"
    )
    CONTACT_RESEARCH_SUMMARY_TIMEOUT: int = int(
        os.getenv("AGENT_CONTACT_RESEARCH_SUMMARY_TIMEOUT", "60")
    )

    # Account Research Summary Agent — turns company enrichment into short UI copy
    ACCOUNT_RESEARCH_SUMMARY_MODEL: str = os.getenv(
        "AGENT_ACCOUNT_RESEARCH_SUMMARY_MODEL", "gpt-5-mini"
    )
    ACCOUNT_RESEARCH_SUMMARY_DEBUG: bool = (
        os.getenv("AGENT_ACCOUNT_RESEARCH_SUMMARY_DEBUG", "false").lower() == "true"
    )
    ACCOUNT_RESEARCH_SUMMARY_TIMEOUT: int = int(
        os.getenv("AGENT_ACCOUNT_RESEARCH_SUMMARY_TIMEOUT", "60")
    )

    # ==========================================================================
    # SUB-AGENTS
    # ==========================================================================

    # Transcript Compression Sub-Agent - condenses transcripts for deal memory
    TRANSCRIPT_COMPRESSION_MODEL: str = os.getenv(
        "AGENT_TRANSCRIPT_COMPRESSION_MODEL", "gpt-5-mini"
    )
    TRANSCRIPT_COMPRESSION_DEBUG: bool = (
        os.getenv("AGENT_TRANSCRIPT_COMPRESSION_DEBUG", "false").lower() == "true"
    )
    TRANSCRIPT_COMPRESSION_TIMEOUT: int = int(
        os.getenv("AGENT_TRANSCRIPT_COMPRESSION_TIMEOUT", "300")
    )

    # ==========================================================================
    # CHAT AGENTS
    # ==========================================================================

    # General Agent - handles user queries with search and deal context tools
    GENERAL_MODEL: str = os.getenv("AGENT_GENERAL_MODEL", "gpt-5.4")
    GENERAL_DEBUG: bool = os.getenv("AGENT_GENERAL_DEBUG", "false").lower() == "true"
    GENERAL_TIMEOUT: int = int(os.getenv("AGENT_GENERAL_TIMEOUT", "600"))

    # Router Agent - routes queries to appropriate agents
    ROUTER_MODEL: str = os.getenv("AGENT_ROUTER_MODEL", "gpt-5.2")
    ROUTER_DEBUG: bool = os.getenv("AGENT_ROUTER_DEBUG", "false").lower() == "true"
    ROUTER_TIMEOUT: int = int(os.getenv("AGENT_ROUTER_TIMEOUT", "30"))

    # Voice Realtime Agent - tool-free conversational agent for live voice sessions
    VOICE_REALTIME_MODEL: str = os.getenv("AGENT_VOICE_REALTIME_MODEL", "gpt-realtime-1.5")
    VOICE_REALTIME_VOICE: str = os.getenv("AGENT_VOICE_REALTIME_VOICE", "alloy")
    VOICE_REALTIME_DEBUG: bool = os.getenv("AGENT_VOICE_REALTIME_DEBUG", "false").lower() == "true"
    VOICE_REALTIME_TIMEOUT: int = int(os.getenv("AGENT_VOICE_REALTIME_TIMEOUT", "120"))

    # ==========================================================================
    # HELPER METHODS
    # ==========================================================================

    _CONFIG_MAP: dict[str, tuple[str, str, str]] = {
        # agent_name: (model_attr, debug_attr, timeout_attr)
        "summary_agent": ("SUMMARY_MODEL", "SUMMARY_DEBUG", "SUMMARY_TIMEOUT"),
        "briefing_agent": ("BRIEFING_MODEL", "BRIEFING_DEBUG", "BRIEFING_TIMEOUT"),
        "deal_summary_agent": ("DEAL_SUMMARY_MODEL", "DEAL_SUMMARY_DEBUG", "DEAL_SUMMARY_TIMEOUT"),
        "risk_blocker_extraction_agent": (
            "RISK_BLOCKER_MODEL",
            "RISK_BLOCKER_DEBUG",
            "RISK_BLOCKER_TIMEOUT",
        ),
        "stakeholder_analysis_agent": (
            "STAKEHOLDER_MODEL",
            "STAKEHOLDER_DEBUG",
            "STAKEHOLDER_TIMEOUT",
        ),
        "general_agent": ("GENERAL_MODEL", "GENERAL_DEBUG", "GENERAL_TIMEOUT"),
        "router_agent": ("ROUTER_MODEL", "ROUTER_DEBUG", "ROUTER_TIMEOUT"),
        "voice_realtime_agent": (
            "VOICE_REALTIME_MODEL",
            "VOICE_REALTIME_DEBUG",
            "VOICE_REALTIME_TIMEOUT",
        ),
        "deal_memory_agent": (
            "DEAL_MEMORY_MODEL",
            "DEAL_MEMORY_DEBUG",
            "DEAL_MEMORY_TIMEOUT",
        ),
        "deal_task_agent": (
            "DEAL_TASK_MODEL",
            "DEAL_TASK_DEBUG",
            "DEAL_TASK_TIMEOUT",
        ),
        "deal_update_agent": (
            "DEAL_UPDATE_MODEL",
            "DEAL_UPDATE_DEBUG",
            "DEAL_UPDATE_TIMEOUT",
        ),
        "transcript_compression_agent": (
            "TRANSCRIPT_COMPRESSION_MODEL",
            "TRANSCRIPT_COMPRESSION_DEBUG",
            "TRANSCRIPT_COMPRESSION_TIMEOUT",
        ),
        "contact_research_summary_agent": (
            "CONTACT_RESEARCH_SUMMARY_MODEL",
            "CONTACT_RESEARCH_SUMMARY_DEBUG",
            "CONTACT_RESEARCH_SUMMARY_TIMEOUT",
        ),
        "account_research_summary_agent": (
            "ACCOUNT_RESEARCH_SUMMARY_MODEL",
            "ACCOUNT_RESEARCH_SUMMARY_DEBUG",
            "ACCOUNT_RESEARCH_SUMMARY_TIMEOUT",
        ),
    }

    @classmethod
    def get_model(cls, agent_name: str) -> tuple[str, str]:
        """Get model ID and API version for an agent.

        Args:
            agent_name: Name of the agent (e.g., "summary_agent", "deal_summary_agent")

        Returns:
            Tuple of (model_id, api_version)

        Raises:
            KeyError: If agent_name is not found
        """
        if agent_name not in cls._CONFIG_MAP:
            raise KeyError(
                f"Unknown agent: {agent_name}. Available: {list(cls._CONFIG_MAP.keys())}"
            )

        model_attr = cls._CONFIG_MAP[agent_name][0]
        return getattr(cls, model_attr), cls.DEFAULT_API_VERSION

    @classmethod
    def get_config(cls, agent_name: str) -> AgentModelConfig:
        """Get full configuration for an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            AgentModelConfig with all settings

        Raises:
            KeyError: If agent_name is not found
        """
        if agent_name not in cls._CONFIG_MAP:
            raise KeyError(
                f"Unknown agent: {agent_name}. Available: {list(cls._CONFIG_MAP.keys())}"
            )

        model_attr, debug_attr, timeout_attr = cls._CONFIG_MAP[agent_name]

        return AgentModelConfig(
            model=getattr(cls, model_attr),
            api_version=cls.DEFAULT_API_VERSION,
            debug_mode=getattr(cls, debug_attr),
            timeout_seconds=getattr(cls, timeout_attr),
        )

    @classmethod
    def get_timeout(cls, agent_name: str) -> int:
        """Get timeout in seconds for an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            Timeout in seconds (defaults to 120 if not configured)
        """
        if agent_name not in cls._CONFIG_MAP:
            return 120

        timeout_attr = cls._CONFIG_MAP[agent_name][2]
        return getattr(cls, timeout_attr)

    @classmethod
    def list_agents(cls) -> list[str]:
        """List all configured agent names.

        Returns:
            List of agent names
        """
        return list(cls._CONFIG_MAP.keys())


# Single shared instance avoids re-reading env config at each callsite.
agent_config = AgentConfig()
