from src.service.agentic_approvals.action_approval_defaults import (
    ACTION_APPROVAL_DEFAULTS,
    LOCKED_APPROVAL_TOOLS,
    get_action_approval_default,
)
from src.service.agentic_approvals.action_approval_policy import resolve_action_approval_decision
from src.service.agentic_approvals.action_approval_service import (
    AGENT_ACTION_APPROVALS_KEY,
    AgentActionApprovalService,
)


def test_default_policy_contains_expected_tools() -> None:
    assert ACTION_APPROVAL_DEFAULTS["create_task"].approval_required is False
    assert ACTION_APPROVAL_DEFAULTS["add_deal_note"].approval_required is False
    assert ACTION_APPROVAL_DEFAULTS["create_deal"].approval_required is True
    assert ACTION_APPROVAL_DEFAULTS["add_stakeholder"].approval_required is True
    assert ACTION_APPROVAL_DEFAULTS["send_calendar_invite"].approval_required is True
    assert ACTION_APPROVAL_DEFAULTS["send_email"].approval_required is True
    assert ACTION_APPROVAL_DEFAULTS["save_email_draft"].approval_required is False
    assert ACTION_APPROVAL_DEFAULTS["send_email_draft"].approval_required is True
    assert ACTION_APPROVAL_DEFAULTS["update_calendar_meeting"].approval_required is True
    assert ACTION_APPROVAL_DEFAULTS["cancel_calendar_meeting"].approval_required is True


def test_send_email_is_locked() -> None:
    default = get_action_approval_default("send_email")

    assert default.editable is False
    assert "always approval-gated" in default.locked_reason
    assert "send_email" in LOCKED_APPROVAL_TOOLS
    assert "send_email_draft" in LOCKED_APPROVAL_TOOLS
    assert "cancel_calendar_meeting" in LOCKED_APPROVAL_TOOLS


def test_effective_preferences_use_defaults_for_empty_payload() -> None:
    rows = AgentActionApprovalService.build_effective_preferences({})

    by_tool = {row["tool_name"]: row for row in rows}
    assert by_tool["create_task"]["approval_required"] is False
    assert by_tool["create_deal"]["approval_required"] is True
    assert by_tool["send_email"]["editable"] is False


def test_merge_preserves_unrelated_preferences() -> None:
    existing = {"dashboard": {"sales": {"hidden_widget_ids": ["x"]}}}

    merged = AgentActionApprovalService.merge_preferences(
        existing,
        {"create_deal": False, "send_email": False},
    )

    assert merged["dashboard"]["sales"]["hidden_widget_ids"] == ["x"]
    tools = merged[AGENT_ACTION_APPROVALS_KEY]["tools"]
    assert tools["create_deal"]["approval_required"] is False
    assert tools["send_email"]["approval_required"] is True


def test_action_approval_decision_respects_user_preference() -> None:
    preferences = AgentActionApprovalService.merge_preferences({}, {"create_deal": False})

    decision = resolve_action_approval_decision(
        tool_name="create_deal",
        user_preferences=preferences,
        organisation_preferences={},
    )

    assert decision.allowed is True
    assert decision.approval_required is False
    assert decision.allowed_direct is True


def test_action_approval_decision_blocks_disabled_org_write_scope() -> None:
    decision = resolve_action_approval_decision(
        tool_name="create_deal",
        user_preferences=AgentActionApprovalService.merge_preferences({}, {"create_deal": False}),
        organisation_preferences={
            "crm": {
                "provider": "copilotgtm",
                "write_policy": {"allowed_scopes": []},
            }
        },
    )

    assert decision.allowed is False
    assert decision.approval_required is True
    assert decision.org_policy_denied is True
    assert decision.code == "crm_write_scope_disabled"


def test_action_approval_decision_blocks_unknown_tools() -> None:
    decision = resolve_action_approval_decision(
        tool_name="unknown_tool",
        user_preferences={},
        organisation_preferences={},
    )

    assert decision.allowed is False
    assert decision.approval_required is True
    assert decision.unknown_tool is True
    assert decision.code == "unknown_action_tool"


def test_action_approval_decision_keeps_send_email_locked() -> None:
    decision = resolve_action_approval_decision(
        tool_name="send_email",
        user_preferences=AgentActionApprovalService.merge_preferences({}, {"send_email": False}),
        organisation_preferences={},
    )

    assert decision.allowed is True
    assert decision.approval_required is True
    assert decision.allowed_direct is False
    assert decision.locked is True
