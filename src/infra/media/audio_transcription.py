"""Transcribe stored chat audio through AssemblyAI or Azure OpenAI.

Slack/WhatsApp voice-note transcription runs in API and worker processes, so this
module uses plain HTTP and environment-backed config instead of provider SDKs.
AssemblyAI is the default ASR provider; Azure OpenAI stays selectable during
rollout with ASR_PROVIDER=azure_openai.
"""

from __future__ import annotations

import asyncio
import io

import httpx
from src.infra.config.configs import transcription_config
from src.infra.utils.logger import get_logger

logger = get_logger("audio_transcription")


def _build_assemblyai_url(base_url: str, path: str) -> str:
    """Build a stable AssemblyAI API URL from a configurable base URL."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


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
    """Transcribe audio bytes to text using the configured ASR provider.

    Args:
        audio_bytes: Raw audio content (webm, mp3, wav, ogg, etc.)
        filename: Original filename; the extension helps the API detect format.
        language: Optional ISO-639-1 language code (e.g. "en") for accuracy.

    Returns:
        Transcribed text, or empty string if transcription fails.
    """
    provider = transcription_config.ASR_PROVIDER
    if provider == "assemblyai":
        return await _transcribe_with_assemblyai(audio_bytes, filename, language)
    if provider in {"azure_openai", "azure"}:
        return await _transcribe_with_azure_openai(audio_bytes, filename, language)

    logger.warning("Audio transcription skipped: unsupported ASR_PROVIDER=%s", provider)
    return ""


async def _transcribe_with_assemblyai(
    audio_bytes: bytes,
    filename: str,
    language: str | None,
) -> str:
    """Upload audio to AssemblyAI, submit a pre-recorded STT job, then poll it."""
    api_key = await transcription_config.get_assemblyai_api_key()
    base_url = transcription_config.ASSEMBLYAI_BASE_URL
    speech_model = transcription_config.ASSEMBLYAI_SPEECH_MODEL

    if not api_key or not base_url or not speech_model:
        logger.warning("Audio transcription skipped: missing AssemblyAI configuration")
        return ""

    headers = {"authorization": api_key}
    upload_headers = {**headers, "content-type": "application/octet-stream"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # AssemblyAI requires local bytes to be uploaded first; /v2/transcript
            # then receives the returned private upload_url.
            upload_response = await client.post(
                _build_assemblyai_url(base_url, "/v2/upload"),
                headers=upload_headers,
                content=audio_bytes,
            )
            upload_response.raise_for_status()
            upload_url = str(upload_response.json().get("upload_url") or "")
            if not upload_url:
                logger.warning("AssemblyAI upload did not return upload_url for %s", filename)
                return ""

            payload: dict[str, object] = {
                "audio_url": upload_url,
                "language_detection": transcription_config.ASSEMBLYAI_LANGUAGE_DETECTION,
                "speech_models": [speech_model],
            }
            if language:
                payload["language_code"] = language

            submit_response = await client.post(
                _build_assemblyai_url(base_url, "/v2/transcript"),
                headers=headers,
                json=payload,
            )
            submit_response.raise_for_status()
            transcript_id = str(submit_response.json().get("id") or "")
            if not transcript_id:
                logger.warning("AssemblyAI submit did not return transcript id for %s", filename)
                return ""

            transcript = await _poll_assemblyai_transcript(client, base_url, headers, transcript_id)

        logger.info(
            "AssemblyAI transcription complete: %s -> %d chars",
            filename,
            len(transcript),
        )
        return transcript

    except Exception:
        logger.warning("AssemblyAI transcription failed for %s", filename, exc_info=True)
        return ""


async def _poll_assemblyai_transcript(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    transcript_id: str,
) -> str:
    """Poll AssemblyAI until the transcript completes, errors, or exceeds our cap."""
    poll_url = _build_assemblyai_url(base_url, f"/v2/transcript/{transcript_id}")

    for attempt in range(transcription_config.ASSEMBLYAI_MAX_POLL_ATTEMPTS):
        response = await client.get(poll_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        status = str(payload.get("status") or "").lower()

        if status == "completed":
            return str(payload.get("text") or "").strip()
        if status == "error":
            logger.warning(
                "AssemblyAI transcription failed: transcript_id=%s error=%s",
                transcript_id,
                payload.get("error"),
            )
            return ""

        if attempt < transcription_config.ASSEMBLYAI_MAX_POLL_ATTEMPTS - 1:
            await asyncio.sleep(transcription_config.ASSEMBLYAI_POLL_INTERVAL_SECONDS)

    logger.warning("AssemblyAI transcription timed out: transcript_id=%s", transcript_id)
    return ""


async def _transcribe_with_azure_openai(
    audio_bytes: bytes,
    filename: str,
    language: str | None,
) -> str:
    """Transcribe audio bytes to text using Azure OpenAI gpt-4o-transcribe."""
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
