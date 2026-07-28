from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.content.static import StaticArticleInput, register_private_article
from app.db.models import Base, Job, PublishJob


def article_input(tmp_path: Path) -> StaticArticleInput:
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"image")
    return StaticArticleInput(
        title="정부24 등본 저장 방법",
        body_html="<h1>등본 저장</h1><p>확인할 내용입니다.</p>",
        tags=["정부24", "등본"],
        category="테스트",
        target_blog_name="생활꿀팁",
        thumbnail_path=thumbnail,
    )


def test_registering_same_normalized_content_twice_is_rejected(tmp_path: Path) -> None:
    """Removing the content-hash lookup would enqueue duplicate publication jobs."""
    engine = create_engine(f"sqlite:///{tmp_path / 'registration.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        created = register_private_article(session, article_input(tmp_path))
        session.commit()

        assert created.publish_job_id > 0
        assert session.query(Job).count() == 1
        assert session.query(PublishJob).one().visibility == "PRIVATE"
        with pytest.raises(ValueError, match="duplicate content"):
            register_private_article(session, article_input(tmp_path))
