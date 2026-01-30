# Teams Meeting Bot (Note-Taker) - Complete Build Plan

## Status: PLANNING

## Problem Statement

CopilotGTM needs a bot that **joins Microsoft Teams meetings as a participant** (like Fireflies.ai / Gong), captures real-time audio, transcribes it, and generates meeting summaries. The bot must work **cross-tenant** -- joining meetings hosted by any organization, not just our own.

## Approach: Browser Automation (Industry Standard)

We use the same approach as Gong ($4.5B), Fireflies ($1B), Otter.ai, and Recall.ai:
- A **headless Chromium browser** (Playwright) joins the Teams meeting as an anonymous guest
- Audio is captured via **PulseAudio virtual audio device** inside a Docker container
- Audio streams to **Azure Speech Services** for real-time transcription
- Transcripts go to **Azure OpenAI** for summarization
- No Teams app installation required on the target organization

### Why Not Native Graph API?

The Graph API (`Calls.AccessMedia.All`) requires the Teams app to be **installed on the organizer's tenant**. This makes cross-tenant impossible without each org installing our app. Every major player in this space (Gong, Fireflies, Otter, Recall.ai) rejected this approach for the same reason.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Existing Infrastructure                       │
│                                                                      │
│  ┌─────────────────┐     ┌──────────────────────────────────────┐   │
│  │  Nango           │     │  FastAPI Backend                     │   │
│  │  (Calendar OAuth) │────▶│                                      │   │
│  │  Google + Outlook │     │  /api/meetings/schedule-bot          │   │
│  └─────────────────┘     │  /api/meetings/transcript-callback    │   │
│                           │  /api/meetings/{id}/summary           │   │
│                           └──────────┬───────────────────────────┘   │
│                                      │                               │
│                          Celery task / K8s Job creation               │
│                                      │                               │
│  ┌───────────────────────────────────▼───────────────────────────┐   │
│  │              Meeting Bot Worker Pod (per meeting)              │   │
│  │                                                                │   │
│  │  ┌────────────┐  ┌─────────────┐  ┌────────────────────────┐  │   │
│  │  │  Xvfb      │  │ PulseAudio  │  │  Playwright + Chromium │  │   │
│  │  │  :99       │  │ null-sink   │  │  (joins Teams meeting) │  │   │
│  │  └────────────┘  └──────┬──────┘  └────────────────────────┘  │   │
│  │                         │                                      │   │
│  │                    Audio capture                                │   │
│  │                    (16kHz PCM)                                  │   │
│  │                         │                                      │   │
│  │  ┌──────────────────────▼─────────────────────────────────┐    │   │
│  │  │  Azure Speech SDK (PushAudioInputStream)                │    │   │
│  │  │  Real-time speech-to-text                               │    │   │
│  │  └──────────────────────┬─────────────────────────────────┘    │   │
│  │                         │ transcript segments                   │   │
│  │                         ▼                                      │   │
│  │  POST /api/meetings/transcript-callback (to FastAPI backend)   │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI Backend (post-meeting)                                │   │
│  │                                                                │   │
│  │  1. Receive full transcript                                    │   │
│  │  2. Store in meetings table + transcript_segments              │   │
│  │  3. Azure OpenAI GPT-4o-mini → summary + action items         │   │
│  │  4. Store in calendar_event_deal_associations (JSONB)          │   │
│  │  5. Notify user via existing Teams messaging bot               │   │
│  └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Component Breakdown

### Component 1: Meeting Bot Worker (NEW Docker Image)

A standalone Python application that runs as a short-lived Kubernetes Job. One instance per meeting.

**Location in repo:**

```
copilot-backend/
├── meeting_bot/                          # NEW -- top-level module
│   ├── Dockerfile                        # Playwright + PulseAudio + Xvfb
│   ├── requirements.txt                  # Pinned deps for this image
│   ├── entrypoint.sh                     # Container startup script
│   ├── pulse-daemon.conf                 # PulseAudio daemon config
│   ├── pulse-client.conf                 # PulseAudio client config
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── main.py                       # Entry point -- orchestrates everything
│   │   ├── config.py                     # Env var config (meeting URL, callback, etc.)
│   │   ├── browser.py                    # Playwright setup + Teams join flow
│   │   ├── audio_capture.py              # PulseAudio monitor → PCM stream
│   │   ├── transcriber.py                # Azure Speech SDK integration
│   │   ├── caption_scraper.py            # Fallback: DOM caption scraping
│   │   ├── notifier.py                   # Send transcript to FastAPI callback
│   │   ├── state_machine.py              # Bot lifecycle states
│   │   └── health.py                     # Health check endpoint (HTTP)
│   └── tests/
│       ├── test_browser.py
│       ├── test_audio_capture.py
│       └── test_transcriber.py
```

