"""Add ward rooms and room assignments for beds."""
from alembic import op

revision = "0014_room_bed_hierarchy"
down_revision = "0013_doctor_quotation_templates"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE scheduling.room ADD COLUMN IF NOT EXISTS ward_id UUID REFERENCES scheduling.ward(id)")
    op.execute("ALTER TABLE scheduling.bed ADD COLUMN IF NOT EXISTS room_id UUID REFERENCES scheduling.room(id)")


def downgrade():
    op.execute("ALTER TABLE scheduling.bed DROP COLUMN IF EXISTS room_id")
    op.execute("ALTER TABLE scheduling.room DROP COLUMN IF EXISTS ward_id")
