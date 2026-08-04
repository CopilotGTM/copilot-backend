"""Tests for EmailService with lazy Key Vault resolution."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.infra.auth.email_service import EmailService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "button_text"),
    [("Google", "Continue with Google"), ("Microsoft", "Continue with Microsoft")],
)
async def test_external_auth_invitation_names_the_required_provider(
    monkeypatch, provider_name, button_text
) -> None:
    service = EmailService()
    sent = {}

    async def capture_email(to_email, subject, html_content, text_content=None, cc=None):
        sent.update(
            subject=subject,
            html=html_content,
            text=text_content,
        )
        return True

    monkeypatch.setattr(service, "send_email", capture_email)

    assert await service.send_external_auth_invitation_email(
        to_email="person@acme.com",
        first_name="Person",
        provider_name=provider_name,
    )
    assert button_text in sent["html"]
    assert provider_name in sent["subject"]


@pytest.mark.asyncio
async def test_signup_welcome_links_to_signin_without_verification_or_credentials(
    monkeypatch,
) -> None:
    service = EmailService()
    sent = {}

    async def capture_email(to_email, subject, html_content, text_content=None, cc=None):
        sent.update(to=to_email, subject=subject, html=html_content, text=text_content)
        return True

    monkeypatch.setattr(service, "send_email", capture_email)

    assert await service.send_signup_welcome_email("founder@acme.com", "Ada")
    content = f"{sent['subject']} {sent['html']} {sent['text']}".lower()
    assert sent["to"] == "founder@acme.com"
    assert f"{service.app_url}/signin" in content
    assert "verify" not in content
    assert "password" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "expected_phrase"),
    [
        ("send_domain_member_joined_email", "joined your Acme workspace"),
        ("send_domain_access_attempt_email", "tried to join your workspace"),
    ],
)
async def test_domain_membership_notifications_name_the_requester(
    monkeypatch, method, expected_phrase
) -> None:
    service = EmailService()
    sent = {}

    async def capture_email(to_email, subject, html_content, text_content=None, cc=None):
        sent.update(to=to_email, subject=subject, html=html_content, text=text_content)
        return True

    monkeypatch.setattr(service, "send_email", capture_email)

    assert await getattr(service, method)(
        "admin@acme.com",
        "teammate@acme.com",
        "Acme",
    )
    assert sent["to"] == "admin@acme.com"
    assert "teammate@acme.com" in sent["text"]
    assert expected_phrase in sent["text"]


class TestEmailServiceLazyInit:
    """EmailService should not resolve API key at init time."""

    def test_no_warning_at_init(self, caplog):
        """No 'not configured' warning should appear during __init__."""
        with caplog.at_level("WARNING"):
            service = EmailService()
        assert "not configured" not in caplog.text
        assert service._configured is None

    def test_is_configured_false_before_first_use(self):
        service = EmailService()
        assert service.is_configured is False


class TestEmailServiceEnsureConfigured:
    """_ensure_configured resolves API key from resend_config."""

    @pytest.mark.asyncio
    async def test_configures_with_api_key_from_vault(self):
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value="vault-resend-key")
            with patch("src.infra.auth.email_service.resend") as mock_resend:
                result = await service._ensure_configured()

        assert result is True
        assert service._configured is True
        assert service.is_configured is True
        assert mock_resend.api_key == "vault-resend-key"

    @pytest.mark.asyncio
    async def test_not_configured_when_no_key(self):
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value=None)
            result = await service._ensure_configured()

        assert result is False
        assert service._configured is False
        assert service.is_configured is False

    @pytest.mark.asyncio
    async def test_only_resolves_once(self):
        """After first call, subsequent calls return cached result."""
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value="key-123")
            with patch("src.infra.auth.email_service.resend"):
                await service._ensure_configured()
                await service._ensure_configured()
                await service._ensure_configured()

        # get_api_key called only once
        mock_config.get_api_key.assert_called_once()


class TestEmailServiceSendEmail:
    """send_email triggers lazy config and sends via Resend."""

    @pytest.mark.asyncio
    async def test_send_email_success(self):
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value="key-123")
            with patch("src.infra.auth.email_service.resend") as mock_resend:
                mock_resend.Emails.send = MagicMock(return_value={"id": "email-1"})
                result = await service.send_email(
                    to_email="user@example.com",
                    subject="Test",
                    html_content="<p>Hello</p>",
                )

        assert result is True
        mock_resend.Emails.send.assert_called_once()
        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["to"] == ["user@example.com"]
        assert call_args["subject"] == "Test"
        assert call_args["html"] == "<p>Hello</p>"

    @pytest.mark.asyncio
    async def test_send_email_returns_false_when_not_configured(self):
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value=None)
            result = await service.send_email(
                to_email="user@example.com",
                subject="Test",
                html_content="<p>Hello</p>",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_send_email_returns_false_on_resend_error(self):
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value="key-123")
            with patch("src.infra.auth.email_service.resend") as mock_resend:
                mock_resend.Emails.send = MagicMock(side_effect=Exception("Resend API error"))
                result = await service.send_email(
                    to_email="user@example.com",
                    subject="Test",
                    html_content="<p>Hello</p>",
                )

        assert result is False

    @pytest.mark.asyncio
    async def test_send_email_includes_text_content(self):
        service = EmailService()

        with patch("src.infra.auth.email_service.resend_config") as mock_config:
            mock_config.get_api_key = AsyncMock(return_value="key-123")
            with patch("src.infra.auth.email_service.resend") as mock_resend:
                mock_resend.Emails.send = MagicMock(return_value={"id": "email-1"})
                await service.send_email(
                    to_email="user@example.com",
                    subject="Test",
                    html_content="<p>Hello</p>",
                    text_content="Hello plain",
                )

        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["text"] == "Hello plain"


class TestEmailServiceSendMethods:
    """High-level send methods delegate to send_email."""

    def _make_configured_service(self):
        service = EmailService()
        service._configured = True
        return service

    @pytest.mark.asyncio
    async def test_send_user_creation_email(self):
        service = self._make_configured_service()

        with patch("src.infra.auth.email_service.resend") as mock_resend:
            mock_resend.Emails.send = MagicMock(return_value={"id": "e1"})
            result = await service.send_user_creation_email(
                to_email="new@example.com",
                first_name="Alice",
                temp_password="t3mp!",
            )

        assert result is True
        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["to"] == ["new@example.com"]
        assert "Welcome" in call_args["subject"]
        assert "Alice" in call_args["html"]
        assert "t3mp!" in call_args["html"]

    @pytest.mark.asyncio
    async def test_send_password_reset_email(self):
        service = self._make_configured_service()

        with patch("src.infra.auth.email_service.resend") as mock_resend:
            mock_resend.Emails.send = MagicMock(return_value={"id": "e2"})
            result = await service.send_password_reset_email(
                to_email="user@example.com",
                first_name="Bob",
                reset_url="https://app.example.com/reset?token=abc",
            )

        assert result is True
        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["to"] == ["user@example.com"]
        assert "Reset" in call_args["subject"]
        assert "https://app.example.com/reset?token=abc" in call_args["html"]

    @pytest.mark.asyncio
    async def test_send_super_admin_creation_email(self):
        service = self._make_configured_service()

        with patch("src.infra.auth.email_service.resend") as mock_resend:
            mock_resend.Emails.send = MagicMock(return_value={"id": "e3"})
            result = await service.send_super_admin_creation_email(
                to_email="admin@example.com",
                first_name="Charlie",
                temp_password="s3cur3!",
            )

        assert result is True
        call_args = mock_resend.Emails.send.call_args[0][0]
        assert call_args["to"] == ["admin@example.com"]
        assert "Super Admin" in call_args["subject"]
        assert "Charlie" in call_args["html"]

    @pytest.mark.asyncio
    async def test_html_escapes_user_input(self):
        """Verify XSS-safe escaping of user-provided values."""
        service = self._make_configured_service()

        with patch("src.infra.auth.email_service.resend") as mock_resend:
            mock_resend.Emails.send = MagicMock(return_value={"id": "e4"})
            await service.send_user_creation_email(
                to_email="user@example.com",
                first_name='<script>alert("xss")</script>',
                temp_password='"><img src=x>',
            )

        call_args = mock_resend.Emails.send.call_args[0][0]
        html = call_args["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
