"""Patient ownership and hospital management module storage."""
from alembic import op
from backend.models import ManagementRecord, PatientAccount, Patient, Ward, Bed, WardAdmission
from sqlalchemy import String, Text
revision = "0006_management"
down_revision = "0005_room_department"
branch_labels = None
depends_on = None

def upgrade():
    # Previous releases added these fields/tables at startup. Make clean database
    # upgrades reproducible without depending on application startup DDL.
    for column in Patient.__table__.columns:
        if column.name not in {"id", "tenant_id", "external_ref"}:
            size = getattr(column.type, "length", None)
            sql_type = f"VARCHAR({size})" if isinstance(column.type, String) and not isinstance(column.type, Text) else "TEXT"
            default = "ACTIVE" if column.name == "status" else ""
            op.execute(f"ALTER TABLE queue.patient ADD COLUMN IF NOT EXISTS {column.name} {sql_type} NOT NULL DEFAULT '{default}'")
    for model in (Ward, Bed, WardAdmission):
        model.__table__.create(op.get_bind(), checkfirst=True)
    ManagementRecord.__table__.create(op.get_bind(), checkfirst=True)
    PatientAccount.__table__.create(op.get_bind(), checkfirst=True)

def downgrade():
    PatientAccount.__table__.drop(op.get_bind())
    ManagementRecord.__table__.drop(op.get_bind())
