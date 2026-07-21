"""Agent-model configuration contracts for built-in and user-configured runtimes."""

from src.infra.config.agent_config import AgentConfig


def test_agent_defaults_assign_gpt_5_6_tiers_by_workload() -> None:
    """Keep costly Sol work limited to complex or write-capable workflows."""
    assert {
        agent_name: AgentConfig.get_model(agent_name)[0] for agent_name in AgentConfig.list_agents()
    } == {
        "summary_agent": "gpt-5.6-terra",
        "briefing_agent": "gpt-5.6-terra",
        "deal_summary_agent": "gpt-5.6-sol",
        "risk_blocker_extraction_agent": "gpt-5.6-terra",
        "stakeholder_analysis_agent": "gpt-5.6-terra",
        "general_agent": "gpt-5.6-terra",
        "router_agent": "gpt-5.6-luna",
        "voice_realtime_agent": "gpt-realtime-2.1",
        "deal_memory_agent": "gpt-5.6-terra",
        "deal_task_agent": "gpt-5.6-sol",
        "deal_update_agent": "gpt-5.6-sol",
        "transcript_compression_agent": "gpt-5.6-luna",
        "contact_research_summary_agent": "gpt-5-mini",
        "account_research_summary_agent": "gpt-5-mini",
        "user_configured_agent": "gpt-5.6-terra",
    }


def test_user_configured_agent_has_a_dedicated_config_key() -> None:
    """Keep triggered user agents independently configurable from interactive chat."""

    config = AgentConfig.get_config("user_configured_agent")

    assert "user_configured_agent" in AgentConfig.list_agents()
    assert config.model == AgentConfig.USER_CONFIGURED_MODEL
    assert config.debug_mode == AgentConfig.USER_CONFIGURED_DEBUG
    assert config.timeout_seconds == AgentConfig.USER_CONFIGURED_TIMEOUT
