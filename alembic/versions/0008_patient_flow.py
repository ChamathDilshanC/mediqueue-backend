"""Assign queue service stages and rooms."""
from alembic import op
revision = "0008_patient_flow"
down_revision = "0007_ward_stay_dates"
branch_labels = None
depends_on = None
def upgrade():
    op.execute("ALTER TABLE queue.queue ADD COLUMN IF NOT EXISTS service_type VARCHAR(30) NOT NULL DEFAULT 'GENERAL'")
    op.execute("ALTER TABLE queue.queue ADD COLUMN IF NOT EXISTS room_id UUID REFERENCES scheduling.room(id)")
def downgrade():
    op.execute("ALTER TABLE queue.queue DROP COLUMN IF EXISTS room_id")
    op.execute("ALTER TABLE queue.queue DROP COLUMN IF EXISTS service_type")