### Component 2: Meeting Orchestrator (In Existing FastAPI Backend)

New service + worker tasks in the existing codebase.

**Location in repo:**

```
copilot-backend/
├── src/
│   ├── api/routers/crm/
│   │   └── meeting_bot_handler.py        # NEW -- API endpoints for bot management
│   ├── service/
│   │   └── meeting_bot/
│   │       ├── __init__.py
│   │       ├── orchestrator_service.py   # NEW -- spawn/monitor bot pods
│   │       ├── transcript_service.py     # NEW -- receive + store transcripts
│   │       └── summary_service.py        # NEW -- Azure OpenAI summarization
│   ├── workers/
│   │   └── deal_worker/
│   │       └── tasks/
│   │           └── meeting_bot_tasks.py  # NEW -- Celery tasks for scheduling
│   ├── domain/models/organisation_tables/sync/
│   │   └── meeting_intelligence.py       # EXISTING -- already has Meeting model
│   └── infra/config/
│       └── configs.py                    # ADD -- MeetingBotConfig class
```

### Component 3: Infrastructure (In copilot-infra Repo)

```
copilot-infra/
├── terraform/azure/modules/
│   └── speech_services/                  # NEW module
│       ├── main.tf                       # azurerm_cognitive_account
│       ├── variables.tf
│       └── outputs.tf
├── terraform/azure/dev/
│   └── main.tf                           # ADD speech_services module invocation
```

---

## Detailed Implementation Plan

### Phase 1: Meeting Bot Worker Docker Image

#### 1.1 Dockerfile

```dockerfile
# meeting_bot/Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

# Install PulseAudio + Xvfb + audio utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio \
    xvfb \
    ffmpeg \
    libasound2-dev \
    libportaudio2 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browsers
RUN playwright install chromium --with-deps

# Copy PulseAudio config
COPY pulse-daemon.conf /etc/pulse/daemon.conf
COPY pulse-client.conf /etc/pulse/client.conf

# Copy application
COPY bot/ /app/bot/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

# Health check
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=10s \
    CMD curl -f http://localhost:8090/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
```

#### 1.2 PulseAudio Configuration

```ini
# meeting_bot/pulse-daemon.conf
daemonize = no
fail = yes
default-sample-format = s16le
default-sample-rate = 16000
default-sample-channels = 1
default-fragments = 4
default-fragment-size-msec = 25
exit-idle-time = -1
load-default-script-file = no
```

```ini
# meeting_bot/pulse-client.conf
autospawn = no
daemon-binary = /bin/true
```

#### 1.3 Container Entrypoint

```bash
#!/bin/bash
# meeting_bot/entrypoint.sh
set -e

# Start virtual display
Xvfb :99 -screen 0 1920x1080x24 -ac &
export DISPLAY=:99

# Start PulseAudio
pulseaudio --fail --daemonize=false --exit-idle-time=-1 &
sleep 2

# Create virtual audio sink
pactl load-module module-null-sink sink_name=virtual_speaker sink_properties=device.description="Virtual_Speaker"
pacmd set-default-sink virtual_speaker
pacmd set-default-source virtual_speaker.monitor

echo "Audio pipeline ready: virtual_speaker.monitor"

# Launch the bot
exec python -m bot.main
```

#### 1.4 Requirements

```txt
# meeting_bot/requirements.txt
playwright==1.50.0
azure-cognitiveservices-speech==1.42.0
pyaudio==0.2.14
httpx==0.27.0
pydantic==2.11.7
pydantic-settings==2.7.0
uvicorn==0.35.0
fastapi==0.116.1
```

#### 1.5 Bot Config

```python
# meeting_bot/bot/config.py
from pydantic_settings import BaseSettings

class BotConfig(BaseSettings):
    # Meeting
    meeting_url: str                          # Teams join URL
    meeting_id: str                           # Internal meeting UUID
    bot_display_name: str = "CopilotGTM Notetaker"

    # Callbacks
    callback_url: str                         # FastAPI endpoint to POST transcripts
    callback_auth_token: str                  # Bearer token for callback

    # Azure Speech
    azure_speech_key: str
    azure_speech_region: str = "centralus"
    speech_language: str = "en-US"

    # Behavior
    max_meeting_duration_seconds: int = 14400  # 4 hours max
    lobby_wait_timeout_seconds: int = 300      # 5 min lobby wait
    health_port: int = 8090

    # Audio
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_chunk_size: int = 1024

    class Config:
        env_prefix = "BOT_"
```

