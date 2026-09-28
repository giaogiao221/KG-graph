from __future__ import annotations

import os

from celery import Celery
from kombu import Exchange, Queue


RULE_EXCHANGE = Exchange("rule", type="direct", durable=True)
LLM_EXCHANGE = Exchange("llm", type="direct", durable=True)


def build_celery_app(
    *, broker_url: str | None = None, result_backend: str | None = None
) -> Celery:
    app = Celery(
        "extraction_platform",
        broker=broker_url
        or os.getenv("EXTRACTION_CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=result_backend
        or os.getenv("EXTRACTION_CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
        include=["app.jobs.tasks"],
    )
    app.conf.update(
        # Declare both queues explicitly.  Kombu otherwise lets the second
        # queue inherit the default ``rule`` exchange and routing key, which
        # makes an LLM message visible to the rule worker as well.
        task_queues=(
            Queue("rule", exchange=RULE_EXCHANGE, routing_key="rule"),
            Queue("llm", exchange=LLM_EXCHANGE, routing_key="llm"),
        ),
        task_default_queue="rule",
        task_default_exchange="rule",
        task_default_exchange_type="direct",
        task_default_routing_key="rule",
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        beat_schedule={
            "publish-job-dispatch-outbox": {
                "task": "app.jobs.tasks.publish_dispatch_outbox",
                "schedule": 10.0,
            },
            "recover-expired-step-leases": {
                "task": "app.jobs.tasks.recover_expired_step_leases",
                "schedule": 30.0,
            },
            "retry-pending-workdir-cleanup": {
                "task": "app.jobs.tasks.retry_pending_workdir_cleanup",
                "schedule": 60.0,
            },
            "sweep-job-artifacts": {
                "task": "app.jobs.tasks.sweep_job_artifacts",
                "schedule": 60.0,
            },
            "retry-pending-storage-cleanup": {
                "task": "app.jobs.tasks.retry_pending_storage_cleanup",
                "schedule": 60.0,
            },
        },
    )
    return app


celery_app = build_celery_app()
