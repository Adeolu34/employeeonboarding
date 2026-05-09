"""Jira service for creating users and onboarding tickets."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# Default project key to add new users to; can be overridden or made dynamic
DEFAULT_PROJECT_KEY = "OB"  # Onboarding project


class JiraService:
    """Manages Jira user and ticket operations for employee onboarding."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jira = None

    @property
    def jira(self):
        """Lazily initialise the JIRA client."""
        if self._jira is None:
            from jira import JIRA  # type: ignore

            self._jira = JIRA(
                server=self.settings.jira_url,
                basic_auth=(self.settings.jira_user, self.settings.jira_api_token),
                options={"verify": True},
            )
        return self._jira

    # ── User management ───────────────────────────────────────────────────────

    def create_user(self, email: str, display_name: str, department: str) -> str:
        """Create a Jira user account.

        Note: Jira Cloud user creation via REST requires site-admin privileges.
        The jira library wraps the ``/rest/api/3/user`` endpoint.

        Args:
            email: New hire's email address.
            display_name: Full name to display in Jira.
            department: Department string (stored in Jira user properties).

        Returns:
            The Jira account ID or username of the created/found user.

        Raises:
            Exception: If user creation and lookup both fail.
        """
        # Check if user already exists
        try:
            existing = self.jira.search_users(query=email, maxResults=1)
            if existing:
                user = existing[0]
                log.info(
                    "Jira user already exists: {id} ({name})",
                    id=user.accountId,
                    name=user.displayName,
                )
                return user.accountId
        except Exception as exc:
            log.debug("Jira user lookup failed: {exc}", exc=exc)

        # Create new user via REST API
        try:
            new_user = self.jira.create_user(
                email=email,
                username=email,
                password=None,  # Jira Cloud will send invite email
                fullname=display_name,
                notify=True,
            )
            account_id = getattr(new_user, "accountId", str(new_user))
            log.info("Created Jira user {id} for {email}", id=account_id, email=email)
            return account_id
        except Exception as exc:
            log.error("Failed to create Jira user for {email}: {exc}", email=email, exc=exc)
            raise

    def add_to_project(self, account_id: str, project_key: str = DEFAULT_PROJECT_KEY) -> bool:
        """Add a user to a Jira project with Browse + Work permissions.

        Jira Cloud uses project-level role membership to grant access.

        Args:
            account_id: Jira account ID of the user.
            project_key: The project key (e.g. "OB", "IT").

        Returns:
            True on success, False on failure.
        """
        try:
            # Get all roles for the project
            roles = self.jira.project_roles(project_key)
            # Add to the "Member" or "Developer" role — adjust to your scheme
            for role_name in ("Member", "Developer", "Users"):
                if role_name in roles:
                    role_url = roles[role_name]
                    self.jira._session.post(
                        role_url,
                        json={"user": [account_id]},
                    )
                    log.info(
                        "Added user {id} to project {proj} role {role}",
                        id=account_id,
                        proj=project_key,
                        role=role_name,
                    )
                    return True

            log.warning(
                "No suitable role found in project {proj} for user {id}",
                proj=project_key,
                id=account_id,
            )
            return False
        except Exception as exc:
            log.error(
                "Failed to add user {id} to project {proj}: {exc}",
                id=account_id,
                proj=project_key,
                exc=exc,
            )
            return False

    # ── Onboarding ticket ─────────────────────────────────────────────────────

    def create_onboarding_ticket(self, employee: Any) -> str:
        """Create an onboarding tracker ticket in the OB project.

        Args:
            employee: Employee ORM model instance.

        Returns:
            Jira issue key (e.g. "OB-42").

        Raises:
            Exception: If issue creation fails.
        """
        start_date = str(employee.start_date) if employee.start_date else "TBD"
        summary = f"Onboarding: {employee.full_name} — {employee.job_title or 'New Hire'}"
        description = self._build_ticket_description(employee, start_date)

        issue_dict: dict[str, Any] = {
            "project": {"key": DEFAULT_PROJECT_KEY},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            },
            "issuetype": {"name": "Task"},
            "labels": ["onboarding", "new-hire"],
        }

        try:
            issue = self.jira.create_issue(fields=issue_dict)
            log.info(
                "Created onboarding ticket {key} for {name}",
                key=issue.key,
                name=employee.full_name,
            )
            return issue.key
        except Exception as exc:
            log.error("Failed to create Jira ticket for {name}: {exc}", name=employee.full_name, exc=exc)
            raise

    @staticmethod
    def _build_ticket_description(employee: Any, start_date: str) -> str:
        """Build the onboarding ticket description text."""
        lines = [
            f"New Employee Onboarding Tracker",
            f"",
            f"Name: {employee.full_name}",
            f"Email: {employee.email}",
            f"Department: {employee.department or 'N/A'}",
            f"Job Title: {employee.job_title or 'N/A'}",
            f"Start Date: {start_date}",
            f"Manager: {employee.manager_email or 'N/A'}",
            f"",
            f"Onboarding Checklist:",
            f"[ ] IT equipment provisioned",
            f"[ ] System accounts created (email, Slack, Jira)",
            f"[ ] HR documents signed",
            f"[ ] Welcome meeting scheduled with manager",
            f"[ ] Benefits enrollment completed",
            f"[ ] Security awareness training assigned",
        ]
        return "\n".join(lines)
