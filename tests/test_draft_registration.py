from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.content.static import DraftArticleInput, enqueue_draft_article, register_draft_article
from app.db.models import Article, ArticleStatus, ArticleVersion, Base, Job, MediaAsset, PublishJob


def draft_input(tmp_path: Path) -> DraftArticleInput:
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"image")
    return DraftArticleInput(
        title="여름 휴가철 국내 기차표 예매 팁",
        body_html="<h1>기차표 예매</h1><p>초안 본문입니다.</p>",
        tags=["여행", "기차표"],
        category="여행꿀팁",
        thumbnail_path=thumbnail,
    )


def test_register_draft_creates_article_without_publish_queue(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'draft.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        registered = register_draft_article(session, draft_input(tmp_path))
        session.commit()

        article = session.get(Article, registered.article_id)
        version = session.get(ArticleVersion, registered.article_version_id)
        assert article is not None
        assert version is not None
        assert article.status == ArticleStatus.DRAFT
        assert version.status == ArticleStatus.DRAFT
        assert session.query(MediaAsset).count() == 1
        assert session.query(PublishJob).count() == 0
        assert session.query(Job).count() == 0


def test_register_draft_rejects_duplicate_content(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'draft_duplicate.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        article = draft_input(tmp_path)
        register_draft_article(session, article)
        session.commit()

        with pytest.raises(ValueError, match="duplicate content"):
            register_draft_article(session, article)


def test_enqueue_draft_creates_private_publish_queue(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'draft_enqueue.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        draft = register_draft_article(session, draft_input(tmp_path))
        queued = enqueue_draft_article(session, draft.article_id, "example-blog")
        session.commit()

        article = session.get(Article, queued.article_id)
        version = session.get(ArticleVersion, queued.article_version_id)
        publish_job = session.get(PublishJob, queued.publish_job_id)
        job = session.get(Job, queued.job_id)
        assert article is not None and article.status == ArticleStatus.READY_TO_PUBLISH
        assert version is not None and version.status == ArticleStatus.READY_TO_PUBLISH
        assert publish_job is not None and publish_job.visibility == "PRIVATE"
        assert job is not None and job.status.value == "PENDING"
