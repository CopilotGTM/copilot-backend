from src.adapters import AgentResponse, ResponseType
from src.adapters.whatsapp_adapter import WhatsAppAdapter


def test_render_text_converts_markdown_table_to_mobile_rows() -> None:
    adapter = WhatsAppAdapter()
    text = """# Deal summary

| Name | Stage | Amount |
| --- | --- | --- |
| Acme | Discovery | $10k |
| Globex | Proposal | $25k |
"""

    payload = adapter.render_text(text)
    rendered = payload.content["text"]

    assert "Deal summary" in rendered
    assert "Name: Acme" in rendered
    assert "Stage: Discovery" in rendered
    assert "Amount: $10k" in rendered
    assert "| --- |" not in rendered
    assert "| Acme |" not in rendered


def test_render_text_converts_markdown_table_without_outer_pipes() -> None:
    adapter = WhatsAppAdapter()
    text = """Name | Stage
--- | ---
Acme | Discovery
"""

    payload = adapter.render_text(text)
    rendered = payload.content["text"]

    assert "Name: Acme" in rendered
    assert "Stage: Discovery" in rendered
    assert "--- | ---" not in rendered


def test_render_text_strips_heavy_slack_or_teams_markdown() -> None:
    adapter = WhatsAppAdapter()

    payload = adapter.render_text(
        "### Next steps\n"
        "> Review this\n\n"
        "```text\n"
        "**Create task** for [Acme](https://example.com/deals/1)\n"
        "```"
    )

    rendered = payload.content["text"]
    assert "Next steps" in rendered
    assert "Review this" in rendered
    assert "Create task for Acme (https://example.com/deals/1)" in rendered
    assert "###" not in rendered
    assert "```" not in rendered
    assert "**" not in rendered
    assert rendered == payload.text_fallback


def test_render_table_uses_compact_rows_without_pipe_tables() -> None:
    adapter = WhatsAppAdapter()
    rows = [
        {"Name": "Deal 1", "Stage": "Discovery"},
        {"Name": "Deal 2", "Stage": "Proposal"},
        {"Name": "Deal 3", "Stage": "Negotiation"},
    ]

    payload = adapter.render(
        AgentResponse(
            text="",
            type=ResponseType.TABLE,
            data={"title": "Open deals", "columns": ["Name", "Stage"], "rows": rows},
        )
    )

    rendered = payload.content["text"]
    assert rendered.startswith("Open deals")
    assert "1. Name: Deal 1; Stage: Discovery" in rendered
    assert "2. Name: Deal 2; Stage: Proposal" in rendered
    assert "|" not in rendered


def test_render_table_truncates_long_structured_results() -> None:
    adapter = WhatsAppAdapter()
    rows = [{"Name": f"Deal {index}"} for index in range(1, 11)]

    payload = adapter.render_table("Deals", ["Name"], rows, None)
    rendered = payload.content["text"]

    assert "8. Name: Deal 8" in rendered
    assert "Deal 9" not in rendered
    assert "2 more results not shown" in rendered
