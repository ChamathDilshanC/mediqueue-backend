"""Allow doctors as workforce assignees."""
from alembic import op


revision = "0016_doctor_workforce_assignments"
down_revision = "0015_workforce_operations"
branch_labels = None
depends_on = None


def upgrade():
    # This revision id is 33 characters; Alembic creates version_num as VARCHAR(32),
    # so recording it would fail on PostgreSQL without a wider column.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    op.execute("""
        ALTER TABLE scheduling.staff_shift
        ADD COLUMN IF NOT EXISTS doctor_id UUID REFERENCES scheduling.doctor(id);
        ALTER TABLE scheduling.ward_task
        ADD COLUMN IF NOT EXISTS doctor_id UUID REFERENCES scheduling.doctor(id);
        ALTER TABLE scheduling.staff_shift
        ADD CONSTRAINT ck_staff_shift_one_assignee
        CHECK ((doctor_id IS NOT NULL)::int + (nurse_id IS NOT NULL)::int + (attendant_id IS NOT NULL)::int = 1);
        ALTER TABLE scheduling.ward_task
        ADD CONSTRAINT ck_ward_task_one_assignee
        CHECK ((doctor_id IS NOT NULL)::int + (nurse_id IS NOT NULL)::int + (attendant_id IS NOT NULL)::int = 1);
    """)


def downgrade():
    op.execute("ALTER TABLE scheduling.ward_task DROP CONSTRAINT IF EXISTS ck_ward_task_one_assignee")
    op.execute("ALTER TABLE scheduling.staff_shift DROP CONSTRAINT IF EXISTS ck_staff_shift_one_assignee")
    op.execute("ALTER TABLE scheduling.ward_task DROP COLUMN IF EXISTS doctor_id")
    op.execute("ALTER TABLE scheduling.staff_shift DROP COLUMN IF EXISTS doctor_id")
