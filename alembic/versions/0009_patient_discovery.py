"""Hospital map locations and configured queue service duration."""
from alembic import op

revision = "0009_patient_discovery"
down_revision = "0008_patient_flow"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE iam.branch ADD COLUMN IF NOT EXISTS address VARCHAR(500) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE iam.branch ADD COLUMN IF NOT EXISTS phone VARCHAR(40) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE iam.branch ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
    op.execute("ALTER TABLE iam.branch ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")
    op.execute("ALTER TABLE queue.queue ADD COLUMN IF NOT EXISTS average_service_minutes INTEGER NOT NULL DEFAULT 5")


def downgrade():
    op.execute("ALTER TABLE queue.queue DROP COLUMN IF EXISTS average_service_minutes")
    for column in ("address", "phone", "latitude", "longitude"):
        op.execute(f"ALTER TABLE iam.branch DROP COLUMN IF EXISTS {column}")
