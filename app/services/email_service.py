"""Email service for employee onboarding notifications."""

from __future__ import annotations

import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from app.config import Settings
from app.utils.logger import get_logger

log = get_logger(__name__)


class EmailService:
    """Handles all email delivery for the onboarding workflow."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── Public methods ────────────────────────────────────────────────────────

    def send_welcome_email(self, employee: Any, attachments: list[str] | None = None) -> None:
        """Send a rich HTML welcome email with optional document attachments.

        Args:
            employee: Employee ORM model instance.
            attachments: List of absolute file paths to attach (e.g. offer letter).
        """
        subject = f"Welcome to {self.settings.company_name}, {employee.first_name}!"
        html_body = self._build_welcome_html(employee)
        self._send(
            to_email=employee.email,
            subject=subject,
            html_body=html_body,
            attachments=attachments or [],
        )
        log.info("Welcome email sent to {email}", email=employee.email)

    def send_manager_notification(self, manager_email: str, employee: Any) -> None:
        """Notify the hiring manager that onboarding has been initiated.

        Args:
            manager_email: Manager's email address.
            employee: Employee ORM model instance.
        """
        subject = f"Onboarding initiated: {employee.full_name} starts {employee.start_date or 'soon'}"
        html_body = self._build_manager_notification_html(employee)
        self._send(to_email=manager_email, subject=subject, html_body=html_body)
        log.info(
            "Manager notification sent to {mgr} for {emp}",
            mgr=manager_email,
            emp=employee.full_name,
        )

    def send_it_setup_instructions(self, employee_email: str, docs_paths: list[str]) -> None:
        """Email IT setup documentation to the new hire.

        Args:
            employee_email: New hire's email address.
            docs_paths: List of absolute file paths to attach.
        """
        subject = "IT Setup Instructions — Action Required"
        html_body = self._build_it_instructions_html()
        self._send(
            to_email=employee_email,
            subject=subject,
            html_body=html_body,
            attachments=docs_paths,
        )
        log.info("IT setup instructions sent to {email}", email=employee_email)

    # ── SMTP sender ───────────────────────────────────────────────────────────

    def _send(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        attachments: list[str] | None = None,
    ) -> None:
        """Send an email via STARTTLS SMTP with optional file attachments."""
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = self.settings.from_email
        msg["To"] = to_email

        # HTML body
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        # Attachments
        for file_path in (attachments or []):
            path = Path(file_path)
            if not path.exists():
                log.warning("Attachment not found, skipping: {path}", path=path)
                continue
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=path.name)
            msg.attach(part)

        context = ssl.create_default_context()
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(self.settings.smtp_user, self.settings.smtp_pass)
            server.sendmail(self.settings.from_email, to_email, msg.as_string())

    # ── HTML builders ─────────────────────────────────────────────────────────

    def _build_welcome_html(self, employee: Any) -> str:
        start_date = str(employee.start_date) if employee.start_date else "your start date"
        company = self.settings.company_name
        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 20px; margin: 0; }}
    .container {{ max-width: 640px; margin: 0 auto; background: #fff;
                  border-radius: 8px; overflow: hidden;
                  box-shadow: 0 2px 12px rgba(0,0,0,.1); }}
    .header {{ background: #1a73e8; color: #fff; padding: 32px 40px; }}
    .header h1 {{ margin: 0; font-size: 26px; }}
    .body {{ padding: 32px 40px; }}
    .checklist {{ background: #f8f9fa; border-radius: 6px; padding: 20px; margin: 20px 0; }}
    .checklist li {{ margin: 8px 0; }}
    .footer {{ padding: 16px 40px; background: #f4f6f9;
               font-size: 12px; color: #888; text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Welcome to {company}!</h1>
    </div>
    <div class="body">
      <p>Hi <strong>{employee.first_name}</strong>,</p>
      <p>We're delighted to have you joining us as <strong>{employee.job_title or 'a new team member'}</strong>
         in the <strong>{employee.department or 'team'}</strong> on <strong>{start_date}</strong>.</p>

      <h3>Your Onboarding Checklist</h3>
      <div class="checklist">
        <ul>
          <li>&#9989; Your accounts have been provisioned (email, Slack, Jira)</li>
          <li>&#9989; Onboarding documents are attached to this email</li>
          <li>&#9744; Complete your IT setup guide (attached)</li>
          <li>&#9744; Schedule a meeting with your manager: {employee.manager_email or 'to be arranged'}</li>
          <li>&#9744; Enroll in benefits within your first 30 days</li>
          <li>&#9744; Complete mandatory security awareness training</li>
        </ul>
      </div>

      <p>If you have any questions before your start date, please reach out to HR
         at <a href="mailto:{self.settings.from_email}">{self.settings.from_email}</a>.</p>

      <p>We can't wait to work with you!</p>
      <p><em>The {company} HR Team</em></p>
    </div>
    <div class="footer">
      This is an automated message from the {company} HR Onboarding System.
    </div>
  </div>
</body>
</html>"""

    def _build_manager_notification_html(self, employee: Any) -> str:
        start_date = str(employee.start_date) if employee.start_date else "soon"
        company = self.settings.company_name
        return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f6f9;padding:20px;">
  <div style="max-width:580px;margin:0 auto;background:#fff;border-radius:8px;padding:32px;
              box-shadow:0 2px 8px rgba(0,0,0,.1);">
    <h2 style="color:#1a73e8;">New Team Member Onboarding Initiated</h2>
    <p>Hi,</p>
    <p>This is to notify you that onboarding has been initiated for your new report:</p>
    <table style="border-collapse:collapse;width:100%;margin:16px 0;">
      <tr><td style="padding:8px;font-weight:bold;background:#f8f9fa;">Name</td>
          <td style="padding:8px;">{employee.full_name}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;background:#f8f9fa;">Email</td>
          <td style="padding:8px;">{employee.email}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;background:#f8f9fa;">Department</td>
          <td style="padding:8px;">{employee.department or 'N/A'}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;background:#f8f9fa;">Job Title</td>
          <td style="padding:8px;">{employee.job_title or 'N/A'}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;background:#f8f9fa;">Start Date</td>
          <td style="padding:8px;">{start_date}</td></tr>
    </table>
    <p>System accounts (email, Slack, Jira) have been provisioned and a welcome email
       has been sent to the new hire. Please schedule an introductory meeting.</p>
    <p><em>{company} HR Onboarding System</em></p>
  </div>
</body>
</html>"""

    def _build_it_instructions_html(self) -> str:
        company = self.settings.company_name
        return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f6f9;padding:20px;">
  <div style="max-width:580px;margin:0 auto;background:#fff;border-radius:8px;padding:32px;">
    <h2 style="color:#1a73e8;">IT Setup Instructions</h2>
    <p>Please find your IT setup guide attached. Follow the steps in order to
       get your workstation, accounts, and VPN configured before your first day.</p>
    <p>If you run into any issues, contact IT support at
       <a href="mailto:it@{company.lower().replace(' ', '')}.com">IT Help Desk</a>.</p>
    <p><em>{company} IT Department</em></p>
  </div>
</body>
</html>"""
