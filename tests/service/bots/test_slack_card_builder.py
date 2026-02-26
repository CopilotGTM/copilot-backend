"""Tests for Slack form card builder behavior."""

from __future__ import annotations

import json

from src.adapters.card_builders import FormDefinition, FormField
from src.adapters.card_builders.slack_card_builder import SlackFormCardBuilder


def _get_actions_block(payload: dict) -> dict:
    for block in payload.get("blocks", []):
        if block.get("type") == "actions":
            return block
    raise AssertionError("actions block not found")


def _get_input_block(payload: dict, block_id: str) -> dict:
    for block in payload.get("blocks", []):
        if block.get("block_id") == block_id:
            return block
    raise AssertionError(f"input block not found: {block_id}")


def test_form_builder_embeds_metadata_in_button_value() -> None:
    metadata = {"deal_id": "abc-123", "organisation_id": "org-123", "nonce": "nonce-1"}
    form = FormDefinition(
        title="Add Stakeholder",
        fields=[FormField(id="email", label="Email", field_type="text", required=True)],
        submit_action="add_stakeholder_submit",
        cancel_action="add_stakeholder_cancel",
        metadata=metadata,
    )

    payload = SlackFormCardBuilder().build(form)
    actions = _get_actions_block(payload)
    assert len(actions["elements"]) == 2

    submit_button = actions["elements"][0]
    cancel_button = actions["elements"][1]

    assert submit_button["action_id"] == "add_stakeholder_submit"
    assert cancel_button["action_id"] == "add_stakeholder_cancel"
    assert json.loads(submit_button["value"]) == metadata
    assert json.loads(cancel_button["value"]) == metadata


def test_form_builder_sets_initial_field_values() -> None:
    form = FormDefinition(
        title="Create Deal",
        fields=[
            FormField(id="name", label="Deal Name", field_type="text", value="Acme Expansion"),
            FormField(
                id="stage",
                label="Stage",
                field_type="choice",
                choices=[
                    {"title": "Qualification", "value": "QUAL"},
                    {"title": "Proposal", "value": "PROP"},
                ],
                value="PROP",
            ),
            FormField(id="close_date", label="Close Date", field_type="date", value="2026-06-15"),
            FormField(id="notify", label="Notify Me", field_type="toggle", value=True),
        ],
    )

    payload = SlackFormCardBuilder().build(form)

    name_block = _get_input_block(payload, "input_name")
    assert name_block["element"]["initial_value"] == "Acme Expansion"

    stage_block = _get_input_block(payload, "input_stage")
    assert stage_block["element"]["initial_option"]["value"] == "PROP"

    date_block = _get_input_block(payload, "input_close_date")
    assert date_block["element"]["initial_date"] == "2026-06-15"

    toggle_block = _get_input_block(payload, "input_notify")
    assert toggle_block["element"]["initial_options"][0]["value"] == "true"