#### 1.6 Browser Join Flow (Core Logic)

```python
# meeting_bot/bot/browser.py
import asyncio
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.async_api import async_playwright, Page, Browser
from bot.config import BotConfig
from bot.state_machine import BotState, BotStateMachine

# Lock user agent to stabilize Teams DOM selectors
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

class TeamsBrowser:
    def __init__(self, config: BotConfig, state: BotStateMachine):
        self.config = config
        self.state = state
        self.browser: Browser | None = None
        self.page: Page | None = None

    async def launch(self):
        """Launch headless Chromium with audio routed to PulseAudio."""
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--disable-gpu",
                "--disable-extensions",
                f"--window-size=1920,1080",
            ],
        )
        context = await self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            permissions=["microphone", "camera"],
        )
        self.page = await context.new_page()

    async def join_meeting(self):
        """Full join flow: navigate → fill name → join → handle lobby."""
        self.state.transition(BotState.NAVIGATING)

        # Step 1: Rewrite URL to bypass "Open in app" dialog
        # Reference: recallai/microsoft-teams-meeting-bot
        join_url = await self._rewrite_teams_url(self.config.meeting_url)
        await self.page.goto(join_url, wait_until="networkidle")

        # Step 2: Enter display name
        self.state.transition(BotState.ENTERING_NAME)
        name_input = self.page.get_by_placeholder("Type your name")
        await name_input.fill(self.config.bot_display_name)

        # Step 3: Click "Join now"
        join_button = self.page.get_by_role("button", name="Join now")
        await join_button.click()

        # Step 4: Detect lobby or direct entry
        self.state.transition(BotState.IN_LOBBY)
        lobby_or_meeting = await self._wait_for_lobby_or_meeting()
        return lobby_or_meeting

    async def _rewrite_teams_url(self, url: str) -> str:
        """
        Rewrite Teams meeting URL to skip the launcher page.
        Sets msLaunch=false, suppressPrompt=true, etc.
        Reference: Recall.ai open-source implementation
        """
        # Follow redirects to get the actual URL
        import httpx
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url)
            resolved_url = str(response.url)

        parsed = urlparse(resolved_url)
        params = parse_qs(parsed.query)
        params.update({
            "msLaunch": ["false"],
            "type": ["meetup-join"],
            "directDl": ["true"],
            "enableMobilePage": ["true"],
            "suppressPrompt": ["true"],
        })
        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    async def _wait_for_lobby_or_meeting(self) -> BotState:
        """Wait for either lobby admission or direct meeting entry."""
        lobby_text = "Someone in the meeting should let you in soon"
        meeting_indicator = "[data-tid='call-composite']"

        try:
            # Race: lobby text vs meeting composite
            result = await asyncio.wait_for(
                self._detect_first(lobby_text, meeting_indicator),
                timeout=30,
            )
            if result == "lobby":
                self.state.transition(BotState.IN_LOBBY)
                # Wait for admission
                await self.page.wait_for_selector(
                    meeting_indicator,
                    timeout=self.config.lobby_wait_timeout_seconds * 1000,
                )
                self.state.transition(BotState.IN_MEETING)
                return BotState.IN_MEETING
            else:
                self.state.transition(BotState.IN_MEETING)
                return BotState.IN_MEETING
        except asyncio.TimeoutError:
            self.state.transition(BotState.LOBBY_TIMEOUT)
            return BotState.LOBBY_TIMEOUT

    async def _detect_first(self, lobby_text: str, meeting_sel: str) -> str:
        """Return whichever element appears first."""
        lobby_task = asyncio.create_task(
            self.page.get_by_text(lobby_text).wait_for(timeout=30000)
        )
        meeting_task = asyncio.create_task(
            self.page.wait_for_selector(meeting_sel, timeout=30000)
        )
        done, pending = await asyncio.wait(
            [lobby_task, meeting_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if lobby_task in done:
            return "lobby"
        return "meeting"

    async def enable_captions(self):
        """Enable live captions as fallback transcript source."""
        # Open More actions menu → Turn on live captions
        more_button = self.page.get_by_label("More actions")
        await more_button.click()
        captions_button = self.page.get_by_text("Turn on live captions")
        await captions_button.click()

    async def leave_meeting(self):
        """Gracefully leave the meeting."""
        self.state.transition(BotState.LEAVING)
        try:
            leave_button = self.page.get_by_label("Leave")
            await leave_button.click()
        except Exception:
            pass
        finally:
            if self.browser:
                await self.browser.close()
            self.state.transition(BotState.COMPLETED)

    async def detect_meeting_ended(self) -> bool:
        """Check if meeting has ended (host ended or everyone left)."""
        end_indicators = [
            "The meeting has ended",
            "You've been removed from the meeting",
            "Return to call",
        ]
        for text in end_indicators:
            try:
                el = self.page.get_by_text(text)
                if await el.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
        return False
```

