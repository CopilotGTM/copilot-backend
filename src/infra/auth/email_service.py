"""Platform email composition and provider-boundary error classification.

Authentication, signup, and notification workers call this service through one
process-wide sender. Configuration is resolved lazily, while provider responses
are classified so durable delivery workers can distinguish safe retries from an
unknown send outcome.
"""

import datetime
import os
from email.utils import formataddr, parseaddr
from html import escape as html_escape

import resend
from src.infra.config.configs import resend_config
from src.infra.utils.logger import get_logger

logger = get_logger()


class PlatformEmailSendError(RuntimeError):
    """Safe provider-boundary failure classification for durable notification workers."""

    def __init__(self, code: str, message: str, *, safe_to_retry: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.safe_to_retry = safe_to_retry


class EmailService:
    """Email service using Resend for sending notifications to admins.

    API key is resolved lazily through the TTL-aware config: env var first,
    then Key Vault.
    """

    def __init__(self):
        self.app_name = os.getenv("APP_NAME", "CopilotGTM")
        configured_sender = os.getenv("FROM_EMAIL", "noreply@copilotgtm.com")
        display_name, address = parseaddr(configured_sender)
        # Add the product name only to a bare address; an explicitly configured
        # display name is part of the deployment contract and must be preserved.
        self.from_email = (
            configured_sender if display_name else formataddr((self.app_name, address))
        )
        self.app_url = os.getenv("APP_URL", "https://app.copilotgtm.com")
        self.admin_app_url = os.getenv("ADMIN_APP_URL", "https://admin.copilotgtm.com")
        self.web_app_url = os.getenv("WEB_APP_URL", "https://www.copilotgtm.com")
        self._configured: bool | None = None  # None = not yet attempted

    async def _ensure_configured(self) -> bool:
        """Apply the current TTL-aware Resend key before each provider call."""
        previously_configured = self._configured
        api_key = await resend_config.get_api_key()
        if api_key:
            resend.api_key = api_key
            self._configured = True
            if previously_configured is not True:
                logger.info("Resend API key configured successfully")
        else:
            self._configured = False
            if previously_configured is not False:
                logger.warning(
                    "Resend API key not configured. Email functionality will be disabled."
                )

        return self._configured

    @property
    def is_configured(self) -> bool:
        """Backward-compatible sync check. Returns False if not yet resolved."""
        return self._configured is True

    def _create_styled_html(self, title: str, preheader: str, content: str) -> str:
        """Creates a styled HTML email body."""
        current_year = datetime.datetime.now().year
        # Header text is caller-controlled; content is already assembled as the
        # service's intended safe HTML and must keep its formatting.
        safe_title = html_escape(title)
        safe_preheader = html_escape(preheader)
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{safe_title}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; }}
                .container {{ max-width: 600px; margin: 20px auto; padding: 20px; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .header {{ text-align: center; padding-bottom: 20px; border-bottom: 1px solid #dddddd; }}
                .header h1 {{ color: #333333; }}
                .content {{ padding: 20px 0; color: #555555; }}
                .content p {{ line-height: 1.6; }}
                .credentials {{ background-color: #f9f9f9; border: 1px solid #eeeeee; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                .credentials p {{ margin: 5px 0; }}
                .button-container {{ text-align: center; margin: 20px 0; }}
                .button {{ background-color: #007bff; color: #ffffff; padding: 12px 25px; text-decoration: none; border-radius: 5px; display: inline-block; }}
                .footer {{ text-align: center; font-size: 12px; color: #999999; padding-top: 20px; border-top: 1px solid #dddddd; }}
                .hidden-preheader {{ display: none; font-size: 1px; color: #ffffff; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden; }}
            </style>
        </head>
        <body>
            <div class="hidden-preheader">{safe_preheader}</div>
            <div class="container">
                <div class="header">
                    <h1>{self.app_name}</h1>
                </div>
                <div class="content">
                    {content}
                </div>
                <div class="footer">
                    <p>&copy; {current_year} {self.app_name}. All rights reserved.</p>
                    <p>If you did not request this email, please ignore it.</p>
                </div>
            </div>
        </body>
        </html>
        """

    async def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str | None = None,
        cc: list[str] | None = None,
    ) -> bool:
        """Send an email using Resend."""
        try:
            # Keep the legacy boolean API for existing callers while routing the
            # actual send through the receipt-aware error classification boundary.
            await self.send_email_with_receipt(
                to_email=to_email,
                subject=subject,
                html_content=html_content,
                text_content=text_content,
                cc=cc,
            )
            return True
        except RuntimeError:
            return False

    async def send_email_with_receipt(
        self,
        *,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str | None = None,
        cc: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Send through the platform account and return the provider resource ID."""

        if not await self._ensure_configured():
            # A missing deployment secret is retryable because configuration may
            # arrive before the durable delivery's next attempt.
            logger.error("Email service is not configured")
            raise PlatformEmailSendError(
                "platform_email_not_configured",
                "Platform email is not configured",
                safe_to_retry=True,
            )
        email_data: resend.Emails.SendParams = {
            "from": self.from_email,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }
        if text_content:
            email_data["text"] = text_content
        if cc:
            email_data["cc"] = cc
        try:
            # The delivery ledger supplies this key on retries so the provider can
            # deduplicate a repeated request instead of sending a second message.
            response = (
                resend.Emails.send(
                    email_data,
                    {"idempotency_key": idempotency_key},
                )
                if idempotency_key
                else resend.Emails.send(email_data)
            )
            provider_id = (
                response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
            )
            if not provider_id:
                raise PlatformEmailSendError(
                    "delivery_outcome_unknown",
                    "Provider outcome is unknown",
                    safe_to_retry=False,
                )
            logger.info("Transactional email accepted by provider")
            return str(provider_id)
        except PlatformEmailSendError:
            logger.error("Transactional email provider outcome is unknown")
            raise
        except Exception as exc:
            logger.error("Transactional email provider request failed")
            raise PlatformEmailSendError(
                "delivery_outcome_unknown",
                "Provider outcome is unknown",
                safe_to_retry=False,
            ) from exc

    async def send_super_admin_creation_email(
        self, to_email: str, first_name: str, temp_password: str
    ) -> bool:
        """Send email to newly created super admin with temporary password"""
        subject = f"Welcome to {self.app_name} - Super Admin Account Created"
        preheader = "Your super admin account is ready."

        safe_name = html_escape(first_name)
        safe_email = html_escape(to_email)
        safe_password = html_escape(temp_password)

        main_content = f"""
            <h2>Welcome, {safe_name}!</h2>
            <p>Your Super Admin account for {self.app_name} has been successfully created.</p>

            <div class="credentials">
                <h3>Your Login Credentials:</h3>
                <p><strong>Email:</strong> {safe_email}</p>
                <p><strong>Temporary Password:</strong> <code>{safe_password}</code></p>
            </div>

            <p><strong>⚠️ Important:</strong> You will be required to change this password upon your first login for security reasons.</p>

            <div class="button-container">
                <a href="{self.admin_app_url}/signin" class="button">Login to Your Account</a>
            </div>

            <h3>Next Steps:</h3>
            <ol>
                <li>Click the button above to visit the admin login page.</li>
                <li>Log in with your email and the temporary password provided.</li>
                <li>You will be prompted to set a new, secure password.</li>
                <li>Complete your account setup.</li>
            </ol>

            <p>If you have any questions, please contact our support team.</p>
            <p>Best regards,<br>The {self.app_name} Team</p>
        """

        html_content = self._create_styled_html(subject, preheader, main_content)

        text_content = f"""
        Welcome to {self.app_name}, {first_name}!
        Your Super Admin account has been created.
        Login with Email: {to_email} and Temporary Password: {temp_password}
        Login here: {self.admin_app_url}/signin
        You will be required to change your password upon first login.
        """

        return await self.send_email(to_email, subject, html_content, text_content)

    async def send_user_creation_email(
        self, to_email: str, first_name: str, temp_password: str
    ) -> bool:
        """Send email to newly created user with temporary password"""
        subject = f"Welcome to {self.app_name} - Your Account is Ready"
        preheader = "Your new account has been successfully created."

        safe_name = html_escape(first_name)
        safe_email = html_escape(to_email)
        safe_password = html_escape(temp_password)

        main_content = f"""
            <h2>Welcome, {safe_name}!</h2>
            <p>Your account for {self.app_name} has been successfully created.</p>

            <div class="credentials">
                <h3>Your Login Credentials:</h3>
                <p><strong>Email:</strong> {safe_email}</p>
                <p><strong>Temporary Password:</strong> <code>{safe_password}</code></p>
            </div>

            <p><strong>⚠️ Important:</strong> You will be required to change this password upon your first login for security reasons.</p>

            <div class="button-container">
                <a href="{self.app_url}/signin" class="button">Login to Your Account</a>
            </div>

            <h3>Next Steps:</h3>
            <ol>
                <li>Click the button above to visit the login page.</li>
                <li>Log in with your email and the temporary password provided.</li>
                <li>You will be prompted to set a new, secure password.</li>
            </ol>

            <p>If you have any questions, please contact our support team.</p>
            <p>Best regards,<br>The {self.app_name} Team</p>
        """

        html_content = self._create_styled_html(subject, preheader, main_content)

        text_content = f"""
        Welcome to {self.app_name}, {first_name}!
        Your account has been created.
        Login with Email: {to_email} and Temporary Password: {temp_password}
        Login here: {self.app_url}/signin
        You will be required to change your password upon first login.
        """

        return await self.send_email(to_email, subject, html_content, text_content)

    async def send_external_auth_invitation_email(
        self,
        to_email: str,
        first_name: str,
        provider_name: str,
    ) -> bool:
        """Invite an approved user through their configured OAuth provider."""
        safe_name = html_escape(first_name)
        safe_provider = html_escape(provider_name)
        subject = f"Welcome to {self.app_name} - Continue with {safe_provider}"
        content = f"""
            <h2>Welcome, {safe_name}!</h2>
            <p>Your {self.app_name} account is ready.</p>
            <p>Use your company {safe_provider} account. A password is not enabled for this account.</p>
            <div class="button-container">
                <a href="{self.app_url}/signin" class="button">Continue with {safe_provider}</a>
            </div>
        """
        text = (
            f"Welcome to {self.app_name}, {first_name}. "
            f"Sign in at {self.app_url}/signin using Continue with {provider_name}."
        )
        return await self.send_email(
            to_email,
            subject,
            self._create_styled_html(subject, f"Continue with {safe_provider}.", content),
            text,
        )

    async def send_domain_member_joined_email(
        self,
        to_email: str,
        member_email: str,
        organisation_name: str,
    ) -> bool:
        """Tell an organisation admin that a verified same-domain member joined."""
        subject = f"A teammate joined {organisation_name}"
        text = f"{member_email} joined your {organisation_name} workspace as a member."
        safe_text = html_escape(text)
        return await self.send_email(
            to_email,
            subject,
            self._create_styled_html(subject, text, f"<p>{safe_text}</p>"),
            text,
        )

    async def send_domain_access_attempt_email(
        self,
        to_email: str,
        requester_email: str,
        organisation_name: str,
    ) -> bool:
        """Tell an organisation admin that invite-only policy blocked a teammate."""
        subject = f"A teammate requested access to {organisation_name}"
        text = (
            f"{requester_email} tried to join your workspace. "
            f"Invite them from {self.app_url}/settings/members if they should have access."
        )
        safe_text = html_escape(text)
        return await self.send_email(
            to_email,
            subject,
            self._create_styled_html(subject, text, f"<p>{safe_text}</p>"),
            text,
        )

    async def send_signup_welcome_email(self, to_email: str, first_name: str) -> bool:
        """Welcome a provider-backed founder after their organisation is active."""
        safe_name = html_escape(first_name)
        subject = f"Welcome to {self.app_name}"
        content = f"""
            <h2>Welcome, {safe_name}!</h2>
            <p>Your {self.app_name} workspace is ready.</p>
            <div class="button-container">
                <a href="{self.app_url}/signin" class="button">Open Your Workspace</a>
            </div>
        """
        text = f"Welcome to {self.app_name}, {first_name}. Open {self.app_url}/signin to continue."
        return await self.send_email(
            to_email,
            subject,
            self._create_styled_html(subject, "Your workspace is ready.", content),
            text,
        )

    async def send_task_assignment_email(
        self,
        to_email: str,
        assignee_name: str | None,
        assigner_name: str,
        task_subject: str,
        task_body: str | None = None,
        task_priority: str | None = None,
        task_due_date: str | None = None,
        task_status: str | None = None,
        deal_name: str | None = None,
        cc_email: str | None = None,
        is_update: bool = False,
    ) -> bool:
        """Send task assignment or update notification to a task assignee."""
        action = "Updated" if is_update else "Assigned"
        subject = f"{self.app_name} - Task {action}: {task_subject}"
        preheader = f"{assigner_name} {'updated a task assigned to you' if is_update else 'assigned you a task'}"

        safe_assignee = html_escape(assignee_name or "there")
        safe_assigner = html_escape(assigner_name)
        safe_subject = html_escape(task_subject)

        # Build detail rows as a clean table
        detail_rows_html = ""
        details: list[tuple[str, str]] = []
        if task_status:
            label = task_status.replace("_", " ").title()
            details.append(("Status", label))
        if task_priority and task_priority != "NONE":
            details.append(("Priority", task_priority.title()))
        if task_due_date:
            details.append(("Due Date", task_due_date))
        if deal_name:
            details.append(("Deal", deal_name))

        if details:
            rows = "".join(
                f'<tr><td style="padding:6px 12px 6px 0;color:#888;font-size:13px;">'
                f"{html_escape(label)}</td>"
                f'<td style="padding:6px 0;font-size:13px;font-weight:500;">'
                f"{html_escape(value)}</td></tr>"
                for label, value in details
            )
            detail_rows_html = (
                f'<table style="border-collapse:collapse;margin:12px 0 16px;">{rows}</table>'
            )

        body_section = ""
        if task_body:
            safe_body = html_escape(task_body).replace("\n", "<br>")
            body_section = (
                f'<div style="background-color:#f8f9fa;border-left:3px solid #007bff;'
                f'padding:12px 16px;border-radius:4px;margin:12px 0;">'
                f'<p style="margin:0 0 4px;font-size:12px;color:#888;font-weight:600;">'
                f"DESCRIPTION</p>"
                f'<p style="margin:0;font-size:14px;line-height:1.6;color:#333;">'
                f"{safe_body}</p></div>"
            )

        heading = (
            f"<strong>{safe_assigner}</strong> has updated a task assigned to you:"
            if is_update
            else f"<strong>{safe_assigner}</strong> has assigned you a task:"
        )

        main_content = f"""
            <h2 style="margin-bottom:4px;">Hi {safe_assignee},</h2>
            <p style="color:#555;">{heading}</p>

            <div style="background-color:#f9f9f9;border:1px solid #eee;padding:16px;border-radius:6px;margin:16px 0;">
                <h3 style="margin:0 0 8px;font-size:16px;">{safe_subject}</h3>
                {detail_rows_html}
            </div>

            {body_section}

            <p style="margin-top:20px;">If you have any questions, please reach out to <strong>{safe_assigner}</strong>.</p>
            <p>Best regards,<br>The {self.app_name} Team</p>
        """

        html_content = self._create_styled_html(subject, preheader, main_content)

        # Plain text fallback
        status_text = f" | Status: {task_status}" if task_status else ""
        priority_text = (
            f" | Priority: {task_priority}" if task_priority and task_priority != "NONE" else ""
        )
        due_text = f" | Due: {task_due_date}" if task_due_date else ""
        deal_text = f" | Deal: {deal_name}" if deal_name else ""
        body_text = f"\n\nDescription:\n{task_body}" if task_body else ""

        verb = "updated a task assigned to you" if is_update else "assigned you a task"
        text_content = (
            f"Hi {assignee_name or 'there'},\n\n"
            f"{assigner_name} {verb}: {task_subject}"
            f"{status_text}{priority_text}{due_text}{deal_text}"
            f"{body_text}\n\n"
            f"If you have any questions, please reach out to {assigner_name}."
        )

        cc = [cc_email] if cc_email else None
        return await self.send_email(to_email, subject, html_content, text_content, cc=cc)

    async def send_password_reset_email(
        self, to_email: str, first_name: str, reset_url: str
    ) -> bool:
        """Send password reset email to a user."""
        subject = f"{self.app_name} - Password Reset Request"
        preheader = "A request to reset your password was received."

        safe_name = html_escape(first_name)
        safe_url = html_escape(reset_url)

        main_content = f"""
            <h2>Password Reset Request</h2>
            <p>Hello {safe_name},</p>
            <p>We received a request to reset the password for your {self.app_name} account.</p>

            <p>If you did not make this request, you can safely ignore this email.</p>

            <div class="button-container">
                <a href="{safe_url}" class="button">Reset Your Password</a>
            </div>

            <p>This password reset link is valid for the next 24 hours.</p>

            <p>If you're having trouble with the button, copy and paste the URL below into your web browser:</p>
            <p><a href="{safe_url}">{safe_url}</a></p>

            <p>Best regards,<br>The {self.app_name} Team</p>
        """

        html_content = self._create_styled_html(subject, preheader, main_content)

        text_content = f"""
        Password Reset Request for {self.app_name}
        Hello {first_name},
        A request was made to reset your password. Click the link to proceed: {reset_url}
        If you did not request this, please ignore this email. The link will expire in 24 hours.
        """

        return await self.send_email(to_email, subject, html_content, text_content)


# Share one lazily configured sender so all email callers use the same branding
# and provider configuration without performing setup during module import.
email_service = EmailService()
