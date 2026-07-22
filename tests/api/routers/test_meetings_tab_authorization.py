from inspect import signature

from src.api.routers.meetings_tab_handler import invite_bot, remove_bot


def test_kai_invite_is_not_gated_by_internal_crm_write_policy() -> None:
    """External-CRM organisations must still be able to manage their meeting bot."""
    assert "_crm_write_guard" not in signature(invite_bot).parameters


def test_kai_remove_is_not_gated_by_internal_crm_write_policy() -> None:
    """Removing Kai remains available independently of CRM provider selection."""
    assert "_crm_write_guard" not in signature(remove_bot).parameters
