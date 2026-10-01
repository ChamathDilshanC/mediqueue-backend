"""Add organization verification applications."""
from alembic import op

revision = "0003_organization_applications"
down_revision = "0002_entities"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE iam.organization_application (
            id UUID NOT NULL,
            applicant_id UUID NOT NULL,
            organization_type VARCHAR(30) NOT NULL,
            official_name VARCHAR(200) NOT NULL,
            address TEXT NOT NULL,
            phone VARCHAR(40) NOT NULL,
            official_email VARCHAR(254) NOT NULL,
            registration_number VARCHAR(120) NOT NULL,
            license_number VARCHAR(120) NOT NULL,
            supporting_document_url VARCHAR(1000) NOT NULL,
            website_url VARCHAR(500) NOT NULL,
            administrator_name VARCHAR(200) NOT NULL,
            administrator_role VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(applicant_id) REFERENCES iam.user_profile (id)
        )
    """)
    op.execute("CREATE INDEX ix_iam_organization_application_applicant_id ON iam.organization_application (applicant_id)")
    op.execute("CREATE INDEX ix_iam_organization_application_status ON iam.organization_application (status)")


def downgrade():
    op.drop_table("organization_application", schema="iam")