#### 1.7 Audio Capture

```python
# meeting_bot/bot/audio_capture.py
import pyaudio
import threading
import logging

logger = logging.getLogger(__name__)

class AudioCapture:
    """
    Captures audio from PulseAudio monitor source.
    The browser outputs meeting audio → PulseAudio null-sink.
    We read from null-sink.monitor → raw PCM bytes.

    Reference: dunkbing/meeting-bot PulseAudio architecture
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1, chunk_size: int = 1024):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        self._running = False
        self._thread: threading.Thread | None = None
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._callback = None

    def start(self, callback):
        """
        Start capturing audio. Calls callback(pcm_bytes) for each chunk.
        callback receives raw PCM bytes (16-bit signed LE, mono, 16kHz).
        """
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Audio capture started (16kHz, mono, s16le)")

    def stop(self):
        """Stop capturing audio."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._pa:
            self._pa.terminate()
        logger.info("Audio capture stopped")

    def _capture_loop(self):
        """Read PCM from PulseAudio monitor in a background thread."""
        self._pa = pyaudio.PyAudio()

        # Find the monitor source (virtual_speaker.monitor)
        device_index = self._find_monitor_device()

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self.chunk_size,
        )

        while self._running:
            try:
                data = self._stream.read(self.chunk_size, exception_on_overflow=False)
                if self._callback:
                    self._callback(data)
            except Exception as e:
                logger.error(f"Audio capture error: {e}")
                break

    def _find_monitor_device(self) -> int:
        """Find PulseAudio monitor device index."""
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if "monitor" in info["name"].lower() and info["maxInputChannels"] > 0:
                logger.info(f"Using audio device: {info['name']} (index {i})")
                return i
        raise RuntimeError("No PulseAudio monitor device found. Is PulseAudio running?")
```

#### 1.8 Azure Speech Transcription

```python
# meeting_bot/bot/transcriber.py
import azure.cognitiveservices.speech as speechsdk
import logging
import json
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

class TranscriptSegment:
    def __init__(self, text: str, speaker: str = "", start_time: float = 0.0, duration: float = 0.0):
        self.text = text
        self.speaker = speaker
        self.start_time = start_time
        self.duration = duration
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "speaker": self.speaker,
            "start_time": self.start_time,
            "duration": self.duration,
            "timestamp": self.timestamp,
        }

class AzureSpeechTranscriber:
    """
    Real-time transcription using Azure Speech SDK.
    Receives PCM audio via PushAudioInputStream.
    Audio format matches PulseAudio output: 16kHz, 16-bit, mono.
    """

    def __init__(self, speech_key: str, speech_region: str, language: str = "en-US"):
        self.speech_key = speech_key
        self.speech_region = speech_region
        self.language = language
        self._recognizer = None
        self._push_stream = None
        self._on_transcript: Callable | None = None
        self._segments: list[TranscriptSegment] = []

    def start(self, on_transcript: Callable[[TranscriptSegment], None]):
        """Start continuous recognition. Calls on_transcript for each recognized segment."""
        self._on_transcript = on_transcript

        # Create push stream -- format matches PulseAudio output exactly
        audio_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000,
            bits_per_sample=16,
            channels=1,
        )
        self._push_stream = speechsdk.audio.PushAudioInputStream(audio_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self._push_stream)

        # Configure speech recognizer
        speech_config = speechsdk.SpeechConfig(
            subscription=self.speech_key,
            region=self.speech_region,
        )
        speech_config.speech_recognition_language = self.language
        speech_config.request_word_level_timestamps()

        # Enable speaker diarization (identify different speakers)
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
            "Continuous",
        )

        self._recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config,
        )

        # Wire up event handlers
        self._recognizer.recognized.connect(self._on_recognized)
        self._recognizer.recognizing.connect(self._on_recognizing)
        self._recognizer.session_stopped.connect(self._on_session_stopped)
        self._recognizer.canceled.connect(self._on_canceled)

        # Start continuous recognition
        self._recognizer.start_continuous_recognition()
        logger.info("Azure Speech transcriber started")

    def push_audio(self, pcm_bytes: bytes):
        """Push raw PCM audio bytes from AudioCapture."""
        if self._push_stream:
            self._push_stream.write(pcm_bytes)

    def stop(self) -> list[TranscriptSegment]:
        """Stop recognition and return all segments."""
        if self._recognizer:
            self._recognizer.stop_continuous_recognition()
        if self._push_stream:
            self._push_stream.close()
        logger.info(f"Transcriber stopped. Total segments: {len(self._segments)}")
        return self._segments

    def _on_recognized(self, evt):
        """Fired when a phrase is finalized."""
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech and evt.result.text:
            segment = TranscriptSegment(
                text=evt.result.text,
                start_time=evt.result.offset / 10_000_000,  # ticks to seconds
                duration=evt.result.duration / 10_000_000,
            )
            self._segments.append(segment)
            if self._on_transcript:
                self._on_transcript(segment)
            logger.debug(f"Recognized: {evt.result.text[:80]}...")

    def _on_recognizing(self, evt):
        """Fired for interim/partial results (not finalized)."""
        pass  # Can be used for real-time streaming to frontend

    def _on_session_stopped(self, evt):
        logger.info("Speech session stopped")

    def _on_canceled(self, evt):
        logger.warning(f"Speech recognition canceled: {evt.cancellation_details}")
```

