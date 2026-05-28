"""align aws db schema with app models

Revision ID: f6b8c7d2e9a4
Revises: c2f4a8f9f8a1
Create Date: 2026-05-26 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6b8c7d2e9a4"
down_revision: Union[str, None] = "c2f4a8f9f8a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Add columns expected by main.py without changing API logic."""
    user_columns = _columns("users")
    if "password" not in user_columns:
        op.add_column("users", sa.Column("password", sa.String(length=255), nullable=True))
        if "password_hash" in user_columns:
            op.execute("UPDATE users SET password = password_hash WHERE password IS NULL")
        op.alter_column("users", "password", nullable=False)

    share_link_columns = _columns("share_links")
    if "client_name" not in share_link_columns:
        op.add_column(
            "share_links",
            sa.Column("client_name", sa.String(length=200), nullable=True),
        )
        op.execute("UPDATE share_links SET client_name = '' WHERE client_name IS NULL")
        op.alter_column("share_links", "client_name", nullable=False)

    if "assigned_staff_user_id" not in share_link_columns:
        op.add_column(
            "share_links",
            sa.Column("assigned_staff_user_id", sa.BigInteger(), nullable=True),
        )
        op.create_foreign_key(
            "fk_share_links_assigned_staff_user_id_users",
            "share_links",
            "users",
            ["assigned_staff_user_id"],
            ["id"],
        )

    if "status" not in share_link_columns:
        op.add_column(
            "share_links",
            sa.Column("status", sa.String(length=10), nullable=True),
        )
        if "is_active" in share_link_columns:
            op.execute(
                "UPDATE share_links "
                "SET status = CASE WHEN is_active THEN 'ACTIVE' ELSE 'REVOKED' END "
                "WHERE status IS NULL"
            )
        else:
            op.execute("UPDATE share_links SET status = 'ACTIVE' WHERE status IS NULL")
        op.alter_column("share_links", "status", nullable=False)

    if "note" not in share_link_columns:
        op.add_column("share_links", sa.Column("note", sa.Text(), nullable=True))

    if "updated_at" not in share_link_columns:
        op.add_column(
            "share_links",
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.execute("UPDATE share_links SET updated_at = created_at WHERE updated_at IS NULL")
        op.alter_column("share_links", "updated_at", nullable=False)

    access_log_columns = _columns("access_logs")
    if "actor_user_id" not in access_log_columns:
        op.add_column("access_logs", sa.Column("actor_user_id", sa.BigInteger(), nullable=True))
        if "user_id" in access_log_columns:
            op.execute("UPDATE access_logs SET actor_user_id = user_id WHERE actor_user_id IS NULL")
        op.create_foreign_key(
            "fk_access_logs_actor_user_id_users",
            "access_logs",
            "users",
            ["actor_user_id"],
            ["id"],
        )

    if "actor_type" not in access_log_columns:
        op.add_column(
            "access_logs",
            sa.Column("actor_type", sa.String(length=20), nullable=True),
        )
        if "access_type" in access_log_columns:
            op.execute(
                "UPDATE access_logs "
                "SET actor_type = CASE "
                "WHEN access_type::text IN ('INTERNAL_USER', 'CUSTOMER') THEN access_type::text "
                "WHEN access_type::text = 'INTERNAL' THEN 'INTERNAL_USER' "
                "WHEN access_type::text = 'EXTERNAL_LINK' THEN 'CUSTOMER' "
                "WHEN user_id IS NULL THEN 'CUSTOMER' "
                "ELSE 'INTERNAL_USER' END "
                "WHERE actor_type IS NULL"
            )
        else:
            op.execute("UPDATE access_logs SET actor_type = 'INTERNAL_USER' WHERE actor_type IS NULL")
        op.alter_column("access_logs", "actor_type", nullable=False)

    if "message" not in access_log_columns:
        op.add_column("access_logs", sa.Column("message", sa.String(length=500), nullable=True))
        if "failure_reason" in access_log_columns:
            op.execute("UPDATE access_logs SET message = failure_reason WHERE message IS NULL")


def downgrade() -> None:
    """Remove only the compatibility columns added in this revision."""
    access_log_columns = _columns("access_logs")
    if "message" in access_log_columns:
        op.drop_column("access_logs", "message")
    if "actor_type" in access_log_columns:
        op.drop_column("access_logs", "actor_type")
    if "actor_user_id" in access_log_columns:
        op.drop_constraint("fk_access_logs_actor_user_id_users", "access_logs", type_="foreignkey")
        op.drop_column("access_logs", "actor_user_id")

    share_link_columns = _columns("share_links")
    if "updated_at" in share_link_columns:
        op.drop_column("share_links", "updated_at")
    if "note" in share_link_columns:
        op.drop_column("share_links", "note")
    if "status" in share_link_columns:
        op.drop_column("share_links", "status")
    if "assigned_staff_user_id" in share_link_columns:
        op.drop_constraint(
            "fk_share_links_assigned_staff_user_id_users",
            "share_links",
            type_="foreignkey",
        )
        op.drop_column("share_links", "assigned_staff_user_id")
    if "client_name" in share_link_columns:
        op.drop_column("share_links", "client_name")

    user_columns = _columns("users")
    if "password" in user_columns:
        op.drop_column("users", "password")
