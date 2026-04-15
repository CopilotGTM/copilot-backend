from src.service.voice_realtime.transport import build_sideband_websocket_url


def test_build_sideband_websocket_url_uses_call_id_without_model_for_attached_call() -> None:
    url = build_sideband_websocket_url(
        endpoint="https://example.openai.azure.com",
        call_id="rtc_abc123",
    )

    assert url == "wss://example.openai.azure.com/openai/v1/realtime?call_id=rtc_abc123"
