"""Bind payments to one checkout, dead-letter outbox events and enforce ward foreign keys.

Earlier releases created ward/bed/ward_admission at application startup without foreign
keys, so `0006_management` (checkfirst) skipped them on those databases. Constraints are
added NOT VALID: every new write is enforced immediately, while pre-existing orphan rows
(if any) do not block the upgrade. Clean them, then run `ALTER TABLE ... VALIDATE CONSTRAINT`.
"""
from alembic import op

revision = "0017_payment_integrity"
down_revision = "0016_doctor_workforce_assignments"
branch_labels = None
depends_on = None

FOREIGN_KEYS = [
    ("scheduling.ward", "department_id", "scheduling.department", "fk_ward_department"),
    ("scheduling.bed", "ward_id", "scheduling.ward", "fk_bed_ward"),
    ("scheduling.bed", "room_id", "scheduling.room", "fk_bed_room"),
    ("scheduling.room", "ward_id", "scheduling.ward", "fk_room_ward"),
    ("scheduling.ward_admission", "patient_id", "queue.patient", "fk_ward_admission_patient"),
    ("scheduling.ward_admission", "ward_id", "scheduling.ward", "fk_ward_admission_ward"),
    ("scheduling.ward_admission", "bed_id", "scheduling.bed", "fk_ward_admission_bed"),
    ("scheduling.ward_task", "ward_id", "scheduling.ward", "fk_ward_task_ward"),
]


def upgrade():
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS checkout_session_id VARCHAR(255)")
    op.execute("ALTER TABLE scheduling.appointment ADD COLUMN IF NOT EXISTS payment_amount INTEGER")
    # Open checkouts created before this release have no recorded session/amount, so the
    # webhook would route them to staff review; returning them to UNPAID lets patients retry.
    op.execute("""
        UPDATE scheduling.appointment
        SET payment_status = 'UNPAID', payment_method = NULL, payment_reference = NULL
        WHERE payment_status = 'CHECKOUT_STARTED' AND checkout_session_id IS NULL
    """)
    op.execute("ALTER TABLE notifications.outbox_event ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_outbox_deliverable
        ON notifications.outbox_event (available_at)
        WHERE published_at IS NULL AND dead_lettered_at IS NULL
    """)
    for table, column, target, name in FOREIGN_KEYS:
        op.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                    WHERE c.contype = 'f' AND c.conrelid = '{table}'::regclass AND a.attname = '{column}'
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT {name}
                    FOREIGN KEY ({column}) REFERENCES {target}(id) NOT VALID;
                END IF;
            END $$;
        """)


def downgrade():
    for table, _, _, name in FOREIGN_KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS notifications.ix_outbox_deliverable")
    op.execute("ALTER TABLE notifications.outbox_event DROP COLUMN IF EXISTS dead_lettered_at")
    op.execute("ALTER TABLE scheduling.appointment DROP COLUMN IF EXISTS payment_amount")
    op.execute("ALTER TABLE scheduling.appointment DROP COLUMN IF EXISTS checkout_session_id")
