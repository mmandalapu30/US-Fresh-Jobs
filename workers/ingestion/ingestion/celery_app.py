"""Celery application.

Durable background processing for ingestion, lifecycle maintenance and alerts.

The settings below are chosen for a workload where a single task can run for many minutes
and download hundreds of megabytes -- defaults tuned for short web tasks would either
lose work or thrash.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging, worker_process_init

from jobplatform_shared import configure_logging, get_settings

__all__ = ["celery_app"]


def _create_app() -> Celery:
    settings = get_settings()

    app = Celery("jobplatform", broker=settings.celery_broker_url)
    app.conf.update(
        result_backend=settings.celery_result_backend,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Deliver one task at a time. Ingestion tasks are long and uneven, so prefetching
        # would leave a worker sitting on queued work it cannot start.
        worker_prefetch_multiplier=1,
        # Acknowledge only after completion: if a worker dies mid-file the task is
        # redelivered rather than silently lost. Safe because ingestion is idempotent.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # A stuck download must not hold a slot forever.
        task_soft_time_limit=3600,
        task_time_limit=3900,
        # Recycle workers periodically: PyArrow buffers fragment memory over time.
        worker_max_tasks_per_child=50,
        result_expires=86400,
        broker_connection_retry_on_startup=True,
        task_routes={
            "ingestion.tasks.sync.*": {"queue": "ingestion"},
            "ingestion.tasks.lifecycle.*": {"queue": "maintenance"},
            "ingestion.tasks.alerts.*": {"queue": "notifications"},
        },
    )

    # Task modules are registered as they are implemented; autodiscovery keeps this list
    # from becoming a place to forget things.
    app.autodiscover_tasks(["ingestion.tasks"], force=False)
    return app


celery_app = _create_app()


@setup_logging.connect
def _configure_celery_logging(**_kwargs: object) -> None:
    """Replace Celery's logging with the platform's structured logger."""
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format, service="worker")


@worker_process_init.connect
def _reset_connections(**_kwargs: object) -> None:
    """Drop inherited database connections after fork.

    A forked child inherits the parent's sockets; two processes writing to one connection
    corrupts the protocol stream. Disposing forces each child to open its own.
    """
    from jobplatform_shared.db import reset_engines

    reset_engines()
