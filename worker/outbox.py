"""Retrying outbox poller with an explicit provider boundary."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from backend.db import SessionLocal
from backend.models import OutboxEvent

logger = logging.getLogger("mediqueue.outbox")


class StubProvider:
    """Development provider that records delivery without external side effects."""
    async def publish(self, event: OutboxEvent) -> None:
        """Deliver one event; production providers implement this interface."""
        return None


def deliverable(now: datetime):
    """Due, undelivered, not dead-lettered events, claimed so concurrent workers never share one."""
    return (select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None), OutboxEvent.dead_lettered_at.is_(None),
                   OutboxEvent.available_at <= now)
            .order_by(OutboxEvent.available_at, OutboxEvent.id)
            .limit(50)
            # PostgreSQL: rows locked by another worker are skipped, not delivered twice.
            .with_for_update(skip_locked=True))


async def poll_once(provider: StubProvider | None = None, max_attempts: int = 5) -> int:
    """Publish due events, applying exponential retry and a one-time dead-letter state."""
    provider = provider or StubProvider(); delivered = 0
    async with SessionLocal() as db:
        events = (await db.scalars(deliverable(datetime.now(timezone.utc)))).all()
        for event in events:
            try:
                await provider.publish(event)
                event.published_at = datetime.now(timezone.utc); delivered += 1
            except Exception:
                event.attempts += 1
                now = datetime.now(timezone.utc)
                if event.attempts >= max_attempts:
                    event.dead_lettered_at = now
                    logger.error("Outbox event %s dead-lettered after %s attempts", event.id, event.attempts)
                else:
                    event.available_at = now + timedelta(seconds=min(300, 2 ** event.attempts))
        await db.commit()
    return delivered


async def run_forever(interval: float = 5) -> None:
    """Continuously poll the outbox for a separately deployed worker."""
    while True:
        await poll_once(); await asyncio.sleep(interval)
