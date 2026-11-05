"""Enterprise AI Platform initial schema."""

from pathlib import Path

from alembic import op
from sqlalchemy import inspect

revision = "0001_platform"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    inspector = inspect(connection)
    if (set(inspector.get_table_names()) - {"alembic_version"}
            or inspector.get_view_names() or inspector.get_materialized_view_names()):
        raise RuntimeError("Platform initialization requires an empty, isolated schema")
    connection.exec_driver_sql(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))


def downgrade():
    raise RuntimeError("Platform baseline cannot be discarded; schema retirement requires separate authorization")
