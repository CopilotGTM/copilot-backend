"""Tests for confirmation card nonce utility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.service.bots.form_handlers.nonce import create_nonce, validate_and_consume_nonce


class TestCreateNonce:
    """Tests for create_nonce."""

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_creates_nonce_and_stores_in_redis(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_get_redis.return_value = mock_client

        nonce = create_nonce("deal_confirm")

        assert isinstance(nonce, str)
        assert len(nonce) == 36  # UUID4 format
        mock_client.set.assert_called_once()
        call_args = mock_client.set.call_args
        assert call_args[0][0] == f"deal_confirm:{nonce}"
        assert call_args[0][1] == "1"
        assert call_args[1]["ex"] == 900

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_custom_ttl(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_get_redis.return_value = mock_client

        create_nonce("deal_confirm", ttl=300)

        call_args = mock_client.set.call_args
        assert call_args[1]["ex"] == 300


class TestValidateAndConsumeNonce:
    """Tests for validate_and_consume_nonce."""

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_valid_nonce_consumed(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.delete.return_value = 1  # Key existed and was deleted
        mock_get_redis.return_value = mock_client

        result = validate_and_consume_nonce("deal_confirm", "abc-123")

        assert result is True
        mock_client.delete.assert_called_once_with("deal_confirm:abc-123")

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_expired_nonce_rejected(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.delete.return_value = 0  # Key did not exist
        mock_get_redis.return_value = mock_client

        result = validate_and_consume_nonce("deal_confirm", "expired-123")

        assert result is False

    @patch("src.service.bots.form_handlers.nonce.get_redis_client")
    def test_redis_error_returns_false(self, mock_get_redis: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.delete.side_effect = Exception("Redis down")
        mock_get_redis.return_value = mock_client

        result = validate_and_consume_nonce("deal_confirm", "abc-123")

        assert result is False
