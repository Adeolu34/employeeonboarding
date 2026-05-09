"""FastAPI application for the Employee Onboarding system."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, init_db
from app.models import Employee, EmployeeStatus, OnboardingStep, StepName, StepStatus
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

app = FastAPI(
    title="Employee Onboarding API",
    description="RPA pipeline: automate new hire onboarding across Slack, Jira, HR portal, and email.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup ───────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise database and required directories."""
    await init_db()
    settings.docs_output_path.mkdir(parents=True, exist_ok=True)
    settings.screenshots_path.mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)
    log.info("Employee Onboarding API started")


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class EmployeeCreate(BaseModel):
    first_name: str
    last_name: str
    email: str
    department: str | None = None
    job_title: str | None = None
    start_date: date | None = None
    manager_email: str | None = None


class EmployeeOut(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str
    department: str | None
    job_title: str | None
    start_date: str | None
    manager_email: str | None
    status: EmployeeStatus
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_instance(cls, emp: Employee) -> "EmployeeOut":
        return cls(
            id=emp.id,
            first_name=emp.first_name,
            last_name=emp.last_name,
            email=emp.email,
            department=emp.department,
            job_title=emp.job_title,
            start_date=str(emp.start_date) if emp.start_date else None,
            manager_email=emp.manager_email,
            status=emp.status,
            created_at=emp.created_at.isoformat(),
            updated_at=emp.updated_at.isoformat(),
        )


class OnboardingStepOut(BaseModel):
    id: int
    employee_id: int
    step_name: StepName
    status: StepStatus
    result_data: dict[str, Any] | None
    error_message: str | None
    completed_at: str | None
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_instance(cls, step: OnboardingStep) -> "OnboardingStepOut":
        return cls(
            id=step.id,
            employee_id=step.employee_id,
            step_name=step.step_name,
            status=step.status,
            result_data=step.result_data,
            error_message=step.error_message,
            completed_at=step.completed_at.isoformat() if step.completed_at else None,
            created_at=step.created_at.isoformat(),
        )


class DashboardStats(BaseModel):
    total_employees: int
    by_status: dict[str, int]
    completion_rate: float
    step_success_rates: dict[str, float]


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Return a simple liveness check."""
    return {"status": "ok", "service": "employee-onboarding"}


@app.post(
    "/employees",
    response_model=EmployeeOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["employees"],
)
async def create_employee(
    payload: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
) -> EmployeeOut:
    """Submit a new employee for onboarding.

    Creates the employee record and dispatches the full onboarding pipeline as a
    background Celery task. Returns immediately with the created record.
    """
    # Check for duplicate email
    existing_result = await db.execute(
        select(Employee).where(Employee.email == payload.email)
    )
    existing = existing_result.scalars().first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Employee with email {payload.email} already exists (id={existing.id})",
        )

    employee = Employee(
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        department=payload.department,
        job_title=payload.job_title,
        start_date=payload.start_date,
        manager_email=payload.manager_email,
        status=EmployeeStatus.pending,
    )
    db.add(employee)
    await db.commit()
    await db.refresh(employee)

    # Dispatch Celery task
    from app.tasks.onboarding_tasks import run_onboarding_task

    run_onboarding_task.apply_async(args=[employee.id])
    log.info(
        "Onboarding dispatched for {name} (id={id})",
        name=employee.full_name,
        id=employee.id,
    )

    return EmployeeOut.from_orm_instance(employee)


@app.get("/employees", response_model=list[EmployeeOut], tags=["employees"])
async def list_employees(
    db: AsyncSession = Depends(get_db),
    status_filter: EmployeeStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> list[EmployeeOut]:
    """List employees with optional status filter and pagination."""
    stmt = select(Employee).order_by(Employee.created_at.desc())
    if status_filter is not None:
        stmt = stmt.where(Employee.status == status_filter)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    employees = result.scalars().all()
    return [EmployeeOut.from_orm_instance(e) for e in employees]


@app.get("/employees/{employee_id}", response_model=EmployeeOut, tags=["employees"])
async def get_employee(
    employee_id: int, db: AsyncSession = Depends(get_db)
) -> EmployeeOut:
    """Fetch a single employee record by ID."""
    employee = await db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
    return EmployeeOut.from_orm_instance(employee)


@app.get(
    "/employees/{employee_id}/steps",
    response_model=list[OnboardingStepOut],
    tags=["employees"],
)
async def get_employee_steps(
    employee_id: int, db: AsyncSession = Depends(get_db)
) -> list[OnboardingStepOut]:
    """Return all onboarding step statuses for a given employee."""
    employee = await db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")

    result = await db.execute(
        select(OnboardingStep)
        .where(OnboardingStep.employee_id == employee_id)
        .order_by(OnboardingStep.created_at)
    )
    steps = result.scalars().all()
    return [OnboardingStepOut.from_orm_instance(s) for s in steps]


@app.post(
    "/employees/{employee_id}/retry-step/{step_name}",
    response_model=dict[str, str],
    tags=["employees"],
)
async def retry_step(
    employee_id: int,
    step_name: StepName,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Manually retry a specific failed or skipped onboarding step.

    Dispatches the corresponding Celery task for the given step.
    """
    employee = await db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")

    # Validate the step exists and is in a retriable state
    step_result = await db.execute(
        select(OnboardingStep).where(
            OnboardingStep.employee_id == employee_id,
            OnboardingStep.step_name == step_name,
        )
    )
    step = step_result.scalars().first()
    if step and step.status not in (StepStatus.failed, StepStatus.skipped, StepStatus.pending):
        raise HTTPException(
            status_code=400,
            detail=f"Step '{step_name}' is in status '{step.status}'; only failed/skipped/pending steps can be retried",
        )

    # Map step name to Celery task
    from app.tasks import onboarding_tasks

    task_map = {
        StepName.slack_account: onboarding_tasks.create_slack_account_task,
        StepName.jira_account: onboarding_tasks.create_jira_account_task,
        StepName.welcome_email: onboarding_tasks.send_welcome_email_task,
        StepName.documents: onboarding_tasks.generate_documents_task,
        StepName.hr_database: onboarding_tasks.update_hr_database_task,
    }

    task = task_map.get(step_name)
    if task is None:
        raise HTTPException(
            status_code=400,
            detail=f"No individual retry task for step '{step_name}'; retry the full onboarding instead",
        )

    task.apply_async(args=[employee_id])
    log.info(
        "Manual retry of step {step} for employee {id}",
        step=step_name,
        id=employee_id,
    )
    return {"status": "dispatched", "step": step_name.value, "employee_id": str(employee_id)}


@app.get("/dashboard/stats", response_model=DashboardStats, tags=["dashboard"])
async def dashboard_stats(db: AsyncSession = Depends(get_db)) -> DashboardStats:
    """Return aggregate onboarding statistics for the dashboard."""
    # Employee counts by status
    total_result = await db.execute(select(func.count(Employee.id)))
    total: int = total_result.scalar_one() or 0

    status_result = await db.execute(
        select(Employee.status, func.count(Employee.id)).group_by(Employee.status)
    )
    by_status: dict[str, int] = {row[0].value: row[1] for row in status_result.all()}

    completed = by_status.get("completed", 0)
    completion_rate = round(completed / total * 100, 2) if total > 0 else 0.0

    # Per-step success rates
    step_stats_result = await db.execute(
        select(
            OnboardingStep.step_name,
            OnboardingStep.status,
            func.count(OnboardingStep.id),
        ).group_by(OnboardingStep.step_name, OnboardingStep.status)
    )
    step_rows = step_stats_result.all()

    step_totals: dict[str, int] = {}
    step_completed: dict[str, int] = {}
    for step_name, step_status, count in step_rows:
        key = step_name.value
        step_totals[key] = step_totals.get(key, 0) + count
        if step_status == StepStatus.completed:
            step_completed[key] = step_completed.get(key, 0) + count

    step_success_rates: dict[str, float] = {
        key: round(step_completed.get(key, 0) / total_count * 100, 2)
        for key, total_count in step_totals.items()
        if total_count > 0
    }

    return DashboardStats(
        total_employees=total,
        by_status=by_status,
        completion_rate=completion_rate,
        step_success_rates=step_success_rates,
    )
