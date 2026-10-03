"""Add department_id to scheduling.room and queue.queue."""
from alembic import op

revision = "0005_room_department"
down_revision = "0004_department_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE scheduling.room ADD COLUMN IF NOT EXISTS department_id UUID REFERENCES scheduling.department(id)")
    op.execute("ALTER TABLE queue.queue ADD COLUMN IF NOT EXISTS department_id UUID REFERENCES scheduling.department(id)")


def downgrade():
    op.execute("ALTER TABLE queue.queue DROP COLUMN IF EXISTS department_id")
    op.execute("ALTER TABLE scheduling.room DROP COLUMN IF EXISTS department_id")
