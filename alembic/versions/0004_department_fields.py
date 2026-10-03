"""Add code, description, location, head_of_dept, and is_active columns to scheduling.department."""
from alembic import op

revision = "0004_department_fields"
down_revision = "0003_organization_applications"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE scheduling.department ADD COLUMN IF NOT EXISTS code VARCHAR(20) DEFAULT '' NOT NULL")
    op.execute("ALTER TABLE scheduling.department ADD COLUMN IF NOT EXISTS description TEXT DEFAULT '' NOT NULL")
    op.execute("ALTER TABLE scheduling.department ADD COLUMN IF NOT EXISTS location VARCHAR(200) DEFAULT '' NOT NULL")
    op.execute("ALTER TABLE scheduling.department ADD COLUMN IF NOT EXISTS head_of_dept VARCHAR(200) DEFAULT '' NOT NULL")
    op.execute("ALTER TABLE scheduling.department ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE NOT NULL")


def downgrade():
    op.execute("ALTER TABLE scheduling.department DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE scheduling.department DROP COLUMN IF EXISTS head_of_dept")
    op.execute("ALTER TABLE scheduling.department DROP COLUMN IF EXISTS location")
    op.execute("ALTER TABLE scheduling.department DROP COLUMN IF EXISTS description")
    op.execute("ALTER TABLE scheduling.department DROP COLUMN IF EXISTS code")