#### 1.9 Bot Main Entrypoint

```python
# meeting_bot/bot/main.py
import asyncio
import signal
import logging
from bot.config import BotConfig
from bot.browser import TeamsBrowser
from bot.audio_capture import AudioCapture
from bot.transcriber import AzureSpeechTranscriber, TranscriptSegment
from bot.notifier import TranscriptNotifier
from bot.state_machine import BotState, BotStateMachine
from bot.health import start_health_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("meeting-bot")

async def run_bot():
    config = BotConfig()
    state = BotStateMachine()
    browser = TeamsBrowser(config, state)
    audio = AudioCapture(
        sample_rate=config.audio_sample_rate,
        channels=config.audio_channels,
        chunk_size=config.audio_chunk_size,
    )
    transcriber = AzureSpeechTranscriber(
        speech_key=config.azure_speech_key,
        speech_region=config.azure_speech_region,
        language=config.speech_language,
    )
    notifier = TranscriptNotifier(
        callback_url=config.callback_url,
        auth_token=config.callback_auth_token,
        meeting_id=config.meeting_id,
    )

    # Start health check server
    asyncio.create_task(start_health_server(state, config.health_port))

    # Graceful shutdown
    shutdown = asyncio.Event()
    def handle_signal(sig):
        logger.info(f"Received {sig}, shutting down...")
        shutdown.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_event_loop().add_signal_handler(sig, handle_signal, sig)

    try:
        # Phase 1: Launch browser and join meeting
        logger.info(f"Joining meeting: {config.meeting_url}")
        await browser.launch()
        join_result = await browser.join_meeting()

        if join_result == BotState.LOBBY_TIMEOUT:
            logger.warning("Lobby timeout -- not admitted")
            await notifier.send_status("lobby_timeout")
            return

        logger.info("Successfully joined meeting")
        await notifier.send_status("joined")

        # Phase 2: Start audio capture → transcription pipeline
        def on_audio_chunk(pcm_bytes: bytes):
            transcriber.push_audio(pcm_bytes)

        def on_transcript_segment(segment: TranscriptSegment):
            # Fire-and-forget send to backend
            asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.create_task,
                notifier.send_segment(segment),
            )

        transcriber.start(on_transcript=on_transcript_segment)
        audio.start(callback=on_audio_chunk)

        # Optionally enable caption scraping as fallback
        try:
            await browser.enable_captions()
        except Exception as e:
            logger.warning(f"Could not enable captions (non-fatal): {e}")

        # Phase 3: Wait for meeting to end or shutdown signal
        while not shutdown.is_set():
            if await browser.detect_meeting_ended():
                logger.info("Meeting has ended")
                break
            await asyncio.sleep(5)

            # Safety: max duration limit
            # (BotStateMachine tracks elapsed time)
            if state.elapsed_seconds() > config.max_meeting_duration_seconds:
                logger.warning("Max meeting duration reached, leaving")
                break

    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)
        await notifier.send_status("error", error=str(e))
    finally:
        # Phase 4: Cleanup and send final transcript
        logger.info("Shutting down bot...")
        audio.stop()
        segments = transcriber.stop()
        await browser.leave_meeting()

        # Send complete transcript to backend
        await notifier.send_complete_transcript(segments)
        await notifier.send_status("completed")
        logger.info(f"Bot finished. {len(segments)} transcript segments captured.")

if __name__ == "__main__":
    asyncio.run(run_bot())
```

