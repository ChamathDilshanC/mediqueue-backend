"""Patient appointment review metadata; existing bookings remain approved."""
from alembic import op

revision = "0010_appointment_review"
down_revision = "0009_patient_discovery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'STAFF'")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS review_reason VARCHAR(500) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(200)")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ")


def downgrade():
    for column in ("reviewed_at", "reviewed_by", "review_reason", "source"):
        op.execute(f"ALTER TABLE scheduling.appointment DROP COLUMN IF EXISTS {column}")
