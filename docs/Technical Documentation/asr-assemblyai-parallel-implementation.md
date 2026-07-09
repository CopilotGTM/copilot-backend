# AssemblyAI ASR Parallel Implementation

## Summary

CopilotGTM now routes stored audio transcription through `src/infra/media/audio_transcription.py`, with AssemblyAI as the default ASR provider and Azure OpenAI retained as a selectable fallback through `ASR_PROVIDER=azure_openai`.

This covers the backend audio paths that call `transcribe_audio(...)`:

- chat attachment tool audio extraction
- Slack voice notes and audio files
- WhatsApp voice notes
- Teams/Slack downloaded audio attachments resolved by the chat worker

Voice Realtime is separate: those sessions connect clients directly to Azure Realtime and receive `conversation.item.input_audio_transcription.completed` events from that realtime session. Migrating that path needs a separate realtime architecture change, not just this stored-audio helper swap.

## Research Notes

AssemblyAI's current pre-recorded flow is:

1. Upload local bytes to `POST /v2/upload` with the API key in the `authorization` header.
2. Submit `POST /v2/transcript` with the returned `upload_url` as `audio_url`.
3. Poll `GET /v2/transcript/{id}` until `status` is `completed` or `error`.

The selected model is `universal-3-5-pro` via:

```json
{
  "audio_url": "<assembly-upload-url>",
  "speech_models": ["universal-3-5-pro"],
  "language_detection": true
}
```

AssemblyAI documents Universal-3.5 Pro as its latest pre-recorded Voice AI model, with stronger accuracy, accented English support, code switching, contextual prompting, and automatic fallback to Universal-2 for broader language coverage. The API docs also document a short synchronous API (`sync.assemblyai.com/transcribe`, `X-AAI-Model: u3-sync-pro`) for clips up to 120 seconds, but this implementation uses the async pre-recorded API because our Slack/WhatsApp/Teams attachments can exceed short-clip limits and already run in worker-safe async flows.

Sources:

- https://www.assemblyai.com/docs/pre-recorded-audio/universal-3-5-pro
- https://www.assemblyai.com/docs/pre-recorded-audio/getting-started/transcribe-an-audio-file
- https://www.assemblyai.com/docs/openapi.yaml
- https://www.assemblyai.com/docs/api-reference/sync-api/transcribe

## Runtime Configuration

Required secret:

- `ASSEMBLYAI_API_KEY`: AssemblyAI project API key. Local/full deploy hydration looks for Key Vault secret `assemblyai-api-key`.

Provider settings:

| Env var | Default | Purpose |
| --- | --- | --- |
| `ASR_PROVIDER` | `assemblyai` | `assemblyai` or `azure_openai` |
| `ASSEMBLYAI_BASE_URL` | `https://api.assemblyai.com` | Use `https://api.eu.assemblyai.com` if EU residency is required |
| `ASSEMBLYAI_SPEECH_MODEL` | `universal-3-5-pro` | Pre-recorded STT model |
| `ASSEMBLYAI_LANGUAGE_DETECTION` | `true` | Let AssemblyAI detect the spoken language |
| `ASSEMBLYAI_POLL_INTERVAL_SECONDS` | `2` | Delay between transcript polls |
| `ASSEMBLYAI_MAX_POLL_ATTEMPTS` | `45` | Caps polling to about 90 seconds by default |

Azure fallback settings are still present:

- `AZURE_TRANSCRIPTION_API_KEY`
- `AZURE_TRANSCRIPTION_ENDPOINT`
- `AZURE_TRANSCRIPTION_DEPLOYMENT`
- `AZURE_TRANSCRIPTION_API_VERSION`

## Implementation Shape

`transcribe_audio(audio_bytes, filename, language)` remains the only caller-facing API. Callers do not know which provider is active.

For AssemblyAI:

- upload raw `audio_bytes` to `/v2/upload`
- submit `/v2/transcript` with `speech_models: ["universal-3-5-pro"]`
- include `language_code` when callers pass a language
- return stripped transcript text on completion
- return empty string on missing config, provider failure, transcript error, or polling timeout

This preserves the existing failure contract: audio transcript enrichment is best effort and should not break chat processing.

## Rollout

1. Add `assemblyai-api-key` to the master Key Vault.
2. Deploy with `ASR_PROVIDER=assemblyai` (default).
3. Watch logs from `audio_transcription` for:
   - `AssemblyAI transcription complete`
   - `Audio transcription skipped: missing AssemblyAI configuration`
   - `AssemblyAI transcription failed`
   - `AssemblyAI transcription timed out`
4. If needed, rollback by setting `ASR_PROVIDER=azure_openai` without reverting code.

## Production Gaps To Discuss

- No webhook callback yet. Polling is simpler and matches current worker behavior, but high audio volume may benefit from AssemblyAI webhooks.
- No customer-level usage metadata is attached yet. AssemblyAI supports usage tracking patterns, but this first pass keeps the helper minimal.
- No provider-specific metrics beyond logs. If this becomes high-volume, add counters for uploads, submit errors, completed transcripts, transcript errors, and timeouts.
- Voice Realtime still uses Azure Realtime. Moving realtime ASR to AssemblyAI Universal-3.5 Pro Realtime requires frontend/session architecture changes.

## Implementation Rating

Current implementation: **7.5/10**.

It is production-usable for existing stored-audio paths because the provider boundary is shared, configurable, tested, and preserves the existing best-effort failure behavior. It is not a 9/10 yet because it uses polling instead of webhooks, has only log-based observability, and does not migrate the separate realtime voice path.
