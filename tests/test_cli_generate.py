from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.content.static import register_private_article
from app.db.models import Article, Job
from app.db.session import create_session_factory
from app.llm.generator import GeneratedArticle


def test_generate_and_enqueue_flow(tmp_path):
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    factory = create_session_factory(db_url)

    from alembic.config import Config
    from alembic import command
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")

    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(b"dummy image data")

    generated = GeneratedArticle(
        title="자동 생성 등본 가이드",
        body_html="<h2>안내</h2><p>본문 내용입니다.</p>",
        tags=["등본", "정부24"],
        summary="요약 내용",
    )

    mock_generator = MagicMock()
    mock_generator.generate.return_value = generated

    with factory() as session:
        from app.content.static import StaticArticleInput
        registered = register_private_article(
            session,
            StaticArticleInput(
                title=generated.title,
                body_html=generated.body_html,
                tags=generated.tags,
                category="생활행정",
                target_blog_name="testblog",
                thumbnail_path=thumbnail,
            ),
        )
        session.commit()

        article = session.scalar(select(Article).where(Article.id == registered.article_id))
        job = session.scalar(select(Job).where(Job.id == registered.job_id))

        assert article is not None
        assert job is not None
        assert job.job_type == "PUBLISH_TISTORY"
