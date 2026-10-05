"""Track processed Stripe webhook events for idempotent payment updates."""
from alembic import op

revision = "0012_stripe_webhook_events"
down_revision = "0011_appointment_quotations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS notifications.stripe_webhook_event (
            event_id VARCHAR(255) PRIMARY KEY,
            event_type VARCHAR(120) NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS notifications.stripe_webhook_event")
