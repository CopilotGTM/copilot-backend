"""Conversation intelligence provider registry."""

from .gong import (
    GongAnalyticsFetcher,
    GongCallsFetcher,
    GongSpeakersFetcher,
    GongTranscriptFetcher,
    fetch_analytics,
    fetch_calls,
    fetch_speakers,
    fetch_transcript_segments,
)

__all__ = [
    "GongCallsFetcher",
    "GongSpeakersFetcher",
    "GongTranscriptFetcher",
    "GongAnalyticsFetcher",
    "fetch_calls",
    "fetch_speakers",
    "fetch_transcript_segments",
    "fetch_analytics",
]
