"""Add ward workforce, shifts, attendance and care tasks."""
from alembic import op


revision = "0015_workforce_operations"
down_revision = "0014_room_bed_hierarchy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS scheduling.nurse (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, branch_id UUID NOT NULL,
            employee_id VARCHAR(50) NOT NULL, name VARCHAR(200) NOT NULL,
            phone VARCHAR(40) NOT NULL DEFAULT '', status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS scheduling.attendant (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, branch_id UUID NOT NULL,
            employee_id VARCHAR(50) NOT NULL, name VARCHAR(200) NOT NULL,
            phone VARCHAR(40) NOT NULL DEFAULT '', status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS scheduling.staff_shift (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, branch_id UUID NOT NULL,
            nurse_id UUID REFERENCES scheduling.nurse(id), attendant_id UUID REFERENCES scheduling.attendant(id),
            shift_date DATE NOT NULL, shift VARCHAR(20) NOT NULL,
            starts_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'SCHEDULED', notes VARCHAR(500) NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS scheduling.staff_attendance (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, branch_id UUID NOT NULL,
            shift_id UUID NOT NULL REFERENCES scheduling.staff_shift(id),
            attendance_status VARCHAR(20) NOT NULL DEFAULT 'PRESENT',
            check_in_at TIMESTAMPTZ, check_out_at TIMESTAMPTZ,
            notes VARCHAR(500) NOT NULL DEFAULT '', verified_by VARCHAR(200) NOT NULL DEFAULT '',
            verified_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS scheduling.ward_task (
            id UUID PRIMARY KEY, tenant_id UUID NOT NULL, branch_id UUID NOT NULL,
            ward_id UUID NOT NULL REFERENCES scheduling.ward(id),
            patient_id UUID REFERENCES queue.patient(id),
            nurse_id UUID REFERENCES scheduling.nurse(id),
            attendant_id UUID REFERENCES scheduling.attendant(id),
            task_type VARCHAR(30) NOT NULL, scheduled_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'ASSIGNED', notes TEXT NOT NULL DEFAULT '',
            completed_at TIMESTAMPTZ, verified_by VARCHAR(200) NOT NULL DEFAULT '',
            verified_at TIMESTAMPTZ
        );
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS scheduling.ward_task")
    op.execute("DROP TABLE IF EXISTS scheduling.staff_attendance")
    op.execute("DROP TABLE IF EXISTS scheduling.staff_shift")
    op.execute("DROP TABLE IF EXISTS scheduling.attendant")
    op.execute("DROP TABLE IF EXISTS scheduling.nurse")