#### 1.10 State Machine

```python
# meeting_bot/bot/state_machine.py
import time
from enum import Enum

class BotState(str, Enum):
    INITIALIZING = "initializing"
    NAVIGATING = "navigating"
    ENTERING_NAME = "entering_name"
    IN_LOBBY = "in_lobby"
    LOBBY_TIMEOUT = "lobby_timeout"
    IN_MEETING = "in_meeting"
    CAPTURING = "capturing"
    LEAVING = "leaving"
    COMPLETED = "completed"
    ERROR = "error"

class BotStateMachine:
    def __init__(self):
        self.state = BotState.INITIALIZING
        self._start_time = time.time()
        self._meeting_join_time: float | None = None

    def transition(self, new_state: BotState):
        self.state = new_state
        if new_state == BotState.IN_MEETING:
            self._meeting_join_time = time.time()

    def elapsed_seconds(self) -> float:
        if self._meeting_join_time:
            return time.time() - self._meeting_join_time
        return 0.0

    def is_active(self) -> bool:
        return self.state in (BotState.IN_MEETING, BotState.CAPTURING)
```

#### 1.11 Notifier (Callback to FastAPI)

```python
# meeting_bot/bot/notifier.py
import httpx
import logging
from bot.transcriber import TranscriptSegment

logger = logging.getLogger(__name__)

class TranscriptNotifier:
    """Sends transcript data back to the FastAPI backend."""

    def __init__(self, callback_url: str, auth_token: str, meeting_id: str):
        self.callback_url = callback_url
        self.auth_token = auth_token
        self.meeting_id = meeting_id
        self._client = httpx.AsyncClient(timeout=30)

    async def send_segment(self, segment: TranscriptSegment):
        """Send a single transcript segment (real-time)."""
        try:
            await self._client.post(
                f"{self.callback_url}/segment",
                json={"meeting_id": self.meeting_id, "segment": segment.to_dict()},
                headers={"Authorization": f"Bearer {self.auth_token}"},
            )
        except Exception as e:
            logger.warning(f"Failed to send segment: {e}")

    async def send_complete_transcript(self, segments: list[TranscriptSegment]):
        """Send the full transcript when meeting ends."""
        try:
            await self._client.post(
                f"{self.callback_url}/complete",
                json={
                    "meeting_id": self.meeting_id,
                    "segments": [s.to_dict() for s in segments],
                    "full_text": " ".join(s.text for s in segments),
                },
                headers={"Authorization": f"Bearer {self.auth_token}"},
            )
        except Exception as e:
            logger.error(f"Failed to send complete transcript: {e}")

    async def send_status(self, status: str, error: str = ""):
        """Send bot status update."""
        try:
            await self._client.post(
                f"{self.callback_url}/status",
                json={"meeting_id": self.meeting_id, "status": status, "error": error},
                headers={"Authorization": f"Bearer {self.auth_token}"},
            )
        except Exception as e:
            logger.warning(f"Failed to send status: {e}")
```

---

### Phase 2: FastAPI Backend Integration

#### 2.1 Config Addition

Add to existing `src/infra/config/configs.py`:

```python
class MeetingBotConfig:
    MEETING_BOT_ENABLED: bool = os.getenv("MEETING_BOT_ENABLED", "false").lower() == "true"
    MEETING_BOT_IMAGE: str = os.getenv("MEETING_BOT_IMAGE", "copilotgtmacrdev.azurecr.io/meeting-bot:latest")
    MEETING_BOT_NAMESPACE: str = os.getenv("MEETING_BOT_NAMESPACE", "copilotgtm")
    MEETING_BOT_SERVICE_ACCOUNT: str = os.getenv("MEETING_BOT_SERVICE_ACCOUNT", "copilot-workers")
    MEETING_BOT_CALLBACK_URL: str = os.getenv("MEETING_BOT_CALLBACK_URL", "http://copilot-backend.copilotgtm.svc:8000/api/meetings/bot-callback")
    MEETING_BOT_MAX_CONCURRENT: int = int(os.getenv("MEETING_BOT_MAX_CONCURRENT", "10"))
    AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
    AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "centralus")
```

#### 2.2 Orchestrator Service (Spawn K8s Jobs)

