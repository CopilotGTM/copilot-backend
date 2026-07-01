from __future__ import annotations

from src.service.bots.form_handlers import FormHandlerRegistry


def test_stakeholder_form_actions_are_not_registered() -> None:
    assert "add_stakeholder_submit" not in FormHandlerRegistry._handlers
    assert "add_stakeholder_cancel" not in FormHandlerRegistry._handlers
