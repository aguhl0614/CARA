from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .service import SOURCES, ensure_sync_states, run_sync

log = logging.getLogger("cara.sync")
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    ensure_sync_states()
    if _scheduler:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    for source, interval in SOURCES.items():
        _scheduler.add_job(
            run_sync,
            "interval",
            seconds=interval,
            args=[source],
            id=f"sync_{source}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    _scheduler.start()
    log.info("CARA scheduler started (%s)", ", ".join(SOURCES))


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
