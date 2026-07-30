from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Article, ArticleStatus, ArticleVersion, Job, JobStatus, MediaAsset, PublishJob, PublishStatus


@dataclass(frozen=True)
class StaticArticleInput:
    title: str
    body_html: str
    tags: list[str]
    category: str
    target_blog_name: str
    thumbnail_path: Path


@dataclass(frozen=True)
class DraftArticleInput:
    title: str
    body_html: str
    tags: list[str]
    category: str
    thumbnail_path: Path


@dataclass(frozen=True)
class RegisteredArticle:
    article_id: int
    article_version_id: int
    publish_job_id: int
    job_id: int


@dataclass(frozen=True)
class RegisteredDraft:
    article_id: int
    article_version_id: int


def content_hash_for(article: StaticArticleInput | DraftArticleInput) -> str:
    normalized = "\n".join(
        [
            _normalize(article.title),
            _normalize(article.body_html),
            ",".join(sorted(_normalize(tag).lower() for tag in article.tags)),
        ]
    )
    return sha256(normalized.encode("utf-8")).hexdigest()


def register_private_article(session: Session, article_input: StaticArticleInput) -> RegisteredArticle:
    if not article_input.thumbnail_path.is_file():
        raise ValueError("thumbnail file does not exist")
    if not article_input.title.strip() or not article_input.body_html.strip() or not article_input.tags:
        raise ValueError("article content is incomplete")
    digest = content_hash_for(article_input)
    if session.scalar(select(Article.id).where(Article.content_hash == digest)) is not None:
        raise ValueError("duplicate content")

    article = Article(content_hash=digest, status=ArticleStatus.READY_TO_PUBLISH)
    session.add(article)
    session.flush()
    version = ArticleVersion(
        article_id=article.id,
        version_number=1,
        title=article_input.title.strip(),
        body_html=article_input.body_html,
        tags_json=article_input.tags,
        category=article_input.category,
        thumbnail_path=str(article_input.thumbnail_path),
        content_hash=digest,
        status=ArticleStatus.READY_TO_PUBLISH,
    )
    session.add(version)
    session.flush()
    session.add(MediaAsset(article_version_id=version.id, kind="THUMBNAIL", local_path=str(article_input.thumbnail_path)))
    publish_job = PublishJob(
        article_version_id=version.id,
        target_blog_name=article_input.target_blog_name,
        category=article_input.category,
        visibility="PRIVATE",
        status=PublishStatus.PENDING,
    )
    session.add(publish_job)
    session.flush()
    job = Job(
        job_type="PUBLISH_TISTORY",
        entity_id=publish_job.id,
        payload_json={},
        status=JobStatus.PENDING,
        priority=90,
        max_attempts=2,
    )
    session.add(job)
    session.flush()
    return RegisteredArticle(article.id, version.id, publish_job.id, job.id)


def register_draft_article(session: Session, article_input: DraftArticleInput) -> RegisteredDraft:
    if not article_input.thumbnail_path.is_file():
        raise ValueError("thumbnail file does not exist")
    if not article_input.title.strip() or not article_input.body_html.strip() or not article_input.tags:
        raise ValueError("article content is incomplete")
    digest = content_hash_for(article_input)
    if session.scalar(select(Article.id).where(Article.content_hash == digest)) is not None:
        raise ValueError("duplicate content")

    article = Article(content_hash=digest, status=ArticleStatus.DRAFT)
    session.add(article)
    session.flush()
    version = ArticleVersion(
        article_id=article.id,
        version_number=1,
        title=article_input.title.strip(),
        body_html=article_input.body_html,
        tags_json=article_input.tags,
        category=article_input.category,
        thumbnail_path=str(article_input.thumbnail_path),
        content_hash=digest,
        status=ArticleStatus.DRAFT,
    )
    session.add(version)
    session.flush()
    session.add(MediaAsset(article_version_id=version.id, kind="THUMBNAIL", local_path=str(article_input.thumbnail_path)))
    session.flush()
    return RegisteredDraft(article.id, version.id)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
