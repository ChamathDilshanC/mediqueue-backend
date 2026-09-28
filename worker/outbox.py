"""Retrying outbox poller with an explicit provider boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from backend.db import SessionLocal
from backend.models import OutboxEvent

class StubProvider:
    """Development provider that records delivery without external side effects."""
    async def publish(self, event: OutboxEvent) -> None:
        """Deliver one event; production providers implement this interface."""
        return None

async def poll_once(provider: StubProvider | None = None, max_attempts: int = 5) -> int:
    """Publish available events, applying exponential retry and dead-letter state."""
    provider = provider or StubProvider(); delivered = 0
    async with SessionLocal() as db:
        events = (await db.scalars(select(OutboxEvent).where(OutboxEvent.published_at.is_(None)).limit(50))).all()
        for event in events:
            try:
                await provider.publish(event); event.published_at = datetime.now(timezone.utc); delivered += 1
            except Exception:
                event.attempts += 1
                event.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** event.attempts))
                if event.attempts >= max_attempts: event.event_type = "dead_letter." + event.event_type
        await db.commit()
    return delivered

async def run_forever(interval: float = 5) -> None:
    """Continuously poll the outbox for a separately deployed worker."""
    while True:
        await poll_once(); await asyncio.sleep(interval)
