"""add topic candidates and draft article status

Revision ID: 20260730_02
Revises: 20260727_01
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_02"
down_revision = "20260727_01"
branch_labels = None
depends_on = None


article_status_values = (
    "DRAFT",
    "READY_TO_PUBLISH",
    "PUBLISHING",
    "VERIFIED",
    "QUARANTINED",
)
article_status = sa.Enum(*article_status_values, name="article_status")
topic_candidate_status = sa.Enum("NEW", "GENERATING", "DRAFT_CREATED", "FAILED", name="topic_candidate_status")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        values = ", ".join(f"'{value}'" for value in article_status_values)
        op.execute(f"ALTER TABLE articles MODIFY status ENUM({values}) NOT NULL")
        op.execute(f"ALTER TABLE article_versions MODIFY status ENUM({values}) NOT NULL")

    op.create_table(
        "topic_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("topic_hash", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("sources_json", sa.JSON(), nullable=False),
        sa.Column("status", topic_candidate_status, nullable=False),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id", ondelete="SET NULL"), unique=True),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "topic_hash", name="uq_topic_candidate_batch_hash"),
    )
    op.create_index("ix_topic_candidates_batch_id", "topic_candidates", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_topic_candidates_batch_id", table_name="topic_candidates")
    op.drop_table("topic_candidates")

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        draft_count = bind.execute(sa.text("SELECT COUNT(*) FROM articles WHERE status = 'DRAFT'")).scalar_one()
        if draft_count:
            raise RuntimeError("cannot downgrade while DRAFT articles exist")
        values = ", ".join(f"'{value}'" for value in article_status_values if value != "DRAFT")
        op.execute(f"ALTER TABLE articles MODIFY status ENUM({values}) NOT NULL")
        op.execute(f"ALTER TABLE article_versions MODIFY status ENUM({values}) NOT NULL")
