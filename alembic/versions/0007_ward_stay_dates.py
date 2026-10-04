"""Add planned discharge and bed allocation timestamps for ward visualizations."""
from alembic import op
revision = "0007_ward_stay_dates"
down_revision = "0006_management"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("ALTER TABLE scheduling.ward_admission ADD COLUMN IF NOT EXISTS planned_discharge_at TIMESTAMPTZ")
    op.execute("ALTER TABLE scheduling.ward_admission ADD COLUMN IF NOT EXISTS bed_assigned_at TIMESTAMPTZ")
    op.execute("UPDATE scheduling.ward_admission SET bed_assigned_at = admitted_at WHERE bed_id IS NOT NULL AND bed_assigned_at IS NULL")

def downgrade():
    op.execute("ALTER TABLE scheduling.ward_admission DROP COLUMN IF EXISTS bed_assigned_at")
    op.execute("ALTER TABLE scheduling.ward_admission DROP COLUMN IF EXISTS planned_discharge_at")