```python
# src/service/meeting_bot/orchestrator_service.py
from kubernetes import client, config as k8s_config
from src.infra.config.configs import meeting_bot_config
import logging

logger = logging.getLogger(__name__)

class MeetingBotOrchestrator:
    def __init__(self):
        k8s_config.load_incluster_config()
        self.batch_v1 = client.BatchV1Api()

    async def spawn_bot(self, meeting_id: str, meeting_url: str, callback_auth_token: str) -> str:
        """Create a K8s Job that runs a meeting bot worker for one meeting."""
        job_name = f"meeting-bot-{meeting_id[:8]}"
        namespace = meeting_bot_config.MEETING_BOT_NAMESPACE

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=namespace,
                labels={"app": "meeting-bot", "meeting-id": meeting_id[:8]},
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=600,
                active_deadline_seconds=14400,  # 4 hour max
                backoff_limit=0,                # No retries
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={"app": "meeting-bot"},
                    ),
                    spec=client.V1PodSpec(
                        service_account_name=meeting_bot_config.MEETING_BOT_SERVICE_ACCOUNT,
                        restart_policy="Never",
                        containers=[
                            client.V1Container(
                                name="meeting-bot",
                                image=meeting_bot_config.MEETING_BOT_IMAGE,
                                env=[
                                    client.V1EnvVar(name="BOT_MEETING_URL", value=meeting_url),
                                    client.V1EnvVar(name="BOT_MEETING_ID", value=meeting_id),
                                    client.V1EnvVar(name="BOT_CALLBACK_URL", value=meeting_bot_config.MEETING_BOT_CALLBACK_URL),
                                    client.V1EnvVar(name="BOT_CALLBACK_AUTH_TOKEN", value=callback_auth_token),
                                    client.V1EnvVar(name="BOT_AZURE_SPEECH_KEY", value=meeting_bot_config.AZURE_SPEECH_KEY),
                                    client.V1EnvVar(name="BOT_AZURE_SPEECH_REGION", value=meeting_bot_config.AZURE_SPEECH_REGION),
                                ],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "500m", "memory": "768Mi"},
                                    limits={"cpu": "1000m", "memory": "1536Mi"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

        self.batch_v1.create_namespaced_job(namespace=namespace, body=job)
        logger.info(f"Spawned meeting bot job: {job_name}")
        return job_name
```

#### 2.3 Celery Task for Scheduled Bot Spawning

```python
# src/workers/deal_worker/tasks/meeting_bot_tasks.py
# Add to deal_worker since it already handles meeting insights

@app.task(bind=True, name="deal_worker.schedule_meeting_bots")
def schedule_meeting_bots(self):
    """
    Periodic task (beat schedule): scan calendars for upcoming Teams meetings
    and spawn bot workers for meetings starting in the next 1-2 minutes.

    This uses the existing Nango calendar sync data already stored in
    the calendar_events table.
    """
    # Query calendar_events where:
    # - start_time is within next 2 minutes
    # - has online_meeting_url containing 'teams.microsoft.com'
    # - no bot already spawned (check meetings table for existing bot record)
    # For each, call orchestrator.spawn_bot()
    pass

@app.task(bind=True, name="deal_worker.process_meeting_transcript")
def process_meeting_transcript(self, meeting_id: str):
    """
    Called when bot sends complete transcript.
    Summarize with Azure OpenAI and store results.
    """
    # 1. Fetch transcript from meetings table
    # 2. Send to Azure OpenAI for summary + action items
    # 3. Update calendar_event_deal_associations with insights
    # 4. Notify user via Teams messaging bot
    pass
```

---

### Phase 3: Infrastructure (Terraform in copilot-infra)

#### 3.1 Azure Speech Services Module

```hcl
# terraform/azure/modules/speech_services/main.tf
resource "azurerm_cognitive_account" "speech" {
  name                = "copilotgtm-speech-${var.env}"
  location            = var.location
  resource_group_name = var.resource_group_name
  kind                = "SpeechServices"
  sku_name            = var.env == "prod" ? "S0" : "F0"  # Free for dev

  tags = var.tags
}

resource "azurerm_key_vault_secret" "speech_key" {
  name         = "azure-speech-key"
  value        = azurerm_cognitive_account.speech.primary_access_key
  key_vault_id = var.key_vault_id
}
```

#### 3.2 Dockerfile Build Target Addition

Add to existing `Dockerfile` in copilot-backend:

```dockerfile
# New build target for meeting bot worker
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble AS meeting-bot

RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio xvfb ffmpeg libportaudio2 portaudio19-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY meeting_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN playwright install chromium --with-deps

COPY meeting_bot/pulse-daemon.conf /etc/pulse/daemon.conf
COPY meeting_bot/pulse-client.conf /etc/pulse/client.conf
COPY meeting_bot/bot/ /app/bot/
COPY meeting_bot/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app
EXPOSE 8090
ENTRYPOINT ["/app/entrypoint.sh"]
```

