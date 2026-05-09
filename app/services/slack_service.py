"""Slack service using slack_sdk WebClient for employee onboarding."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.utils.logger import get_logger

log = get_logger(__name__)


class SlackService:
    """Manages Slack workspace operations for new hire onboarding."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    @property
    def client(self):
        """Lazily initialise the slack_sdk WebClient."""
        if self._client is None:
            from slack_sdk import WebClient  # type: ignore

            self._client = WebClient(token=self.settings.slack_bot_token)
        return self._client

    # ── Workspace invitation ──────────────────────────────────────────────────

    def invite_to_workspace(
        self, email: str, first_name: str, last_name: str
    ) -> str | None:
        """Invite a new hire to the Slack workspace.

        Slack's free-tier scoped bots cannot directly create users — the standard
        pattern is to use ``admin.users.invite`` (requires Enterprise Grid) or
        to look up an existing user. This method attempts lookup first, then invite.

        Args:
            email: New hire's email address.
            first_name: New hire's first name.
            last_name: New hire's last name.

        Returns:
            Slack user ID string, or None if the invitation could not be sent.
        """
        # Try to find existing user by email first
        existing_id = self.get_user_by_email(email)
        if existing_id:
            log.info("Slack user already exists: {id}", id=existing_id)
            return existing_id

        # Attempt admin invite (requires admin:users:write scope)
        try:
            response = self.client.admin_users_invite(
                team_id=self._get_team_id(),
                email=email,
                channel_ids=[],
                real_name=f"{first_name} {last_name}",
            )
            user_id = response.get("user_id") or response.get("id")
            log.info("Invited {email} to Slack workspace, user_id={id}", email=email, id=user_id)
            return user_id
        except Exception as exc:
            log.warning(
                "admin_users_invite failed (may need Enterprise Grid): {exc}. "
                "Falling back to standard invite.",
                exc=exc,
            )

        # Standard invite via users.admin.invite (legacy, some workspaces)
        try:
            response = self.client.api_call(
                "users.admin.invite",
                params={
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "set_active": True,
                },
            )
            if response.get("ok"):
                log.info("Sent standard Slack invite to {email}", email=email)
                # Fetch user ID after invite
                return self.get_user_by_email(email)
        except Exception as exc:
            log.warning("Standard Slack invite also failed: {exc}", exc=exc)

        return None

    def get_user_by_email(self, email: str) -> str | None:
        """Look up a Slack user ID by email address.

        Args:
            email: Email address to search.

        Returns:
            Slack user ID string or None if not found.
        """
        try:
            response = self.client.users_lookupByEmail(email=email)
            user_id = response["user"]["id"]
            log.debug("Found Slack user {id} for {email}", id=user_id, email=email)
            return user_id
        except Exception as exc:
            log.debug("Slack user not found for {email}: {exc}", email=email, exc=exc)
            return None

    # ── Channel membership ────────────────────────────────────────────────────

    def add_to_channels(self, user_id: str, channels: list[str]) -> list[str]:
        """Add a user to one or more Slack channels.

        Args:
            user_id: Slack user ID.
            channels: List of channel names (e.g. '#general') or channel IDs.

        Returns:
            List of channel IDs successfully joined.
        """
        joined: list[str] = []
        for channel in channels:
            channel_id = self._resolve_channel(channel)
            if not channel_id:
                log.warning("Could not resolve channel: {ch}", ch=channel)
                continue
            try:
                self.client.conversations_invite(channel=channel_id, users=user_id)
                joined.append(channel_id)
                log.info("Added user {uid} to channel {ch}", uid=user_id, ch=channel)
            except Exception as exc:
                log.warning(
                    "Failed to add user {uid} to channel {ch}: {exc}",
                    uid=user_id,
                    ch=channel,
                    exc=exc,
                )
        return joined

    # ── Direct messages ────────────────────────────────────────────────────────

    def send_welcome_dm(self, user_id: str, employee: Any) -> bool:
        """Send a formatted welcome DM to a new hire.

        Args:
            user_id: Slack user ID of the new hire.
            employee: Employee ORM model instance.

        Returns:
            True on success, False on error.
        """
        message = self._build_welcome_message(employee)
        try:
            response = self.client.chat_postMessage(
                channel=user_id,
                blocks=message["blocks"],
                text=message["fallback"],
            )
            log.info("Welcome DM sent to user {uid}", uid=user_id)
            return response.get("ok", False)
        except Exception as exc:
            log.error("Failed to send welcome DM to {uid}: {exc}", uid=user_id, exc=exc)
            return False

    # ── Private helpers ────────────────────────────────────────────────────────

    def _get_team_id(self) -> str:
        """Fetch the workspace team ID."""
        try:
            info = self.client.team_info()
            return info["team"]["id"]
        except Exception:
            return ""

    def _resolve_channel(self, channel: str) -> str | None:
        """Resolve a channel name or ID to a channel ID."""
        # If it already looks like an ID (Cxxxxxxxx)
        if channel.startswith("C") and len(channel) > 6:
            return channel

        # Strip leading #
        name = channel.lstrip("#")
        try:
            cursor = None
            while True:
                kwargs: dict[str, Any] = {"limit": 200, "exclude_archived": True}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self.client.conversations_list(**kwargs)
                for ch in response.get("channels", []):
                    if ch["name"] == name:
                        return ch["id"]
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as exc:
            log.warning("Channel resolution failed for {ch}: {exc}", ch=channel, exc=exc)
        return None

    @staticmethod
    def _build_welcome_message(employee: Any) -> dict[str, Any]:
        """Build a Block Kit welcome message for Slack."""
        full_name = employee.full_name
        job_title = employee.job_title or "Team Member"
        department = employee.department or "Your Team"
        start_date = str(employee.start_date) if employee.start_date else "your start date"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Welcome to the team, {employee.first_name}!"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Hi *{full_name}*! We're thrilled to have you joining as *{job_title}* "
                        f"in the *{department}* department starting *{start_date}*.\n\n"
                        "Here are a few things to get you started:"
                    ),
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": ":email: Check your email for onboarding documents"},
                    {"type": "mrkdwn", "text": ":jira: Your Jira account has been set up"},
                    {"type": "mrkdwn", "text": ":slack: You've been added to team channels"},
                    {"type": "mrkdwn", "text": ":calendar: IT setup guide is in your inbox"},
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "_This message was sent by the HR Onboarding Bot_"}
                ],
            },
        ]
        return {
            "blocks": blocks,
            "fallback": f"Welcome to the team, {full_name}! We're glad to have you.",
        }
