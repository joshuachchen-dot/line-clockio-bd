"""add card_number to employees

Revision ID: 004
Revises: 003
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("card_number", sa.String(8), nullable=True),
    )
    op.create_index("ix_employees_card_number", "employees", ["card_number"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_employees_card_number", table_name="employees")
    op.drop_column("employees", "card_number")
