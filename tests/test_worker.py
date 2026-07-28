from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.db.models import Article, ArticleStatus, ArticleVersion, Base, Job, JobRun, JobStatus, PublishJob, PublishStatus
from app.jobs.queue import JobQueue
from app.jobs.worker import PublisherWorker
from app.publishing.client import PublishedPost, PublisherFailure


class SuccessfulPublisher:
    def publish(self, draft):
        return PublishedPost(url="https://example.tistory.com/101")

    def verify_private(self, post, draft):
        return None


class ExpiredLoginPublisher:
    def publish(self, draft):
        raise PublisherFailure("AUTH_REQUIRED", "login has expired")

    def verify_private(self, post, draft):
        raise AssertionError("verification must not run when login is expired")


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        auto_publish_enabled=True,
        tistory_production_enabled=True,
        tistory_expected_blog_name="생활꿀팁",
        tistory_allowed_category="테스트",
        tistory_profile_path=tmp_path / "profile",
    )


def add_ready_publish_job(session: Session, tmp_path: Path) -> int:
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"image")
    article = Article(content_hash="f" * 64, status=ArticleStatus.READY_TO_PUBLISH)
    session.add(article)
    session.flush()
    version = ArticleVersion(
        article_id=article.id,
        version_number=1,
        title="정부24 등본 저장 방법",
        body_html="<h1>등본 저장</h1><p>확인할 내용입니다.</p>",
        tags_json=["정부24", "등본"],
        category="테스트",
        thumbnail_path=str(thumbnail),
        content_hash="f" * 64,
        status=ArticleStatus.READY_TO_PUBLISH,
    )
    session.add(version)
    session.flush()
    publish = PublishJob(
        article_version_id=version.id,
        target_blog_name="생활꿀팁",
        category="테스트",
        visibility="PRIVATE",
        status=PublishStatus.PENDING,
    )
    session.add(publish)
    session.flush()
    job = Job(
        job_type="PUBLISH_TISTORY",
        entity_id=publish.id,
        payload_json={},
        status=JobStatus.PENDING,
        priority=90,
        max_attempts=2,
    )
    session.add(job)
    session.commit()
    return job.id


def test_worker_records_verified_private_publish(tmp_path: Path) -> None:
    """Removing success persistence would lose the URL and leave completed jobs runnable."""
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job_id = add_ready_publish_job(session, tmp_path)
        handled = PublisherWorker(session, settings_for(tmp_path), SuccessfulPublisher()).run_once("worker-a")
        assert handled is True

        job = session.get(Job, job_id)
        publish = session.scalar(select(PublishJob))
        run = session.scalar(select(JobRun))
        assert job.status is JobStatus.SUCCEEDED
        assert publish.status is PublishStatus.VERIFIED
        assert publish.result_url == "https://example.tistory.com/101"
        assert run.finished_at is not None


def test_worker_does_not_retry_expired_login(tmp_path: Path) -> None:
    """Marking authentication errors retryable would repeatedly drive an expired browser profile."""
    engine = create_engine(f"sqlite:///{tmp_path / 'expired-login.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job_id = add_ready_publish_job(session, tmp_path)
        PublisherWorker(session, settings_for(tmp_path), ExpiredLoginPublisher()).run_once("worker-a")

        job = session.get(Job, job_id)
        publish = session.scalar(select(PublishJob))
        assert job.status is JobStatus.FAILED
        assert job.last_error_code == "AUTH_REQUIRED"
        assert publish.status is PublishStatus.AUTH_REQUIRED
