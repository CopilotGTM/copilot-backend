import datetime
import os
from html import escape as html_escape

import resend
from src.infra.utils.logger import get_logger

logger = get_logger()


class EmailService:
    """Email service using Resend for sending notifications to admins"""

    def __init__(self):
        self.resend_api_key = os.getenv("RESEND_API_KEY")
        self.from_email = os.getenv("FROM_EMAIL", "noreply@copilotgtm.com")
        self.app_name = os.getenv("APP_NAME", "CopilotGTM")
        self.app_url = os.getenv("APP_URL", "https://app.copilotgtm.com")
        self.admin_app_url = os.getenv("ADMIN_APP_URL", "https://admin.copilotgtm.com")
        self.web_app_url = os.getenv("WEB_APP_URL", "https://www.copilotgtm.com")

        # Initialize Resend client
        if self.resend_api_key:
            resend.api_key = self.resend_api_key
            self.is_configured = True
        else:
            self.is_configured = False
            logger.warning("Resend API key not configured. Email functionality will be disabled.")

    def _create_styled_html(self, title: str, preheader: str, content: str) -> str:
        """Creates a styled HTML email body."""
        current_year = datetime.datetime.now().year
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{title}</title>
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
            <div class="hidden-preheader">{preheader}</div>
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

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str | None = None,
    ) -> bool:
        """
        Send an email using Resend
        """
        if not self.is_configured:
            logger.error("Email service not configured. Cannot send email.")
            return False

        try:
            email_data = {
                "from": self.from_email,
                "to": [to_email],
                "subject": subject,
                "html": html_content,
            }
            if text_content:
                email_data["text"] = text_content

            resend.Emails.send(email_data)
            logger.info(f"Email sent successfully to {to_email} via Resend")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {str(e)}")
            return False

    def send_super_admin_creation_email(
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

        return self.send_email(to_email, subject, html_content, text_content)

    def send_user_creation_email(self, to_email: str, first_name: str, temp_password: str) -> bool:
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

        return self.send_email(to_email, subject, html_content, text_content)

    def send_password_reset_email(self, to_email: str, first_name: str, reset_url: str) -> bool:
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

        return self.send_email(to_email, subject, html_content, text_content)


# Create global instance
email_service = EmailService()
