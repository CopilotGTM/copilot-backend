"""Transcript compression sub-agent.

Condenses raw meeting transcripts into speaker-attributed key points.
Used as a sub-agent inside the get_compressed_transcript tool.
"""

from src.agents.sub_agents.transcript_compression.agent import TranscriptCompressionAgent

__all__ = ["TranscriptCompressionAgent"]
