"""Azure OpenAI audio transcription via the REST audio endpoint.

Slack voice-note transcription now runs in the API process, so this helper avoids
the worker-only SDK stack and talks to Azure directly over HTTP using the same
deployment/env configuration as the rest of the app.
"""

from __future__ import annotations

import io

import httpx
from src.infra.config.configs import transcription_config
from src.infra.utils.logger import get_logger

logger = get_logger("audio_transcription")


def _build_transcription_url(endpoint: str, deployment: str, api_version: str) -> str:
    """Build the Azure OpenAI audio transcription endpoint for a deployment."""
    base_endpoint = endpoint.rstrip("/")
    base_path = base_endpoint if base_endpoint.endswith("/openai") else f"{base_endpoint}/openai"

    return f"{base_path}/deployments/{deployment}/audio/transcriptions?api-version={api_version}"


def _extract_transcript(response: httpx.Response) -> str:
    """Handle both JSON and plain-text transcription responses."""
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("text") or "").strip()
        return ""

    return response.text.strip()


async def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "audio.webm",
    language: str | None = None,
) -> str:
    """Transcribe audio bytes to text using Azure OpenAI gpt-4o-transcribe.

    Args:
        audio_bytes: Raw audio content (webm, mp3, wav, ogg, etc.)
        filename: Original filename; the extension helps the API detect format.
        language: Optional ISO-639-1 language code (e.g. "en") for accuracy.

    Returns:
        Transcribed text, or empty string if transcription fails.
    """
    api_key = transcription_config.AZURE_TRANSCRIPTION_API_KEY
    endpoint = transcription_config.AZURE_TRANSCRIPTION_ENDPOINT
    deployment = transcription_config.AZURE_TRANSCRIPTION_DEPLOYMENT
    api_version = transcription_config.AZURE_TRANSCRIPTION_API_VERSION

    if not api_key or not endpoint or not deployment:
        logger.warning("Audio transcription skipped: missing Azure credentials")
        return ""

    try:
        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = filename

        form_data = {"model": deployment}
        if language:
            form_data["language"] = language

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                _build_transcription_url(endpoint, deployment, api_version),
                headers={"api-key": api_key},
                data=form_data,
                files={"file": (filename, file_obj, "application/octet-stream")},
            )
            response.raise_for_status()

        transcript = _extract_transcript(response)
        logger.info(
            "Audio transcription complete: %s -> %d chars",
            filename,
            len(transcript),
        )
        return transcript

    except Exception:
        logger.warning("Audio transcription failed for %s", filename, exc_info=True)
        return ""
