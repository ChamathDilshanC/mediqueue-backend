"""Non-identifying live queue summaries and explicitly approximate wait times."""
from datetime import datetime, timedelta, timezone
from math import ceil
from statistics import median
from sqlalchemy import select
from .models import AuditEvent, QueueToken


async def queue_snapshot(queue, db):
    from .main import business_date
    today = business_date(queue)
    tokens = (await db.scalars(select(QueueToken).where(
        QueueToken.queue_id == queue.id, QueueToken.tenant_id == queue.tenant_id,
        QueueToken.branch_id == queue.branch_id, QueueToken.business_date == today
    ).order_by(QueueToken.created_at, QueueToken.token_number))).all()
    # Use actual start-to-completion durations, never time spent waiting.
    events = (await db.scalars(select(AuditEvent).join(QueueToken, QueueToken.id == AuditEvent.entity_id).where(
        QueueToken.queue_id == queue.id, AuditEvent.tenant_id == queue.tenant_id,
        AuditEvent.action.in_(("start", "complete")),
        AuditEvent.created_at >= datetime.now(timezone.utc) - timedelta(days=7)
    ).order_by(AuditEvent.created_at.desc()).limit(200))).all()
    starts, completions = {}, {}
    for event in events:
        target = starts if event.action == "start" else completions
        target.setdefault(event.entity_id, event.created_at)
    samples = [(completions[key] - start).total_seconds() / 60
               for key, start in starts.items() if key in completions]
    samples = [value for value in samples if 0.5 <= value <= 240]
    measured = len(samples) >= 3
    minutes = round(median(samples), 1) if measured else queue.average_service_minutes
    serving = [f"{t.token_number:03d}" for t in tokens if t.status in ("CALLED", "RECALLED", "IN_SERVICE")]
    waiting = sum(t.status == "WAITING" for t in tokens)
    return tokens, {
        "waiting_count": waiting, "serving_count": len(serving), "now_serving": serving,
        "estimated_wait_minutes": ceil((waiting + len(serving)) * minutes),
        "average_service_minutes": minutes, "estimate_source": "recent_service_times" if measured else "configured_average",
        "estimate_samples": len(samples), "as_of": datetime.now(timezone.utc), "business_date": str(today),
    }
