"""Add reusable quotation templates to doctors."""
from alembic import op


revision = "0013_doctor_quotation_templates"
down_revision = "0012_stripe_webhook_events"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE scheduling.doctor "
        "ADD COLUMN IF NOT EXISTS quotation_template JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade():
    op.execute(
        "ALTER TABLE scheduling.doctor "
        "DROP COLUMN IF EXISTS quotation_template"
    )
