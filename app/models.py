"""SQLAlchemy ORM models for the Employee Onboarding system."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ── Enums ─────────────────────────────────────────────────────────────────────


class EmployeeStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


class StepName(str, enum.Enum):
    email_account = "email_account"
    slack_account = "slack_account"
    jira_account = "jira_account"
    welcome_email = "welcome_email"
    documents = "documents"
    hr_database = "hr_database"


class StepStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


# ── Models ────────────────────────────────────────────────────────────────────


class Employee(Base):
    """Represents a new hire whose onboarding is being automated."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    department: Mapped[str | None] = mapped_column(String(128))
    job_title: Mapped[str | None] = mapped_column(String(256))
    start_date: Mapped[datetime | None] = mapped_column(Date)
    manager_email: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[EmployeeStatus] = mapped_column(
        Enum(EmployeeStatus),
        default=EmployeeStatus.pending,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    steps: Mapped[list[OnboardingStep]] = relationship(
        "OnboardingStep", back_populates="employee", cascade="all, delete-orphan"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<Employee id={self.id} email={self.email} status={self.status}>"


class OnboardingStep(Base):
    """Tracks the status of each individual onboarding step for an employee."""

    __tablename__ = "onboarding_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_name: Mapped[StepName] = mapped_column(
        Enum(StepName), nullable=False
    )
    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus),
        default=StepStatus.pending,
        nullable=False,
    )
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    employee: Mapped[Employee] = relationship("Employee", back_populates="steps")

    def __repr__(self) -> str:
        return f"<OnboardingStep id={self.id} employee={self.employee_id} step={self.step_name} status={self.status}>"
