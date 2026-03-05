from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "pdf_compare",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_soft_time_limit=settings.compare_task_soft_timeout_sec,
    task_time_limit=settings.compare_task_hard_timeout_sec,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "cleanup-expired-jobs-hourly": {
            "task": "app.workers.tasks.cleanup_expired_jobs_task",
            "schedule": crontab(minute="0"),
        }
    },
)

celery_app.autodiscover_tasks(["app.workers"])
