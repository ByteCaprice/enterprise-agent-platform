"""Permit governed custom steps and the code Workflow result receipt.

No existing artifacts, bindings or business capabilities are modified or seeded.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_business_code"
down_revision = "0002_outbound_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_ai_resource_grant_action", "ai_resource_grant", type_="check")
    op.create_check_constraint(
        "ck_ai_resource_grant_action", "ai_resource_grant",
        "action IN ('capability:read','capability:write','capability:publish','prompt:render',"
        "'task:submit','task:read','task:cancel','model:invoke','tool:invoke','agent:invoke','workflow:invoke',"
        "'memory:read','memory:write','quality:run','quality:review','skill:use','feedback:read','feedback:write')",
    )
    op.add_column("ai_workflow_node", sa.Column("parent_node_id", sa.Text(), nullable=True))
    op.add_column("ai_workflow_node", sa.Column("scope", postgresql.JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_ai_workflow_node_parent", "ai_workflow_node", "ai_workflow_node",
        ["task_id", "task_attempt_id", "parent_node_id"], ["task_id", "task_attempt_id", "id"],
    )
    op.create_check_constraint("ck_ai_workflow_node_parent", "ai_workflow_node",
                               "parent_node_id IS NULL OR parent_node_id != id")
    op.create_check_constraint("ck_ai_workflow_node_scope", "ai_workflow_node",
                               "scope IS NULL OR jsonb_typeof(scope) = 'object'")
    op.drop_constraint("ck_ai_workflow_node_kind", "ai_workflow_node", type_="check")
    op.create_check_constraint(
        "ck_ai_workflow_node_kind", "ai_workflow_node", "kind IN ('rule','agent','tool','llm','step','result','workflow')",
    )
    op.drop_constraint("ck_ai_workflow_node_status", "ai_workflow_node", type_="check")
    op.create_check_constraint("ck_ai_workflow_node_status", "ai_workflow_node",
                               "status IN ('running','succeeded','failed','skipped','unavailable','cancelled')")
    op.drop_constraint("ck_ai_workflow_node_terminal", "ai_workflow_node", type_="check")
    op.create_check_constraint(
        "ck_ai_workflow_node_terminal", "ai_workflow_node",
        "(status = 'running' AND finish_time IS NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NULL) OR "
        "(status = 'succeeded' AND finish_time IS NOT NULL AND output IS NOT NULL AND output_hash IS NOT NULL AND error_code IS NULL) OR "
        "(status = 'skipped' AND finish_time IS NOT NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NULL) OR "
        "(status IN ('failed','cancelled','unavailable') AND finish_time IS NOT NULL AND output IS NULL AND output_hash IS NULL "
        "AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$')",
    )


def downgrade():
    raise RuntimeError("Business execution evidence cannot be discarded; schema retirement requires separate authorization")
