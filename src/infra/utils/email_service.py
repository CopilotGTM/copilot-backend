import os
from typing import Optional
import resend
from src.infra.utils.logger import get_logger

logger = get_logger()


class EmailService:
    """Email service using Resend for sending notifications to admins"""
    
    def __init__(self):
        self.resend_api_key = os.getenv("RESEND_API_KEY")
        self.from_email = os.getenv("FROM_EMAIL", "noreply@yourdomain.com")
        self.app_name = os.getenv("APP_NAME", "CopilotGTM")
        self.app_url = os.getenv("APP_URL", "http://localhost:3000")
        resend.api_key = self.resend_api_key

        # Initialize Resend client
        if self.resend_api_key:
            self.is_configured = True
        else:
            self.resend = None
            self.is_configured = False
            logger.warning("Resend API key not configured. Email functionality will be disabled.")
    
    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None
    ) -> bool:
        """
        Send an email using Resend
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            html_content: HTML content of the email
            text_content: Plain text content (optional)
            
        Returns:
            True if email sent successfully, False otherwise
        """
        if not self.is_configured:
            logger.error("Email service not configured. Cannot send email.")
            return False
        
        try:
            # Prepare email data
            email_data = {
                "from": self.from_email,
                "to": [to_email],
                "subject": subject,
                "html": html_content
            }
            
            # Add text content if provided
            if text_content:
                email_data["text"] = text_content
            
            # Send email via Resend
            response = resend.Emails.send(email_data)
            
            logger.info(f"Email sent successfully to {to_email} via Resend")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {str(e)}")
            return False
    
    def send_super_admin_creation_email(
        self,
        to_email: str,
        first_name: str,
        temp_password: str
    ) -> bool:
        """
        Send email to newly created super admin with temporary password
        
        Args:
            to_email: Admin email address
            first_name: Admin's first name
            temp_password: Temporary password
            
        Returns:
            True if email sent successfully, False otherwise
        """
        subject = f"Welcome to {self.app_name} - Super Admin Account Created"
        
        html_content = f"""
        <html>
        <body>
            <h2>Welcome to {self.app_name}!</h2>
            <p>Hello {first_name},</p>
            <p>Your Super Admin account has been successfully created.</p>
            
            <h3>Your Login Credentials:</h3>
            <p><strong>Email:</strong> {to_email}</p>
            <p><strong>Temporary Password:</strong> {temp_password}</p>
            
            <p><strong>⚠️ Important:</strong> You will be required to change your password on your first login for security reasons.</p>
            
            <h3>Next Steps:</h3>
            <ol>
                <li>Visit the admin login page: <a href="{self.app_url}/admin/login">{self.app_url}/admin/login</a></li>
                <li>Login with your email and temporary password above</li>
                <li>You will be automatically redirected to change your password</li>
                <li>Set a strong, secure password</li>
                <li>Complete your account setup</li>
            </ol>
            
            <h3>Security Note:</h3>
            <p>This temporary password will expire after your first login. Please change it immediately for security.</p>
            
            <p>If you have any questions, please contact the system administrator.</p>
            
            <p>Best regards,<br>
            The {self.app_name} Team</p>
        </body>
        </html>
        """
        
        text_content = f"""
        Welcome to {self.app_name}!
        
        Hello {first_name},
        
        Your Super Admin account has been successfully created.
        
        Your Login Credentials:
        Email: {to_email}
        Temporary Password: {temp_password}
        
        ⚠️ Important: You will be required to change your password on your first login for security reasons.
        
        Next Steps:
        1. Visit the admin login page: {self.app_url}/admin/login
        2. Login with your email and temporary password above
        3. You will be automatically redirected to change your password
        4. Set a strong, secure password
        5. Complete your account setup
        
        Security Note:
        This temporary password will expire after your first login. Please change it immediately for security.
        
        If you have any questions, please contact the system administrator.
        
        Best regards,
        The {self.app_name} Team
        """
        
        return self.send_email(to_email, subject, html_content, text_content)
    
    def send_password_reset_email(
        self,
        to_email: str,
        first_name: str,
        reset_url: str
    ) -> bool:
        """
        Send password reset email to admin
        
        Args:
            to_email: Admin email address
            first_name: Admin's first name
            reset_url: Password reset URL
            
        Returns:
            True if email sent successfully, False otherwise
        """
        subject = f"{self.app_name} - Password Reset Request"
        
        html_content = f"""
        <html>
        <body>
            <h2>Password Reset Request</h2>
            <p>Hello {first_name},</p>
            <p>We received a request to reset your password for your {self.app_name} admin account.</p>
            
            <p>Click the link below to reset your password:</p>
            <p><a href="{reset_url}">Reset Password</a></p>
            
            <p>If you didn't request this password reset, please ignore this email.</p>
            
            <p>This link will expire in 24 hours for security reasons.</p>
            
            <p>Best regards,<br>
            The {self.app_name} Team</p>
        </body>
        </html>
        """
        
        text_content = f"""
        Password Reset Request
        
        Hello {first_name},
        
        We received a request to reset your password for your {self.app_name} admin account.
        
        Click the link below to reset your password:
        {reset_url}
        
        If you didn't request this password reset, please ignore this email.
        
        This link will expire in 24 hours for security reasons.
        
        Best regards,
        The {self.app_name} Team
        """
        
        return self.send_email(to_email, subject, html_content, text_content)


# Create global instance
email_service = EmailService() 