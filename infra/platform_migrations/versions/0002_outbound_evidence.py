"""Persist authoritative safe outbound evidence on model calls.

Existing calls intentionally retain NULL evidence and cannot authorize fallback.
"""

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_outbound_evidence"
down_revision = "0001_platform"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_platform_model_call", Column("outbound_evidence", JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_ai_platform_model_call_outbound_evidence", "ai_platform_model_call",
        "outbound_evidence IS NULL OR jsonb_typeof(outbound_evidence) = 'object'",
    )


def downgrade():
    raise RuntimeError("Outbound financial/retry evidence cannot be discarded; schema retirement requires separate authorization")
