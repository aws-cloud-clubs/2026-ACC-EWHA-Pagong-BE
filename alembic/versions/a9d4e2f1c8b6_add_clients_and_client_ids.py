"""add clients and client ids

Revision ID: a9d4e2f1c8b6
Revises: f6b8c7d2e9a4
Create Date: 2026-05-28 10:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9d4e2f1c8b6"
down_revision: Union[str, None] = "f6b8c7d2e9a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Create clients table and connect projects/share links by client_id."""
    tables = _tables()
    if "clients" not in tables:
        op.create_table(
            "clients",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
        op.create_index(op.f("ix_clients_id"), "clients", ["id"], unique=False)

    op.execute(
        """
        INSERT INTO clients (id, name)
        VALUES (1, 'A뷰티'), (2, 'B식품')
        ON CONFLICT (id) DO NOTHING
        """
    )

    project_columns = _columns("projects")
    if "client_id" not in project_columns:
        op.add_column("projects", sa.Column("client_id", sa.BigInteger(), nullable=True))
        op.execute(
            """
            INSERT INTO clients (id, name)
            SELECT row_number() OVER (ORDER BY client_name) + COALESCE((SELECT max(id) FROM clients), 0), client_name
            FROM (
                SELECT DISTINCT client_name
                FROM projects
                WHERE client_name IS NOT NULL
                  AND client_name <> ''
                  AND client_name NOT IN (SELECT name FROM clients)
            ) names
            """
        )
        op.execute(
            """
            UPDATE projects
            SET client_id = clients.id
            FROM clients
            WHERE projects.client_name = clients.name
              AND projects.client_id IS NULL
            """
        )
        op.create_foreign_key(
            "fk_projects_client_id_clients",
            "projects",
            "clients",
            ["client_id"],
            ["id"],
        )

    share_link_columns = _columns("share_links")
    if "client_id" not in share_link_columns:
        op.add_column("share_links", sa.Column("client_id", sa.BigInteger(), nullable=True))
        if "client_name" in share_link_columns:
            op.execute(
                """
                INSERT INTO clients (id, name)
                SELECT row_number() OVER (ORDER BY client_name) + COALESCE((SELECT max(id) FROM clients), 0), client_name
                FROM (
                    SELECT DISTINCT client_name
                    FROM share_links
                    WHERE client_name IS NOT NULL
                      AND client_name <> ''
                      AND client_name NOT IN (SELECT name FROM clients)
                ) names
                """
            )
            op.execute(
                """
                UPDATE share_links
                SET client_id = clients.id
                FROM clients
                WHERE share_links.client_name = clients.name
                  AND share_links.client_id IS NULL
                """
            )
        op.create_foreign_key(
            "fk_share_links_client_id_clients",
            "share_links",
            "clients",
            ["client_id"],
            ["id"],
        )


def downgrade() -> None:
    """Remove client_id compatibility columns and clients table."""
    share_link_columns = _columns("share_links")
    if "client_id" in share_link_columns:
        op.drop_constraint("fk_share_links_client_id_clients", "share_links", type_="foreignkey")
        op.drop_column("share_links", "client_id")

    project_columns = _columns("projects")
    if "client_id" in project_columns:
        op.drop_constraint("fk_projects_client_id_clients", "projects", type_="foreignkey")
        op.drop_column("projects", "client_id")

    if "clients" in _tables():
        op.drop_index(op.f("ix_clients_id"), table_name="clients")
        op.drop_table("clients")
