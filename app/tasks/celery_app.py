"""Celery application factory for the Employee Onboarding service."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "employee_onboarding",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.onboarding_tasks",
    ],
)

# ── Serialisation ─────────────────────────────────────────────────────────────
celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.timezone = "UTC"
celery_app.conf.enable_utc = True

# ── Result handling ───────────────────────────────────────────────────────────
celery_app.conf.result_expires = 60 * 60 * 48   # 48 hours
celery_app.conf.task_track_started = True
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1

# ── Default queue ──────────────────────────────────────────────────────────────
celery_app.conf.task_default_queue = "default"
celery_app.conf.task_default_exchange = "default"
celery_app.conf.task_default_routing_key = "default"

# ── Beat schedule ─────────────────────────────────────────────────────────────
celery_app.conf.beat_schedule = {
    "check-pending-onboarding-every-10-minutes": {
        "task": "app.tasks.onboarding_tasks.check_pending_onboarding_task",
        "schedule": crontab(minute="*/10"),
    },
}

# ── Worker safety ─────────────────────────────────────────────────────────────
celery_app.conf.worker_max_tasks_per_child = 50
celery_app.conf.task_soft_time_limit = 600    # 10 min soft limit
celery_app.conf.task_time_limit = 900         # 15 min hard limit