---

## Open-Source References

| Project | URL | What to Reuse |
|---|---|---|
| **Recall.ai Teams Bot** | https://github.com/recallai/microsoft-teams-meeting-bot | URL rewriting, join flow, lobby detection, caption scraping, Docker orchestration |
| **dunkbing/meeting-bot** | https://github.com/dunkbing/meeting-bot | PulseAudio + GStreamer audio capture in Docker |
| **ScreenApp Meeting Bot** | https://github.com/screenappai/meeting-bot | Stealth mode, graceful shutdown, Azure Blob upload, single-job pattern |
| **CueMeet Teams Bot** | https://github.com/CueMeet/cuemeet-teams-bot | Python + PulseAudio configs (pulse-daemon.conf, pulse-client.conf), entrypoint.sh |
| **Meeting-BaaS** | https://github.com/Meeting-Baas/meet-teams-bot | Chrome vs Chromium insight (use Chrome for Teams), JSON output pattern |
| **Attendee** | https://github.com/attendee-labs/attendee | Python/Django bot management, partial transcript streaming |

---

## Dependencies Summary

### Meeting Bot Worker (NEW image)

| Package | Version | Purpose |
|---|---|---|
| `playwright` | 1.50.0 | Browser automation (Chromium) |
| `azure-cognitiveservices-speech` | 1.42.0 | Azure Speech SDK (real-time STT) |
| `pyaudio` | 0.2.14 | PulseAudio PCM capture |
| `httpx` | 0.27.0 | Async HTTP client (callbacks to backend) |
| `pydantic-settings` | 2.7.0 | Typed config from env vars |
| `uvicorn` + `fastapi` | latest | Health check endpoint |

### System Packages (in Docker)

| Package | Purpose |
|---|---|
| `pulseaudio` | Virtual audio device management |
| `xvfb` | Virtual display for headless Chromium |
| `ffmpeg` | Audio format conversion (if needed) |
| `portaudio19-dev` | PyAudio C library dependency |

### FastAPI Backend Additions (existing image)

| Package | Version | Purpose |
|---|---|---|
| `kubernetes` | 31.0.0 | K8s API client (spawn Jobs) |

---

## Existing Resources Used (No New Provisioning)

| Resource | Already Exists | Used For |
|---|---|---|
| AKS Cluster | Yes | Run bot worker pods |
| ACR | Yes | Store meeting-bot Docker image |
| Azure OpenAI | Yes | Transcript summarization |
| PostgreSQL | Yes | Store transcripts (meetings + transcript_segments tables) |
| Redis | Yes | Celery broker for scheduling tasks |
| Key Vault | Yes | Store Azure Speech key |
| Nango | Yes | Calendar access (detect upcoming meetings) |
| Teams Bot Service | Yes | Deliver summaries back to users |
| copilot-workers SA | Yes | Service account for bot pods |

## New Azure Resource Required

| Resource | SKU | Cost |
|---|---|---|
| **Azure Speech Services** | F0 (free, 5hrs/mo) for dev, S0 ($1/hr) for prod | ~$100/mo for 100 meetings |

---

## Data Flow (Using Existing Models)

The `meeting_intelligence.py` models already support this. The bot writes to:

```
calendar_events (existing)
    ├── online_meeting_url → detected by scheduler, passed to bot
    └── calendar_event_id → links to meeting record
         │
meetings (existing)
    ├── source = MeetingSource.NATIVE_BOT    ← our bot
    ├── status = MeetingStatus.PROCESSING    ← while bot is running
    ├── full_transcript = "..."              ← from bot callback
    └── meeting_id → links to segments
         │
transcript_segments (existing)
    ├── text, speaker, start_time, duration  ← from Azure Speech
    └── segment_sequence                     ← ordering
         │
calendar_event_deal_associations (existing)
    └── ai_insights (JSONB)                  ← summary, action items from GPT-4o
```

---

## Build & Deploy Order

1. **Terraform**: Add `speech_services` module → `terraform apply`
2. **Meeting Bot Worker**: Build Docker image → push to ACR
3. **FastAPI Backend**: Add orchestrator service + Celery tasks + callback endpoints
4. **Helm**: Add meeting-bot-related env vars to values.yaml (Speech key, image tag)
5. **Test**: Manual trigger → spawn bot → join test meeting → verify transcript
6. **Beat Schedule**: Enable periodic calendar scanning in deal-worker beat
