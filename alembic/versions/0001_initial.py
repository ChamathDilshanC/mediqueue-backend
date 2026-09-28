"""Create the initial tenant-scoped queue and notification schemas."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all tables required by the first queue vertical slice."""
    for schema in ("iam", "queue", "notifications"):
        op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))

    uuid = sa.Uuid()
    op.create_table(
        "tenant",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        schema="iam",
    )
    op.create_table(
        "branch",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, sa.ForeignKey("iam.tenant.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        schema="iam",
    )
    op.create_table(
        "queue",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("branch_id", uuid, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("token_sequence", sa.Integer, nullable=False, server_default="0"),
        schema="queue",
    )
    op.create_table(
        "patient",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=False),
        schema="queue",
    )
    op.create_table(
        "visit",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("branch_id", uuid, nullable=False),
        sa.Column("patient_id", uuid, sa.ForeignKey("queue.patient.id"), nullable=False),
        schema="queue",
    )
    op.create_table(
        "queue_token",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("branch_id", uuid, nullable=False),
        sa.Column("queue_id", uuid, sa.ForeignKey("queue.queue.id"), nullable=False),
        sa.Column("visit_id", uuid, sa.ForeignKey("queue.visit.id"), nullable=False),
        sa.Column("business_date", sa.Date, nullable=False),
        sa.Column("token_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="WAITING"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "branch_id", "queue_id", "business_date", "token_number",
            name="uq_token_scope",
        ),
        schema="queue",
    )
    op.create_table(
        "idempotency_key",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_idempotency_scope"),
        schema="queue",
    )
    op.create_table(
        "audit_event",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema="queue",
    )
    op.create_table(
        "outbox_event",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        schema="notifications",
    )


def downgrade() -> None:
    """Drop the initial schemas in dependency order."""
    for schema, names in (
        ("notifications", ["outbox_event"]),
        ("queue", ["audit_event", "idempotency_key", "queue_token", "visit", "patient", "queue"]),
        ("iam", ["branch", "tenant"]),
    ):
        for name in names:
            op.drop_table(name, schema=schema)
