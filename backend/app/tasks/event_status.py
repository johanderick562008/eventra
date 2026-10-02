"""Background job that persists ONGOING / COMPLETED statuses.

This is cosmetic for stored data and reporting: API responses derive the status from the clock,
and booking always checks ``start_at`` under a lock, so a delayed sweep can never allow an
invalid booking.
"""

import asyncio
import logging
from contextlib import suppress

from sqlalchemy import update

from app.db.session import SessionLocal
from app.models.event import Event, EventStatus
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


def sweep_event_statuses() -> tuple[int, int]:
    """Returns ``(marked_ongoing, marked_completed)``."""
    now = utcnow()
    with SessionLocal() as db, db.begin():
        completed = db.execute(
            update(Event)
            .where(
                Event.status.in_([EventStatus.UPCOMING, EventStatus.ONGOING]),
                Event.end_at <= now,
            )
            .values(status=EventStatus.COMPLETED, updated_at=now)
        ).rowcount
        ongoing = db.execute(
            update(Event)
            .where(
                Event.status == EventStatus.UPCOMING,
                Event.start_at <= now,
                Event.end_at > now,
            )
            .values(status=EventStatus.ONGOING, updated_at=now)
        ).rowcount
    return ongoing, completed


async def run_event_status_sweeper(interval_seconds: int, stop: asyncio.Event) -> None:
    logger.info("Event status sweeper started (every %ss)", interval_seconds)
    while not stop.is_set():
        try:
            ongoing, completed = await asyncio.to_thread(sweep_event_statuses)
            if ongoing or completed:
                logger.info("Event sweep: %d ongoing, %d completed", ongoing, completed)
        except Exception:
            logger.exception("Event status sweep failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
    logger.info("Event status sweeper stopped")
