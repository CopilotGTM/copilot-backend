from __future__ import annotations

from src.agents.common.json_extractor import JsonExtractor
from src.agents.common.metadata_parser import MetadataParser


def test_metadata_parser_ignores_switch_agent_metadata() -> None:
    parser = MetadataParser(JsonExtractor())

    frames = list(
        parser.parse(
            """
            {
              "version": "aep/1",
              "confidence_score": "high",
              "switch_agent": {
                "to": "RFP_SHEET",
                "reason": "User needs RFP help"
              }
            }
            """
        )
    )

    assert len(frames) == 1
    assert frames[0].type == "answer.metrics"
