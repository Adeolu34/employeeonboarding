"""Celery tasks for the employee onboarding pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.models import Employee, EmployeeStatus, OnboardingStep, StepName, StepStatus
from app.tasks.celery_app import celery_app
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

# Steps that are non-critical: failure will not abort the overall onboarding
NON_CRITICAL_STEPS: set[StepName] = {
    StepName.slack_account,
    StepName.jira_account,
    StepName.welcome_email,
}


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_sync_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = settings.database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def _upsert_step(
    session,
    employee_id: int,
    step_name: StepName,
    status: StepStatus,
    result_data: dict | None = None,
    error_message: str | None = None,
) -> OnboardingStep:
    """Create or update an OnboardingStep row atomically."""
    existing = session.execute(
        select(OnboardingStep).where(
            OnboardingStep.employee_id == employee_id,
            OnboardingStep.step_name == step_name,
        )
    ).scalars().first()

    if existing is None:
        existing = OnboardingStep(employee_id=employee_id, step_name=step_name)
        session.add(existing)

    existing.status = status
    if result_data is not None:
        existing.result_data = result_data
    if error_message is not None:
        existing.error_message = error_message
    if status == StepStatus.completed:
        existing.completed_at = datetime.now(tz=timezone.utc)

    session.flush()
    return existing


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)


# ── Main orchestration task ───────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    name="app.tasks.onboarding_tasks.run_onboarding_task",
    max_retries=2,
    default_retry_delay=120,
)
def run_onboarding_task(self, employee_id: int) -> dict[str, Any]:
    """Orchestrate all onboarding steps for a new employee.

    Steps are executed sequentially. Non-critical step failures are logged and
    the pipeline continues. Critical step failures abort the run.

    Steps in order:
      1. generate_documents
      2. update_hr_database (critical)
      3. create_slack_account (non-critical)
      4. create_jira_account (non-critical)
      5. send_welcome_email (non-critical)
    """
    session = _get_sync_session()
    employee: Employee | None = session.get(Employee, employee_id)

    if employee is None:
        log.error("Employee {id} not found", id=employee_id)
        return {"error": f"Employee {employee_id} not found"}

    log.info(
        "Starting onboarding for {name} ({email})",
        name=employee.full_name,
        email=employee.email,
    )

    employee.status = EmployeeStatus.in_progress
    session.commit()

    results: dict[StepName, str] = {}
    has_critical_failure = False

    # ── Step ordering ──────────────────────────────────────────────────────
    steps: list[tuple[StepName, Any]] = [
        (StepName.documents, _run_step_generate_documents),
        (StepName.hr_database, _run_step_hr_database),
        (StepName.slack_account, _run_step_slack),
        (StepName.jira_account, _run_step_jira),
        (StepName.welcome_email, _run_step_welcome_email),
    ]

    for step_name, step_fn in steps:
        _upsert_step(session, employee_id, step_name, StepStatus.pending)
        session.commit()

        try:
            result_data = step_fn(session, employee)
            _upsert_step(
                session,
                employee_id,
                step_name,
                StepStatus.completed,
                result_data=result_data,
            )
            session.commit()
            results[step_name] = "completed"
            log.info("Step {step} completed for employee {id}", step=step_name, id=employee_id)

        except Exception as exc:
            error_msg = str(exc)
            log.error(
                "Step {step} failed for employee {id}: {exc}",
                step=step_name,
                id=employee_id,
                exc=error_msg,
            )
            _upsert_step(
                session,
                employee_id,
                step_name,
                StepStatus.failed,
                error_message=error_msg,
            )
            session.commit()
            results[step_name] = f"failed: {error_msg}"

            if step_name not in NON_CRITICAL_STEPS:
                has_critical_failure = True
                log.error(
                    "Critical step {step} failed; aborting onboarding for {id}",
                    step=step_name,
                    id=employee_id,
                )
                break

    # ── Final status ───────────────────────────────────────────────────────
    if has_critical_failure:
        employee.status = EmployeeStatus.failed
    else:
        employee.status = EmployeeStatus.completed

    session.commit()
    session.close()

    log.info(
        "Onboarding {outcome} for employee {id}",
        outcome=employee.status.value,
        id=employee_id,
    )
    return {"employee_id": employee_id, "status": employee.status.value, "steps": {k.value: v for k, v in results.items()}}


# ── Step implementations ───────────────────────────────────────────────────────


def _run_step_generate_documents(session, employee: Employee) -> dict:
    from app.services.document_service import DocumentService

    svc = DocumentService(settings)
    output_dir = settings.docs_output_path / str(employee.id)
    output_dir.mkdir(parents=True, exist_ok=True)

    offer_path = svc.generate_offer_letter(employee, output_dir)
    it_path = svc.generate_it_setup_guide(employee, output_dir)
    checklist_path = svc.generate_onboarding_checklist(employee, output_dir)

    return {
        "offer_letter": str(offer_path),
        "it_setup_guide": str(it_path),
        "onboarding_checklist": str(checklist_path),
    }


def _run_step_hr_database(session, employee: Employee) -> dict:
    return _run_async(_async_hr_database(employee))


async def _async_hr_database(employee: Employee) -> dict:
    from app.automation.portal_automation import HRPortalAutomation

    automation = HRPortalAutomation(settings)
    return await automation.run(employee)


def _run_step_slack(session, employee: Employee) -> dict:
    from app.services.slack_service import SlackService

    svc = SlackService(settings)
    user_id = svc.invite_to_workspace(employee.email, employee.first_name, employee.last_name)
    if user_id:
        svc.add_to_channels(user_id, [settings.slack_default_channel])
        svc.send_welcome_dm(user_id, employee)
    return {"slack_user_id": user_id}


def _run_step_jira(session, employee: Employee) -> dict:
    from app.services.jira_service import JiraService

    svc = JiraService(settings)
    username = svc.create_user(
        email=employee.email,
        display_name=employee.full_name,
        department=employee.department or "",
    )
    ticket_key = svc.create_onboarding_ticket(employee)
    return {"jira_username": username, "onboarding_ticket": ticket_key}


def _run_step_welcome_email(session, employee: Employee) -> dict:
    from app.services.email_service import EmailService
    from app.services.document_service import DocumentService

    doc_svc = DocumentService(settings)
    output_dir = settings.docs_output_path / str(employee.id)
    doc_paths = [
        p
        for p in [
            output_dir / f"offer_letter_{employee.id}.docx",
            output_dir / f"it_setup_guide_{employee.id}.docx",
            output_dir / f"onboarding_checklist_{employee.id}.docx",
        ]
        if p.exists()
    ]

    email_svc = EmailService(settings)
    email_svc.send_welcome_email(employee, [str(p) for p in doc_paths])
    if employee.manager_email:
        email_svc.send_manager_notification(employee.manager_email, employee)

    return {"welcome_email_sent": True}


# ── Scheduled check ───────────────────────────────────────────────────────────


@celery_app.task(name="app.tasks.onboarding_tasks.check_pending_onboarding_task")
def check_pending_onboarding_task() -> dict[str, Any]:
    """Dispatch run_onboarding_task for any employees still in pending status."""
    session = _get_sync_session()
    try:
        pending = (
            session.execute(
                select(Employee).where(Employee.status == EmployeeStatus.pending)
            )
            .scalars()
            .all()
        )
        dispatched = []
        for employee in pending:
            run_onboarding_task.apply_async(args=[employee.id])
            dispatched.append(employee.id)
            log.info("Dispatched onboarding for pending employee {id}", id=employee.id)

        return {"dispatched": dispatched}
    finally:
        session.close()


# ── Per-step individual tasks (for API retry endpoint) ───────────────────────


@celery_app.task(name="app.tasks.onboarding_tasks.create_slack_account_task")
def create_slack_account_task(employee_id: int) -> dict[str, Any]:
    """Create or re-create a Slack workspace invitation for a single employee."""
    session = _get_sync_session()
    try:
        employee = session.get(Employee, employee_id)
        if not employee:
            return {"error": "not found"}
        result = _run_step_slack(session, employee)
        _upsert_step(session, employee_id, StepName.slack_account, StepStatus.completed, result_data=result)
        session.commit()
        return result
    except Exception as exc:
        _upsert_step(session, employee_id, StepName.slack_account, StepStatus.failed, error_message=str(exc))
        session.commit()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.onboarding_tasks.create_jira_account_task")
def create_jira_account_task(employee_id: int) -> dict[str, Any]:
    """Create or re-create a Jira user account for a single employee."""
    session = _get_sync_session()
    try:
        employee = session.get(Employee, employee_id)
        if not employee:
            return {"error": "not found"}
        result = _run_step_jira(session, employee)
        _upsert_step(session, employee_id, StepName.jira_account, StepStatus.completed, result_data=result)
        session.commit()
        return result
    except Exception as exc:
        _upsert_step(session, employee_id, StepName.jira_account, StepStatus.failed, error_message=str(exc))
        session.commit()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.onboarding_tasks.send_welcome_email_task")
def send_welcome_email_task(employee_id: int) -> dict[str, Any]:
    """(Re-)send the welcome email for a single employee."""
    session = _get_sync_session()
    try:
        employee = session.get(Employee, employee_id)
        if not employee:
            return {"error": "not found"}
        result = _run_step_welcome_email(session, employee)
        _upsert_step(session, employee_id, StepName.welcome_email, StepStatus.completed, result_data=result)
        session.commit()
        return result
    except Exception as exc:
        _upsert_step(session, employee_id, StepName.welcome_email, StepStatus.failed, error_message=str(exc))
        session.commit()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.onboarding_tasks.generate_documents_task")
def generate_documents_task(employee_id: int) -> dict[str, Any]:
    """(Re-)generate onboarding documents for a single employee."""
    session = _get_sync_session()
    try:
        employee = session.get(Employee, employee_id)
        if not employee:
            return {"error": "not found"}
        result = _run_step_generate_documents(session, employee)
        _upsert_step(session, employee_id, StepName.documents, StepStatus.completed, result_data=result)
        session.commit()
        return result
    except Exception as exc:
        _upsert_step(session, employee_id, StepName.documents, StepStatus.failed, error_message=str(exc))
        session.commit()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.onboarding_tasks.update_hr_database_task")
def update_hr_database_task(employee_id: int) -> dict[str, Any]:
    """(Re-)run the HR portal automation for a single employee."""
    session = _get_sync_session()
    try:
        employee = session.get(Employee, employee_id)
        if not employee:
            return {"error": "not found"}
        result = _run_step_hr_database(session, employee)
        _upsert_step(session, employee_id, StepName.hr_database, StepStatus.completed, result_data=result)
        session.commit()
        return result
    except Exception as exc:
        _upsert_step(session, employee_id, StepName.hr_database, StepStatus.failed, error_message=str(exc))
        session.commit()
        raise
    finally:
        session.close()
