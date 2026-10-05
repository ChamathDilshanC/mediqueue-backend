"""Add hospital quotations and patient payment choices to appointments."""
from alembic import op

revision = "0011_appointment_quotations"
down_revision = "0010_appointment_review"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS quotation JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS payment_method VARCHAR(30)")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) NOT NULL DEFAULT 'UNPAID'")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(200)")


def downgrade():
    for column in ("payment_reference", "payment_status", "payment_method", "quotation"):
        op.execute(f"ALTER TABLE scheduling.appointment DROP COLUMN IF EXISTS {column}")
