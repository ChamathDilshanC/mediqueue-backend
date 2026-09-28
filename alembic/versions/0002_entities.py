"""Add user profiles, branch memberships and scheduling entities."""
from alembic import op

revision = "0002_entities"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS scheduling")
    op.execute("""
        CREATE TABLE iam.user_profile (
            id UUID NOT NULL,
            display_name VARCHAR(200) NOT NULL,
            PRIMARY KEY (id)
        )
    """)
    op.execute("""
        CREATE TABLE iam.membership (
            id UUID NOT NULL,
            user_id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            role VARCHAR(20) NOT NULL,
            active BOOLEAN NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (user_id, branch_id),
            FOREIGN KEY(user_id) REFERENCES iam.user_profile (id),
            FOREIGN KEY(tenant_id) REFERENCES iam.tenant (id),
            FOREIGN KEY(branch_id) REFERENCES iam.branch (id)
        )
    """)
    op.execute('CREATE INDEX ix_iam_membership_branch_id ON iam.membership (branch_id)')
    op.execute('CREATE INDEX ix_iam_membership_tenant_id ON iam.membership (tenant_id)')
    op.execute("""
        CREATE TABLE scheduling.department (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            name VARCHAR(200) NOT NULL,
            PRIMARY KEY (id)
        )
    """)
    op.execute('CREATE INDEX ix_scheduling_department_branch_id ON scheduling.department (branch_id)')
    op.execute('CREATE INDEX ix_scheduling_department_tenant_id ON scheduling.department (tenant_id)')
    op.execute("""
        CREATE TABLE scheduling.room (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            name VARCHAR(120) NOT NULL,
            PRIMARY KEY (id)
        )
    """)
    op.execute('CREATE INDEX ix_scheduling_room_branch_id ON scheduling.room (branch_id)')
    op.execute('CREATE INDEX ix_scheduling_room_tenant_id ON scheduling.room (tenant_id)')
    op.execute("""
        CREATE TABLE scheduling.doctor (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            department_id UUID NOT NULL,
            name VARCHAR(200) NOT NULL,
            specialty VARCHAR(200) NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(department_id) REFERENCES scheduling.department (id)
        )
    """)
    op.execute('CREATE INDEX ix_scheduling_doctor_branch_id ON scheduling.doctor (branch_id)')
    op.execute('CREATE INDEX ix_scheduling_doctor_tenant_id ON scheduling.doctor (tenant_id)')
    op.execute("""
        CREATE TABLE scheduling.schedule (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            doctor_id UUID NOT NULL,
            room_id UUID NOT NULL,
            starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
            ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
            capacity INTEGER NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(doctor_id) REFERENCES scheduling.doctor (id),
            FOREIGN KEY(room_id) REFERENCES scheduling.room (id)
        )
    """)
    op.execute('CREATE INDEX ix_scheduling_schedule_branch_id ON scheduling.schedule (branch_id)')
    op.execute('CREATE INDEX ix_scheduling_schedule_tenant_id ON scheduling.schedule (tenant_id)')
    op.execute("""
        CREATE TABLE scheduling.appointment (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            branch_id UUID NOT NULL,
            schedule_id UUID NOT NULL,
            patient_id UUID NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(schedule_id) REFERENCES scheduling.schedule (id),
            FOREIGN KEY(patient_id) REFERENCES queue.patient (id)
        )
    """)
    op.execute('CREATE INDEX ix_scheduling_appointment_branch_id ON scheduling.appointment (branch_id)')
    op.execute('CREATE INDEX ix_scheduling_appointment_tenant_id ON scheduling.appointment (tenant_id)')


def downgrade():
    op.drop_table('appointment', schema='scheduling')
    op.drop_table('schedule', schema='scheduling')
    op.drop_table('doctor', schema='scheduling')
    op.drop_table('room', schema='scheduling')
    op.drop_table('department', schema='scheduling')
    op.drop_table('membership', schema='iam')
    op.drop_table('user_profile', schema='iam')
