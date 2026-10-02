"""add topic candidates and draft article status

Revision ID: 20260730_02
Revises: 20260727_01
Create Date: 2026-07-30
"""

from alembic import context, op
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
topic_candidate_columns = (
    "id",
    "batch_id",
    "topic",
    "topic_hash",
    "category",
    "reason",
    "sources_json",
    "status",
    "article_id",
    "error_message",
    "created_at",
    "updated_at",
)


def _existing_topic_candidates(bind) -> tuple[bool, set[str]]:
    """topic_candidates 테이블이 이미 있는지와 그 인덱스 이름들.

    MySQL DDL은 롤백되지 않아, 이전 실행이 테이블만 만들고 버전 기록 전에 실패했거나 테이블을 손으로 만든 DB가 있다.
    그런 DB에서 CREATE TABLE이 'already exists'로 실패하지 않도록 있는 것은 건너뛴다. 단, 컬럼이 빠진 테이블은
    스키마가 다르다는 뜻이라 그대로 두고 멈춘다(조용히 버전만 올리면 앱이 실행 중에 깨진다).
    오프라인(--sql) 모드에서는 DB를 읽을 수 없으므로 항상 만든다.
    """
    if context.is_offline_mode():
        return False, set()
    inspector = sa.inspect(bind)
    if not inspector.has_table("topic_candidates"):
        return False, set()
    columns = {column["name"] for column in inspector.get_columns("topic_candidates")}
    missing = [name for name in topic_candidate_columns if name not in columns]
    if missing:
        raise RuntimeError(
            "topic_candidates table already exists but is missing columns: "
            + ", ".join(missing)
            + ". Fix or drop the table, then run init-db again."
        )
    return True, {index["name"] for index in inspector.get_indexes("topic_candidates")}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # 이미 DRAFT가 들어 있어도 같은 정의로 다시 바꾸는 것뿐이라 여러 번 실행해도 안전하다.
        values = ", ".join(f"'{value}'" for value in article_status_values)
        op.execute(f"ALTER TABLE articles MODIFY status ENUM({values}) NOT NULL")
        op.execute(f"ALTER TABLE article_versions MODIFY status ENUM({values}) NOT NULL")

    table_exists, existing_indexes = _existing_topic_candidates(bind)
    if table_exists:
        if "ix_topic_candidates_batch_id" not in existing_indexes:
            op.create_index("ix_topic_candidates_batch_id", "topic_candidates", ["batch_id"])
        return

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
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # MySQL DDL은 트랜잭션이 아니라서, 안전 검사를 먼저 하지 않으면 테이블을 지운 뒤에 실패해 반쯤 내려간 상태가 남는다.
        draft_articles = bind.execute(sa.text("SELECT COUNT(*) FROM articles WHERE status = 'DRAFT'")).scalar_one()
        draft_versions = bind.execute(sa.text("SELECT COUNT(*) FROM article_versions WHERE status = 'DRAFT'")).scalar_one()
        if draft_articles or draft_versions:
            raise RuntimeError("cannot downgrade while DRAFT articles exist")

    op.drop_index("ix_topic_candidates_batch_id", table_name="topic_candidates")
    op.drop_table("topic_candidates")

    if bind.dialect.name == "mysql":
        values = ", ".join(f"'{value}'" for value in article_status_values if value != "DRAFT")
        op.execute(f"ALTER TABLE articles MODIFY status ENUM({values}) NOT NULL")
        op.execute(f"ALTER TABLE article_versions MODIFY status ENUM({values}) NOT NULL")
