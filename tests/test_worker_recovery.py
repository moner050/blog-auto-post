"""워커/큐의 복구·일관성 테스트 (SQLite 파일 DB + 가짜 발행기, 브라우저 없음).

핵심: 워커가 죽거나 중단돼도 잡이 RUNNING으로 영구 정지하지 않고, 이미 글이 올라갔을 수 있는 잡은 절대 자동 재실행되지 않는다.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import app.jobs.worker as worker_module
from app.core.settings import Settings
from app.db.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    Base,
    Job,
    JobRun,
    JobStatus,
    PublishJob,
    PublishStatus,
    utc_now,
)
from app.jobs.queue import JobQueue, as_utc
from app.jobs.worker import PublisherWorker, resolve_thumbnail_path
from app.publishing.client import PublishedPost, PublisherFailure

POST_URL = "https://example.tistory.com/101"


# --- 준비물 ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """스키마만 만든 SQLite 파일을 한 번 만들어 두고 테스트마다 복사한다(create_all이 테스트당 0.3초라서)."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    template_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(template_engine)
    template_engine.dispose()
    return path


@pytest.fixture
def engine(tmp_path: Path, template_db: Path):  # noqa: ANN201
    shutil.copyfile(template_db, tmp_path / "worker.db")
    # 다른 세션이 쓰기 락에 막히면 1초 만에 'database is locked'로 드러나게 한다.
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}", connect_args={"timeout": 1})
    yield engine
    engine.dispose()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,  # 실제 .env를 읽지 않는다
        "database_url": f"sqlite:///{tmp_path / 'worker.db'}",
        "auto_publish_enabled": True,
        "tistory_production_enabled": True,
        "tistory_expected_blog_name": "생활꿀팁",
        "tistory_allowed_category": "테스트",
        "tistory_profile_path": tmp_path / "profile",
    }
    values.update(overrides)
    return Settings(**values)


def add_job(
    session: Session,
    tmp_path: Path,
    n: int = 1,
    *,
    thumbnail: str | None = None,
    visibility: str = "PRIVATE",
    job_type: str = "PUBLISH_TISTORY",
) -> SimpleNamespace:
    """Article/Version/PublishJob/Job 한 벌(PENDING)을 만든다."""
    if thumbnail is None:
        image = tmp_path / f"thumb-{n}.png"
        image.write_bytes(b"image")
        thumbnail = str(image)
    article = Article(content_hash=f"{n:064d}", status=ArticleStatus.READY_TO_PUBLISH)
    session.add(article)
    session.flush()
    version = ArticleVersion(
        article_id=article.id,
        version_number=1,
        title=f"정부24 등본 저장 방법 {n}",
        body_html="<h1>등본 저장</h1><p>확인할 내용입니다.</p>",
        tags_json=["정부24", "등본"],
        category="테스트",
        thumbnail_path=thumbnail,
        content_hash=f"{n:064d}",
        status=ArticleStatus.READY_TO_PUBLISH,
    )
    session.add(version)
    session.flush()
    publish = PublishJob(
        article_version_id=version.id,
        target_blog_name="생활꿀팁",
        category="테스트",
        visibility=visibility,
        status=PublishStatus.PENDING,
    )
    session.add(publish)
    session.flush()
    job = Job(
        job_type=job_type,
        entity_id=publish.id,
        payload_json={},
        status=JobStatus.PENDING,
        priority=90,
        max_attempts=2,
    )
    session.add(job)
    session.commit()
    return SimpleNamespace(job=job.id, publish=publish.id, version=version.id, article=article.id, title=version.title)


def mark_running(session: Session, ids: SimpleNamespace, locked_at: datetime, *, worker: str = "dead-worker") -> None:
    """워커가 브라우저 단계에서 죽은 상태를 만든다: 잡 RUNNING + 열린 JobRun + PUBLISHING."""
    job = session.get(Job, ids.job)
    job.status = JobStatus.RUNNING
    job.locked_by = worker
    job.locked_at = locked_at
    job.started_at = locked_at
    job.attempt_count = 1
    session.add(JobRun(job_id=job.id, worker_id=worker, status=JobStatus.RUNNING, started_at=locked_at))
    session.get(PublishJob, ids.publish).status = PublishStatus.PUBLISHING
    session.get(ArticleVersion, ids.version).status = ArticleStatus.PUBLISHING
    session.get(Article, ids.article).status = ArticleStatus.PUBLISHING
    session.commit()


def snapshot(engine, ids: SimpleNamespace) -> SimpleNamespace:  # noqa: ANN001
    """새 세션으로 읽은 현재 DB 상태(다른 세션에서 본 값)."""
    with Session(engine) as session:
        job = session.get(Job, ids.job)
        publish = session.get(PublishJob, ids.publish)
        version = session.get(ArticleVersion, ids.version)
        article = session.get(Article, ids.article)
        runs = session.scalars(select(JobRun).where(JobRun.job_id == ids.job).order_by(JobRun.id)).all()
        return SimpleNamespace(
            job=job.status,
            code=job.last_error_code,
            message=job.last_error_message,
            attempts=job.attempt_count,
            locked_by=job.locked_by,
            run_after=job.run_after,
            publish=publish.status,
            result_url=publish.result_url,
            details=publish.verification_details,
            version=version.status,
            article=article.status,
            runs=[(run.status, run.finished_at is not None, run.error_code) for run in runs],
        )


class ScriptedPublisher:
    """publish()/verify_private() 동작을 정해 주는 가짜 발행기."""

    def __init__(
        self,
        *,
        url: str = POST_URL,
        publish_error: BaseException | None = None,
        verify_error: BaseException | None = None,
        evidence: str | None = None,
        on_publish: Callable[[Any], None] | None = None,
        on_verify: Callable[[Any, Any], None] | None = None,
    ) -> None:
        self.url = url
        self.publish_error = publish_error
        self.verify_error = verify_error
        self.evidence = evidence
        self.on_publish = on_publish
        self.on_verify = on_verify
        self.calls: list[Any] = []

    def publish(self, draft: Any) -> PublishedPost:
        self.calls.append(draft)
        if self.on_publish is not None:
            self.on_publish(draft)
        if self.publish_error is not None:
            raise self.publish_error
        return PublishedPost(self.url)

    def verify_private(self, post: PublishedPost, draft: Any) -> str | None:
        if self.on_verify is not None:
            self.on_verify(post, draft)
        if self.verify_error is not None:
            raise self.verify_error
        return self.evidence


def run_once(engine, tmp_path: Path, publisher: ScriptedPublisher, **settings: Any) -> tuple[bool, str | None]:  # noqa: ANN001
    with Session(engine) as session:
        worker = PublisherWorker(session, make_settings(tmp_path, **settings), publisher)
        handled = worker.run_once("worker-test")
        return handled, worker.last_outcome


# =================================================================================================================
# P4: write-ahead 커밋 — 브라우저 단계 동안 다른 세션에서 RUNNING/PUBLISHING이 보여야 한다
# =================================================================================================================
def test_running_publishing_and_jobrun_are_committed_before_the_browser_step(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """flush만 하고 커밋하지 않으면 워커가 죽을 때 흔적이 없고, 열린 쓰기 트랜잭션이 DB 락을 몇 분간 쥔다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    observed: dict[str, Any] = {}

    def observe(draft: Any) -> None:
        with Session(engine) as other:  # 다른 연결에서 본 상태
            observed["job"] = other.get(Job, ids.job).status
            observed["publish"] = other.get(PublishJob, ids.publish).status
            observed["version"] = other.get(ArticleVersion, ids.version).status
            observed["article"] = other.get(Article, ids.article).status
            observed["runs"] = [(run.status, run.finished_at) for run in other.scalars(select(JobRun))]
            try:  # 쓰기 락을 쥐고 있다면 timeout(1s) 뒤 'database is locked'
                other.add(Article(content_hash="9" * 64, status=ArticleStatus.DRAFT))
                other.commit()
                observed["other_write"] = "committed"
            except Exception as error:  # noqa: BLE001
                observed["other_write"] = f"{type(error).__name__}: {error}"

    handled, _ = run_once(engine, tmp_path, ScriptedPublisher(on_publish=observe))

    assert handled is True
    assert observed["job"] is JobStatus.RUNNING
    assert observed["publish"] is PublishStatus.PUBLISHING
    assert observed["version"] is ArticleStatus.PUBLISHING and observed["article"] is ArticleStatus.PUBLISHING
    assert observed["runs"] == [(JobStatus.RUNNING, None)]
    assert observed["other_write"] == "committed"


def test_post_url_is_committed_before_private_verification_runs(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """P3: 검증 중에 죽거나 실패해도 (공개일 수 있는) 글의 주소가 DB에 남아 있어야 한다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    seen: dict[str, Any] = {}

    def observe(post: PublishedPost, draft: Any) -> None:
        with Session(engine) as other:
            publish = other.get(PublishJob, ids.publish)
            seen["url"], seen["status"] = publish.result_url, publish.status

    run_once(engine, tmp_path, ScriptedPublisher(on_verify=observe))

    assert seen == {"url": POST_URL, "status": PublishStatus.PUBLISHING}


def test_success_records_verified_state_and_the_evidence(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    handled, outcome = run_once(engine, tmp_path, ScriptedPublisher(evidence="HTTP 404"))

    state = snapshot(engine, ids)
    assert (handled, outcome) == (True, "SUCCEEDED")
    assert state.job is JobStatus.SUCCEEDED and state.publish is PublishStatus.VERIFIED
    assert state.version is ArticleStatus.VERIFIED and state.article is ArticleStatus.VERIFIED
    assert state.result_url == POST_URL
    assert state.details == {"visibility": "PRIVATE", "title": ids.title, "evidence": "HTTP 404"}
    assert state.runs == [(JobStatus.SUCCEEDED, True, None)]


def test_success_without_evidence_keeps_the_original_details_shape(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(evidence=None))

    assert snapshot(engine, ids).details == {"visibility": "PRIVATE", "title": ids.title}


# =================================================================================================================
# P3: 검증 실패 시 URL 보존 + 상태 일관성(P5)
# =================================================================================================================
def test_failed_verification_keeps_the_url_and_quarantines_the_article(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """이전에는 검증이 실패하면 post.url을 버려 공개일 수 있는 글을 다시 찾을 수 없었고, 글은 PUBLISHING에 남았다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    publisher = ScriptedPublisher(verify_error=PublisherFailure("PUBLISH_UNVERIFIED", "unauthenticated page showed the post"))

    handled, outcome = run_once(engine, tmp_path, publisher)

    state = snapshot(engine, ids)
    assert (handled, outcome) == (True, "FAILED")
    assert state.job is JobStatus.FAILED and state.code == "PUBLISH_UNVERIFIED"
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED
    assert state.result_url == POST_URL and POST_URL in state.message
    assert state.version is ArticleStatus.QUARANTINED and state.article is ArticleStatus.QUARANTINED
    assert state.runs == [(JobStatus.FAILED, True, "PUBLISH_UNVERIFIED")]


def test_adapter_failure_that_carries_a_post_url_is_stored_as_result_url(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """어댑터가 publish() 안에서 소유자 검증에 실패하면 PublishedPost를 못 돌려주므로 실패에 실린 주소를 보존한다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    failure = PublisherFailure("PUBLISH_UNVERIFIED", "owner check failed", post_url="https://example.tistory.com/77")

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=failure))

    state = snapshot(engine, ids)
    assert state.result_url == "https://example.tistory.com/77"
    assert "https://example.tistory.com/77" in state.message


@pytest.mark.parametrize(
    "failure, publish_status",
    [
        (PublisherFailure("PUBLISH_UNVERIFIED", "x"), PublishStatus.PUBLISH_UNVERIFIED),
        (PublisherFailure("UI_BROKEN", "x"), PublishStatus.UI_BROKEN),
        (PublisherFailure("AUTH_REQUIRED", "x"), PublishStatus.AUTH_REQUIRED),
        # 어댑터 밖으로 새어 나온 예기치 못한 오류: 브라우저 단계(PUBLISHING)에서 끝났으니 글이 이미 있을 수 있다.
        (RuntimeError("boom"), PublishStatus.PUBLISH_UNVERIFIED),
        (PublisherFailure("SOMETHING_NEW", "x"), PublishStatus.PUBLISH_UNVERIFIED),
    ],
)
def test_failures_after_publishing_quarantine_article_and_version(engine, tmp_path: Path, failure: Exception, publish_status: PublishStatus) -> None:  # noqa: ANN001
    """QUARANTINED가 정의만 있고 할당되지 않아 실패한 글이 PUBLISHING으로 남던 문제."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=failure))

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED
    assert state.publish is publish_status
    assert state.version is ArticleStatus.QUARANTINED and state.article is ArticleStatus.QUARANTINED


def test_unexpected_error_message_names_the_exception_type(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=ValueError("bad")))

    state = snapshot(engine, ids)
    assert state.code == "UNEXPECTED_ERROR" and state.message == "ValueError: bad"


def test_preflight_block_leaves_the_article_ready_to_publish_and_never_calls_the_publisher(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path, visibility="PUBLIC")  # 프리플라이트가 막는 행
    publisher = ScriptedPublisher()

    handled, outcome = run_once(engine, tmp_path, publisher)

    state = snapshot(engine, ids)
    assert (handled, outcome) == (True, "FAILED") and publisher.calls == []
    assert state.job is JobStatus.FAILED and state.code == "PREFLIGHT_FAILED"
    assert state.publish is PublishStatus.BLOCKED
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH


def test_adapter_preflight_block_after_the_write_ahead_restores_ready_to_publish(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """어댑터가 PREFLIGHT_FAILED로 막으면(브라우저를 열기 전) PUBLISHING으로 바뀐 상태를 발행 대기로 되돌린다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=PublisherFailure("PREFLIGHT_FAILED", "only PRIVATE visibility may be published")))

    state = snapshot(engine, ids)
    assert state.publish is PublishStatus.BLOCKED
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH


def test_unsupported_job_type_does_not_touch_a_publish_job_with_the_same_id(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """entity_id가 PublishJob id가 아닌 잡이 우연히 같은 id의 발행 잡·글 상태를 건드리면 안 된다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path, job_type="SOMETHING_ELSE")

    handled, outcome = run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert (handled, outcome) == (True, "FAILED")
    assert state.code == "UNSUPPORTED_JOB"
    assert state.publish is PublishStatus.PENDING
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH


def test_retryable_failure_waits_for_a_retry_without_quarantining(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """재시도 대기 중인 잡의 글을 격리 상태로 두면 다음 실행 전까지 사람 확인이 필요한 것처럼 보인다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    handled, outcome = run_once(engine, tmp_path, ScriptedPublisher(publish_error=PublisherFailure("UI_BROKEN", "x", retryable=True)))

    state = snapshot(engine, ids)
    assert (handled, outcome) == (True, "FAILED")
    assert state.job is JobStatus.RETRY_WAIT and state.run_after is not None
    assert state.publish is PublishStatus.PENDING
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH


def test_a_possibly_published_failure_is_never_retried_even_if_marked_retryable(engine, tmp_path: Path) -> None:
    """글이 이미 있을 수 있는 PUBLISH_UNVERIFIED를 자동 재시도하면 중복 글이 생긴다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=PublisherFailure("PUBLISH_UNVERIFIED", "x", retryable=True)))

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED
    assert state.version is ArticleStatus.QUARANTINED


def test_a_broken_database_transaction_is_rolled_back_so_the_job_is_not_left_running(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """브라우저 단계의 DB 오류로 세션이 롤백 필요 상태가 돼도 실패를 기록할 수 있어야 한다(잡이 RUNNING으로 남지 않게)."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        existing_hash = f"{1:064d}"

    with Session(engine) as session:
        def break_session(draft: Any) -> None:
            session.add(Article(content_hash=existing_hash, status=ArticleStatus.DRAFT))
            session.flush()  # UNIQUE 위반 → IntegrityError가 그대로 올라가고 세션은 롤백이 필요한 상태가 된다

        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher(on_publish=break_session))
        assert worker.run_once("worker-test") is True

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.code == "UNEXPECTED_ERROR"
    assert state.message.startswith("IntegrityError")
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED  # 브라우저 단계의 예기치 못한 오류: 글이 있을 수 있다
    assert state.version is ArticleStatus.QUARANTINED


# =================================================================================================================
# P4: 만료된 RUNNING 잡 복구 — 절대 PENDING으로 되돌리지 않는다
# =================================================================================================================
def test_stale_running_jobs_are_quarantined_while_fresh_ones_are_untouched(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """강제 종료된 워커의 잡은 영구 RUNNING이었다. 만료되면 수동 확인 상태로 돌리고(PENDING 금지), 새 잡은 건드리지 않는다."""
    now = utc_now()
    with Session(engine) as session:
        stale_aware = add_job(session, tmp_path, 1)
        stale_naive = add_job(session, tmp_path, 2)
        fresh = add_job(session, tmp_path, 3)
        pending = add_job(session, tmp_path, 4)
        mark_running(session, stale_aware, now - timedelta(hours=2))
        mark_running(session, stale_naive, (now - timedelta(hours=2)).replace(tzinfo=None))  # DB가 돌려주는 naive UTC
        mark_running(session, fresh, now - timedelta(minutes=5), worker="live-worker")
    publisher = ScriptedPublisher()

    handled, outcome = run_once(engine, tmp_path, publisher)

    assert (handled, outcome) == (True, "SUCCEEDED")
    assert [draft.title for draft in publisher.calls] == [pending.title]  # 만료된 잡은 다시 발행되지 않는다
    for ids in (stale_aware, stale_naive):
        state = snapshot(engine, ids)
        assert state.job is JobStatus.FAILED and state.code == "STALE_RUNNING"
        assert "worker=dead-worker" in state.message
        assert state.publish is PublishStatus.PUBLISH_UNVERIFIED
        assert state.version is ArticleStatus.QUARANTINED and state.article is ArticleStatus.QUARANTINED
        assert state.runs == [(JobStatus.FAILED, True, "STALE_RUNNING")]  # 열려 있던 JobRun도 닫혔다
        assert state.attempts == 1  # 재실행되지 않았다
    live = snapshot(engine, fresh)
    assert live.job is JobStatus.RUNNING and live.locked_by == "live-worker" and live.code is None
    assert live.publish is PublishStatus.PUBLISHING and live.version is ArticleStatus.PUBLISHING
    assert live.runs == [(JobStatus.RUNNING, False, None)]
    assert snapshot(engine, pending).job is JobStatus.SUCCEEDED


def test_stale_jobs_never_return_to_the_queue(engine, tmp_path: Path) -> None:
    """이미 클릭됐을 수 있는 잡을 PENDING/RETRY_WAIT로 되돌리면 같은 글이 또 올라간다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        mark_running(session, ids, utc_now() - timedelta(days=7))
    publisher = ScriptedPublisher()

    results = [run_once(engine, tmp_path, publisher) for _ in range(3)]

    assert results == [(False, "NONE")] * 3
    assert publisher.calls == []
    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.job not in (JobStatus.PENDING, JobStatus.RETRY_WAIT)


def test_stale_recovery_keeps_a_known_post_url_in_the_message(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        mark_running(session, ids, utc_now() - timedelta(hours=3))
        session.get(PublishJob, ids.publish).result_url = POST_URL
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert state.result_url == POST_URL and POST_URL in state.message


def test_lock_ttl_comes_from_settings(engine, tmp_path: Path) -> None:  # noqa: ANN001
    now = utc_now()
    with Session(engine) as session:
        thirty = add_job(session, tmp_path, 1)
        ninety = add_job(session, tmp_path, 2)
        mark_running(session, thirty, now - timedelta(minutes=30))
        mark_running(session, ninety, now - timedelta(minutes=90))

    run_once(engine, tmp_path, ScriptedPublisher(), publish_lock_ttl_seconds=3600)  # TTL 60분

    assert snapshot(engine, thirty).job is JobStatus.RUNNING
    assert snapshot(engine, ninety).code == "STALE_RUNNING"


def test_naive_utc_timestamps_are_not_misread_as_local_time(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """naive 값을 로컬 시간으로 읽으면 UTC보다 앞선 시간대에서 방금 잡은 락이 만료로 오판된다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        mark_running(session, ids, utc_now().replace(tzinfo=None) - timedelta(seconds=30), worker="live-worker")

    run_once(engine, tmp_path, ScriptedPublisher())

    assert snapshot(engine, ids).job is JobStatus.RUNNING


def test_a_job_claimed_by_another_process_is_not_misjudged_from_a_cached_row(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """세션 캐시에 PENDING으로 남은 행을 그대로 믿으면(locked_at 없음) 다른 프로세스가 방금 잡은 잡을 만료로 오판한다."""
    factory = sessionmaker(engine, expire_on_commit=False)  # 운영(create_session_factory)과 같은 설정
    with factory() as setup:
        ids = add_job(setup, tmp_path)
    with factory() as mine, factory() as other:
        # 강한 참조를 유지해야 한다(세션의 identity map은 약한 참조라서, 참조가 없으면 캐시된 행이 사라져 검증이 무의미해진다).
        cached = mine.get(Job, ids.job)
        assert cached.status is JobStatus.PENDING and cached.locked_at is None  # 내 세션 캐시에는 PENDING으로 남아 있다
        claimed = JobQueue(other).claim_next("other-worker", utc_now())  # 다른 프로세스가 방금 클레임
        assert claimed is not None

        handled = PublisherWorker(mine, make_settings(tmp_path), ScriptedPublisher()).run_once("worker-test")

        assert cached.status is JobStatus.RUNNING  # 만료 검사가 캐시 행을 DB 값으로 갱신했다

    state = snapshot(engine, ids)
    assert handled is False
    assert state.job is JobStatus.RUNNING and state.locked_by == "other-worker" and state.code is None


def test_find_stale_running_handles_naive_aware_and_missing_timestamps(engine, tmp_path: Path) -> None:  # noqa: ANN001
    now = utc_now()
    with Session(engine) as session:
        old_aware = add_job(session, tmp_path, 1)
        old_naive = add_job(session, tmp_path, 2)
        recent = add_job(session, tmp_path, 3)
        no_stamp = add_job(session, tmp_path, 4)
        only_started = add_job(session, tmp_path, 5)
        started_recently = add_job(session, tmp_path, 6)
        pending = add_job(session, tmp_path, 7)
        mark_running(session, old_aware, now - timedelta(hours=1))
        mark_running(session, old_naive, (now - timedelta(hours=1)).replace(tzinfo=None))
        mark_running(session, recent, now - timedelta(seconds=10))
        mark_running(session, no_stamp, now)
        mark_running(session, only_started, now)
        mark_running(session, started_recently, now)
        job = session.get(Job, no_stamp.job)
        job.locked_at = None
        job.started_at = None  # 시각이 하나도 없는 RUNNING은 만료로 본다
        job = session.get(Job, only_started.job)
        job.locked_at = None
        job.started_at = now - timedelta(hours=1)  # locked_at이 없으면 started_at으로 판단(오래됨 → 만료)
        job = session.get(Job, started_recently.job)
        job.locked_at = None
        job.started_at = now - timedelta(seconds=10)  # started_at으로 판단(최근 → 만료 아님)
        session.commit()

        stale = JobQueue(session).find_stale_running(now - timedelta(minutes=30))

        stale_ids = {job.id for job in stale}
        assert stale_ids == {old_aware.job, old_naive.job, no_stamp.job, only_started.job}
        assert pending.job not in stale_ids and recent.job not in stale_ids and started_recently.job not in stale_ids


def test_as_utc_normalizes_naive_and_aware_values() -> None:
    naive = datetime(2026, 10, 1, 6, 0, 0)
    kst = datetime(2026, 10, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=9)))

    assert as_utc(naive) == datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    assert as_utc(kst) == datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    assert as_utc(None) is None


# =================================================================================================================
# P4: KeyboardInterrupt 등 BaseException — 일관된 상태를 남기고 다시 던진다
# =================================================================================================================
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupt_during_the_browser_step_leaves_a_consistent_state_and_reraises(engine, tmp_path: Path, interrupt: type[BaseException]) -> None:  # noqa: ANN001
    """`except Exception`은 KeyboardInterrupt를 못 잡아 잡이 RUNNING/PUBLISHING에 영구히 남았다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    with Session(engine) as session:
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher(publish_error=interrupt()))
        with pytest.raises(interrupt):
            worker.run_once("worker-test")
        assert worker.last_outcome == "FAILED"

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.code == "INTERRUPTED"
    assert interrupt.__name__ in state.message
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED  # 글이 이미 올라갔을 수 있다
    assert state.version is ArticleStatus.QUARANTINED and state.article is ArticleStatus.QUARANTINED
    assert state.runs == [(JobStatus.FAILED, True, "INTERRUPTED")]


def test_interrupt_during_verification_keeps_the_committed_url(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    with Session(engine) as session:
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher(verify_error=KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            worker.run_once("worker-test")

    state = snapshot(engine, ids)
    assert state.code == "INTERRUPTED" and state.result_url == POST_URL and POST_URL in state.message
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED


def test_interrupt_before_the_browser_step_does_not_quarantine(engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """브라우저 단계(PUBLISHING 커밋)에 들어가기 전의 중단은 글이 있을 수 없으므로 격리하지 않는다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    def interrupted_preflight(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(worker_module, "validate_preflight", interrupted_preflight)
    with Session(engine) as session:
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher())
        with pytest.raises(KeyboardInterrupt):
            worker.run_once("worker-test")

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.code == "INTERRUPTED"
    assert state.publish is PublishStatus.FAILED
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH
    # 클레임 직후 커밋해 둔 JobRun이 롤백에 휩쓸리지 않고 닫힌 채 남아 있어야 한다
    assert state.runs == [(JobStatus.FAILED, True, "INTERRUPTED")]


# =================================================================================================================
# P6: 킬 스위치는 잡을 클레임하기 전에 본다
# =================================================================================================================
@pytest.mark.parametrize("switch", ["auto_publish_enabled", "tistory_production_enabled"])
def test_disabled_publishing_leaves_every_job_untouched_and_resumes_when_reenabled(engine, tmp_path: Path, switch: str) -> None:  # noqa: ANN001
    """클레임 뒤에 스위치를 검사하면 실행할 때마다 잡 1개가 영구 FAILED/BLOCKED가 되어 큐가 소진됐다."""
    with Session(engine) as session:
        first = add_job(session, tmp_path, 1)
        second = add_job(session, tmp_path, 2)
    publisher = ScriptedPublisher()

    for _ in range(3):
        handled, outcome = run_once(engine, tmp_path, publisher, **{switch: False})
        assert (handled, outcome) == (False, "DISABLED")

    for ids in (first, second):
        state = snapshot(engine, ids)
        assert state.job is JobStatus.PENDING and state.attempts == 0 and state.locked_by is None
        assert state.publish is PublishStatus.PENDING and state.runs == []
    assert publisher.calls == []

    handled, outcome = run_once(engine, tmp_path, publisher)  # 다시 켜면 큐가 그대로 재개된다
    assert (handled, outcome) == (True, "SUCCEEDED")
    assert snapshot(engine, first).job is JobStatus.SUCCEEDED


def test_disabled_publishing_does_not_even_recover_stale_jobs(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """'잡을 건드리지 않고 False'가 계약이다. 만료 복구도 켜져 있을 때만 한다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        mark_running(session, ids, utc_now() - timedelta(days=1))

    handled, outcome = run_once(engine, tmp_path, ScriptedPublisher(), auto_publish_enabled=False)

    assert (handled, outcome) == (False, "DISABLED")
    assert snapshot(engine, ids).job is JobStatus.RUNNING


# =================================================================================================================
# last_outcome (웹 계층과의 계약)
# =================================================================================================================
def test_last_outcome_values(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        add_job(session, tmp_path, 1)
        add_job(session, tmp_path, 2)
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher())
        assert worker.last_outcome is None  # 실행 전

        assert worker.run_once("w") is True and worker.last_outcome == "SUCCEEDED"
        worker.publisher = ScriptedPublisher(publish_error=PublisherFailure("UI_BROKEN", "x"))
        assert worker.run_once("w") is True and worker.last_outcome == "FAILED"
        assert worker.run_once("w") is False and worker.last_outcome == "NONE"  # 이전 값이 남지 않는다

        worker.settings = make_settings(tmp_path, tistory_production_enabled=False)
        assert worker.run_once("w") is False and worker.last_outcome == "DISABLED"


def test_last_outcome_is_cleared_when_run_once_raises(engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """예외로 끝난 실행이 이전 실행의 결과('SUCCEEDED')를 그대로 보여 주면 웹 계층이 잘못된 메시지를 만든다."""
    with Session(engine) as session:
        add_job(session, tmp_path, 1)
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher())
        assert worker.run_once("w") is True and worker.last_outcome == "SUCCEEDED"

        def broken_claim(self: JobQueue, worker_id: str, now: datetime) -> None:
            raise RuntimeError("database is down")

        monkeypatch.setattr(JobQueue, "claim_next", broken_claim)
        with pytest.raises(RuntimeError):
            worker.run_once("w")

        assert worker.last_outcome is None


# =================================================================================================================
# P6: 썸네일 경로 해석
# =================================================================================================================
def test_relative_thumbnail_is_resolved_against_the_project_root_from_another_directory(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """작업 디렉터리가 달라 상대 경로를 못 찾으면 프리플라이트가 잡을 영구 실패시켰다."""
    project = tmp_path / "project"
    (project / "storage").mkdir(parents=True)
    (project / "storage" / "lifestyle.png").write_bytes(b"png")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(worker_module, "PROJECT_ROOT", project)
    monkeypatch.chdir(elsewhere)
    with Session(engine) as session:
        ids = add_job(session, tmp_path, thumbnail="storage/lifestyle.png")
    publisher = ScriptedPublisher()

    handled, outcome = run_once(engine, tmp_path, publisher)

    assert (handled, outcome) == (True, "SUCCEEDED")
    assert publisher.calls[0].thumbnail_path == project / "storage" / "lifestyle.png"
    assert publisher.calls[0].thumbnail_path.is_absolute()
    assert snapshot(engine, ids).job is JobStatus.SUCCEEDED


def test_relative_thumbnail_prefers_the_working_directory_when_the_file_exists_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "storage").mkdir(parents=True)
    (project / "storage" / "a.png").write_bytes(b"project")
    cwd = tmp_path / "cwd"
    (cwd / "storage").mkdir(parents=True)
    (cwd / "storage" / "a.png").write_bytes(b"cwd")
    monkeypatch.setattr(worker_module, "PROJECT_ROOT", project)
    monkeypatch.chdir(cwd)

    assert resolve_thumbnail_path("storage/a.png") == (cwd / "storage" / "a.png").resolve()


def test_absolute_and_missing_thumbnails_are_returned_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "PROJECT_ROOT", tmp_path / "project")
    absolute = tmp_path / "x.png"

    assert resolve_thumbnail_path(str(absolute)) == absolute  # 없어도 그대로(프리플라이트가 판단)
    assert resolve_thumbnail_path("storage/missing.png") == Path("storage/missing.png")


def test_missing_thumbnail_blocks_with_preflight_and_keeps_the_article_ready(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path, thumbnail=str(tmp_path / "does-not-exist.png"))
    publisher = ScriptedPublisher()

    run_once(engine, tmp_path, publisher)

    state = snapshot(engine, ids)
    assert publisher.calls == [] and state.code == "PREFLIGHT_FAILED"
    assert state.publish is PublishStatus.BLOCKED and state.article is ArticleStatus.READY_TO_PUBLISH


# =================================================================================================================
# 2차 독립 리뷰 반영: 글이 만들어진 뒤의 실패는 항상 PUBLISH_UNVERIFIED + 주소 보존, 중복 발행 방지
# =================================================================================================================
def test_an_unexpected_error_during_verification_is_unverified_not_a_retryable_failure(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """E8: 검증 단계에서 브라우저가 안 떠 날 오류가 나면 UNEXPECTED_ERROR/FAILED로 기록돼, 이미 만들어진 글이 있는데도 재시도가 경고 없이 허용됐다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(verify_error=RuntimeError("chromium launch failed")))

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.code == "PUBLISH_UNVERIFIED"
    assert "RuntimeError" not in (state.code or "") and "chromium launch failed" in state.message
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.result_url == POST_URL
    assert POST_URL in state.message
    assert state.version is ArticleStatus.QUARANTINED and state.article is ArticleStatus.QUARANTINED


@pytest.mark.parametrize("code", ["UI_BROKEN", "AUTH_REQUIRED", "UNEXPECTED_ERROR", "SOMETHING_NEW"])
def test_any_failure_after_the_post_exists_is_reported_as_unverified_with_the_original_code_in_the_message(
    engine, tmp_path: Path, code: str  # noqa: ANN001
) -> None:
    """글 주소를 이미 아는 상태에서 검증기가 UI_BROKEN 같은 코드를 던져도 '글 없음'으로 읽히면 안 된다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)

    run_once(engine, tmp_path, ScriptedPublisher(verify_error=PublisherFailure(code, "검증기 오류")))

    state = snapshot(engine, ids)
    assert state.code == "PUBLISH_UNVERIFIED" and f"[{code}] 검증기 오류" in state.message
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.result_url == POST_URL


def test_a_verification_failure_keeps_the_post_url_the_failure_itself_carries(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    other = "https://example.tistory.com/entry/other"

    run_once(engine, tmp_path, ScriptedPublisher(verify_error=PublisherFailure("PUBLISH_UNVERIFIED", "x", post_url=other)))

    assert other in snapshot(engine, ids).message


def test_the_post_url_survives_a_failed_database_write_of_result_url(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """W4: result_url을 커밋하다 DB 오류가 나면 롤백과 함께 주소가 사라졌다. 메모리·로그에서 되살려 실패 기록에 남긴다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    armed = {"on": False}

    with Session(engine) as session:
        real_commit = session.commit

        def commit() -> None:
            if armed["on"]:
                armed["on"] = False  # 한 번만 실패시킨다(실패 기록 자체는 저장돼야 한다)
                raise OperationalError("UPDATE publish_jobs", {}, Exception("disk I/O error"))
            real_commit()

        session.commit = commit  # type: ignore[method-assign]
        publisher = ScriptedPublisher(on_publish=lambda draft: armed.update(on=True))  # publish()가 끝난 직후의 커밋이 실패한다
        worker = PublisherWorker(session, make_settings(tmp_path), publisher)
        assert worker.run_once("worker-test") is True

    state = snapshot(engine, ids)
    assert state.code == "PUBLISH_UNVERIFIED" and "OperationalError" in state.message
    assert state.result_url == POST_URL and POST_URL in state.message
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.article is ArticleStatus.QUARANTINED


def test_an_interrupt_right_after_the_post_exists_still_records_its_url(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """글이 만들어진 직후(result_url 커밋 도중) 중단되면 DB에는 주소가 없다. 메모리에 둔 주소를 INTERRUPTED 기록에 옮겨 적는다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    armed = {"on": False}

    with Session(engine) as session:
        real_commit = session.commit

        def commit() -> None:
            if armed["on"]:
                armed["on"] = False
                raise KeyboardInterrupt()
            real_commit()

        session.commit = commit  # type: ignore[method-assign]
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher(on_publish=lambda draft: armed.update(on=True)))
        with pytest.raises(KeyboardInterrupt):
            worker.run_once("worker-test")

    state = snapshot(engine, ids)
    assert state.code == "INTERRUPTED" and state.result_url == POST_URL and POST_URL in state.message
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.article is ArticleStatus.QUARANTINED


def test_a_later_failure_on_the_same_worker_does_not_inherit_the_previous_jobs_post_url(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """워커 객체를 계속 쓰는 프로세스에서, 앞 잡이 알게 된 글 주소가 다음 잡의 실패 기록(글 없음)에 묻어 가면 안 된다."""
    with Session(engine) as session:
        add_job(session, tmp_path, 1)
        second = add_job(session, tmp_path, 2)
        worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher())
        assert worker.run_once("w") is True and worker.last_outcome == "SUCCEEDED"  # 1번 잡이 POST_URL 글을 만든다
        worker.publisher = ScriptedPublisher(publish_error=PublisherFailure("UI_BROKEN", "비공개 옵션 없음"))

        assert worker.run_once("w") is True and worker.last_outcome == "FAILED"  # 2번 잡은 글을 만들지 못했다

    state = snapshot(engine, second)
    assert state.publish is PublishStatus.UI_BROKEN and state.result_url is None
    assert POST_URL not in state.message and state.article is ArticleStatus.QUARANTINED


def test_publish_attaches_the_post_url_to_failures_that_carry_none(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """_publish의 계약: 글이 만들어진 뒤의 어떤 실패든 post_url을 실어 PUBLISH_UNVERIFIED로 올린다(run_once 바깥에서 불러도)."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        job = session.get(Job, ids.job)
        for error in (PublisherFailure("UI_BROKEN", "x"), PublisherFailure("PUBLISH_UNVERIFIED", "y"), RuntimeError("z")):
            worker = PublisherWorker(session, make_settings(tmp_path), ScriptedPublisher(verify_error=error))
            session.get(PublishJob, ids.publish).status = PublishStatus.PENDING
            session.get(PublishJob, ids.publish).result_url = None

            with pytest.raises(PublisherFailure) as raised:
                worker._publish(job)

            assert raised.value.code == "PUBLISH_UNVERIFIED" and raised.value.post_url == POST_URL


def test_the_duplicate_guard_failure_carries_the_known_post_url() -> None:
    publish_job = PublishJob(status=PublishStatus.PUBLISH_UNVERIFIED, result_url=POST_URL)

    with pytest.raises(PublisherFailure) as raised:
        PublisherWorker._refuse_if_possibly_published(publish_job)

    assert raised.value.code == "DUPLICATE_GUARD" and raised.value.post_url == POST_URL and not raised.value.retryable
    PublisherWorker._refuse_if_possibly_published(PublishJob(status=PublishStatus.PENDING, result_url=None))  # 글이 없으면 통과


def test_the_post_url_is_logged_as_soon_as_it_is_known(engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """DB가 완전히 죽어 있어도 글 주소가 로그에는 남는다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(worker_module, "log_event", lambda logger, event, **fields: events.append((event, fields)))

    run_once(engine, tmp_path, ScriptedPublisher())

    assert events == [("post_created", {"job_id": ids.job, "publish_job_id": ids.publish, "url": POST_URL})]


def test_a_retryable_failure_that_carries_a_post_url_is_never_retried(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """W6: 주소를 아는(= 글이 있는) 실패를 retryable로 표시해도 다시 실행하면 같은 글이 또 올라간다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    failure = PublisherFailure("UI_BROKEN", "x", retryable=True, post_url="https://example.tistory.com/555")

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=failure))

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.job is not JobStatus.RETRY_WAIT
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.result_url == "https://example.tistory.com/555"
    assert state.article is ArticleStatus.QUARANTINED


@pytest.mark.parametrize(
    "setup",
    [
        {"result_url": "https://example.tistory.com/101"},
        {"status": PublishStatus.PUBLISH_UNVERIFIED},
        {"status": PublishStatus.PUBLISHING},
        {"status": PublishStatus.VERIFIED},
    ],
    ids=["has-result-url", "unverified", "publishing", "verified"],
)
def test_the_worker_refuses_to_publish_again_when_a_post_may_already_exist(engine, tmp_path: Path, setup: dict[str, Any]) -> None:  # noqa: ANN001
    """W5: 큐를 직접 되돌린 잡이 이전에 올라간(공개일 수도 있는) 글을 기록에서 지우고 같은 글을 또 올렸다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        publish = session.get(PublishJob, ids.publish)
        if "status" in setup:
            publish.status = setup["status"]
        if "result_url" in setup:
            publish.result_url = setup["result_url"]
        session.commit()
        before = (publish.status, publish.result_url)
    publisher = ScriptedPublisher()

    handled, outcome = run_once(engine, tmp_path, publisher)

    state = snapshot(engine, ids)
    assert (handled, outcome) == (True, "FAILED") and publisher.calls == []
    assert state.job is JobStatus.FAILED and state.code == "DUPLICATE_GUARD"
    assert "다시 발행하지 않습니다" in state.message and "force" in state.message
    assert (state.publish, state.result_url) == before  # 있던 사실(상태·글 주소)을 지우거나 덮어쓰지 않는다
    assert state.article is ArticleStatus.READY_TO_PUBLISH  # 거절은 글의 상태를 바꾸지 않는다
    assert state.attempts == 1 and state.runs == [(JobStatus.FAILED, True, "DUPLICATE_GUARD")]


def test_the_duplicate_guard_message_names_the_known_post_url(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        session.get(PublishJob, ids.publish).result_url = POST_URL
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    assert POST_URL in snapshot(engine, ids).message


def test_a_verified_publish_job_stays_verified_when_a_requeued_job_is_refused(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """거절을 PUBLISH_UNVERIFIED로 기록하면 이미 확인된(VERIFIED) 글이 격리 상태로 강등된다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        session.get(PublishJob, ids.publish).status = PublishStatus.VERIFIED
        session.get(PublishJob, ids.publish).result_url = POST_URL
        session.get(ArticleVersion, ids.version).status = ArticleStatus.VERIFIED
        session.get(Article, ids.article).status = ArticleStatus.VERIFIED
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert state.publish is PublishStatus.VERIFIED and state.version is ArticleStatus.VERIFIED
    assert state.article is ArticleStatus.VERIFIED


@pytest.mark.parametrize("status", [PublishStatus.PENDING, PublishStatus.FAILED, PublishStatus.UI_BROKEN, PublishStatus.AUTH_REQUIRED, PublishStatus.BLOCKED])
def test_publish_jobs_without_a_post_are_published_normally(engine, tmp_path: Path, status: PublishStatus) -> None:  # noqa: ANN001
    """글이 없다고 알려진 상태(대시보드 재등록 포함)의 발행 건은 막지 않는다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        session.get(PublishJob, ids.publish).status = status
        session.commit()
    publisher = ScriptedPublisher()

    handled, outcome = run_once(engine, tmp_path, publisher)

    assert (handled, outcome) == (True, "SUCCEEDED") and len(publisher.calls) == 1


# --- 브라우저 시작 실패: 글이 없고, 격리하지 않는다 -----------------------------------------------------------------
def test_browser_launch_failure_retries_without_quarantining(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """E9/finding 8: 같은 프로필을 쓰는 다른 브라우저 때문에 launch가 실패하면 글이 없다. 재시도하되 글을 격리하지 않는다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
    failure = PublisherFailure("BROWSER_LAUNCH_FAILED", "프로필이 이미 열려 있습니다", retryable=True)

    run_once(engine, tmp_path, ScriptedPublisher(publish_error=failure))

    state = snapshot(engine, ids)
    assert state.job is JobStatus.RETRY_WAIT and state.code == "BROWSER_LAUNCH_FAILED"
    assert state.publish is PublishStatus.PENDING
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH


def test_browser_launch_failure_after_the_last_attempt_leaves_the_article_ready_not_quarantined(engine, tmp_path: Path) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)  # max_attempts=2
    publisher = ScriptedPublisher(publish_error=PublisherFailure("BROWSER_LAUNCH_FAILED", "x", retryable=True))
    run_once(engine, tmp_path, publisher)
    with Session(engine) as session:
        session.get(Job, ids.job).run_after = utc_now() - timedelta(seconds=1)
        session.commit()

    run_once(engine, tmp_path, publisher)

    state = snapshot(engine, ids)
    assert state.attempts == 2 and state.job is JobStatus.FAILED
    assert state.publish is PublishStatus.FAILED
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH  # 대시보드에서 force 없이 재등록 가능


# --- 재시도 상한·클레임 경쟁(리뷰어가 제안한 생존 변이 킬 테스트) --------------------------------------------------------
def test_claim_does_not_double_claim_a_job_another_worker_took_in_between(engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """M45: 클레임 UPDATE의 상태 조건이 빠지면, 후보를 고른 직후 다른 워커가 잡은 잡을 다시 잡아 두 워커가 같은 글을 올린다."""
    with Session(engine) as setup:
        ids = add_job(setup, tmp_path)
    first, second = Session(engine), Session(engine)
    try:
        assert JobQueue(second).claim_next("other", utc_now()) is not None  # 다른 워커가 먼저 잡는다
        real_scalar, calls = first.scalar, {"n": 0}

        def stale_scalar(statement: Any, *args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1  # 첫 SELECT는 다른 워커의 UPDATE 직전에 실행된 것처럼 옛 후보를 돌려준다
            return ids.job if calls["n"] == 1 else real_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(first, "scalar", stale_scalar)

        assert JobQueue(first).claim_next("me", utc_now()) is None
    finally:
        first.close()
        second.close()
    state = snapshot(engine, ids)
    assert state.locked_by == "other" and state.attempts == 1


def test_attempts_are_counted_and_the_retry_cap_is_exact(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """M46/M64: attempt_count가 올라가지 않거나 상한 비교가 하나 어긋나면 재시도가 끝나지 않거나 한 번 덜 시도한다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)  # max_attempts=2
    publisher = ScriptedPublisher(publish_error=PublisherFailure("UI_BROKEN", "x", retryable=True))

    run_once(engine, tmp_path, publisher)  # 1번째 시도 → 재시도 대기
    first = snapshot(engine, ids)
    with Session(engine) as session:
        session.get(Job, ids.job).run_after = utc_now() - timedelta(seconds=1)
        session.commit()
    run_once(engine, tmp_path, publisher)  # 2번째 시도 → 상한 도달

    last = snapshot(engine, ids)
    assert (first.attempts, first.job) == (1, JobStatus.RETRY_WAIT)
    assert (last.attempts, last.job) == (2, JobStatus.FAILED)
    assert len(publisher.calls) == 2


def test_stale_recovery_does_not_touch_the_publish_job_that_shares_an_id_with_another_job_type(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """M56: 발행 잡이 아닌 잡의 entity_id가 우연히 PublishJob id와 같아도 그 발행 건·글을 만료 복구가 건드리면 안 된다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path, job_type="SOMETHING_ELSE")
        mark_running(session, ids, utc_now() - timedelta(days=1))
        session.get(PublishJob, ids.publish).status = PublishStatus.PENDING  # 같은 id를 우연히 공유하는 무관한 행
        session.get(ArticleVersion, ids.version).status = ArticleStatus.READY_TO_PUBLISH
        session.get(Article, ids.article).status = ArticleStatus.READY_TO_PUBLISH
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.code == "STALE_RUNNING"
    assert state.publish is PublishStatus.PENDING and state.article is ArticleStatus.READY_TO_PUBLISH


# --- 발행 단계 시작 전에 죽은 워커 -------------------------------------------------------------------------------------
def test_a_worker_that_died_before_the_publishing_commit_does_not_quarantine_the_article(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """finding 9: 클레임 직후(PUBLISHING 커밋 전) 죽은 잡은 브라우저를 열지 않았다. 글이 없는데 격리하면 force 재등록을 강요한다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        job = session.get(Job, ids.job)
        job.status = JobStatus.RUNNING
        job.locked_by, job.attempt_count = "dead-worker", 1
        job.locked_at = job.started_at = utc_now() - timedelta(hours=2)
        session.add(JobRun(job_id=job.id, worker_id="dead-worker", status=JobStatus.RUNNING, started_at=job.locked_at))
        session.commit()  # PublishJob은 PENDING 그대로: 워커가 PUBLISHING을 커밋하기 전에 죽었다
    publisher = ScriptedPublisher()

    run_once(engine, tmp_path, publisher)

    state = snapshot(engine, ids)
    assert publisher.calls == []  # 이 잡은 되살리지 않는다(자동 재실행 금지)
    assert state.job is JobStatus.FAILED and state.code == "STALE_RUNNING"
    assert "브라우저를 열기 전" in state.message and "글은 만들어지지 않았습니다" in state.message
    assert state.publish is PublishStatus.PENDING
    assert state.version is ArticleStatus.READY_TO_PUBLISH and state.article is ArticleStatus.READY_TO_PUBLISH
    assert state.runs == [(JobStatus.FAILED, True, "STALE_RUNNING")]  # 열려 있던 JobRun은 닫았다


def test_a_stale_job_without_a_run_record_is_also_recoverable(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """클레임 커밋 직후, JobRun을 만들기도 전에 죽은 경우."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        job = session.get(Job, ids.job)
        job.status, job.locked_by, job.attempt_count = JobStatus.RUNNING, "dead-worker", 1
        job.locked_at = job.started_at = utc_now() - timedelta(hours=2)
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert state.job is JobStatus.FAILED and state.code == "STALE_RUNNING" and state.runs == []
    assert state.publish is PublishStatus.PENDING and state.article is ArticleStatus.READY_TO_PUBLISH


def test_a_stale_job_that_already_has_a_post_url_is_still_quarantined_even_if_the_status_says_pending(engine, tmp_path: Path) -> None:  # noqa: ANN001
    """상태와 글 주소가 어긋난 행(수동 편집 등)은 안전 쪽으로: 글 주소가 있으면 시작 전에 죽은 것으로 보지 않는다."""
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        job = session.get(Job, ids.job)
        job.status, job.locked_by, job.attempt_count = JobStatus.RUNNING, "dead-worker", 1
        job.locked_at = job.started_at = utc_now() - timedelta(hours=2)
        session.get(PublishJob, ids.publish).result_url = POST_URL
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.article is ArticleStatus.QUARANTINED
    assert POST_URL in state.message and "자동으로 다시 실행하지 않습니다" in state.message


@pytest.mark.parametrize("status", [PublishStatus.PUBLISHING, PublishStatus.PUBLISH_UNVERIFIED, PublishStatus.UI_BROKEN, PublishStatus.FAILED])
def test_a_stale_job_past_the_publishing_commit_is_always_quarantined(engine, tmp_path: Path, status: PublishStatus) -> None:  # noqa: ANN001
    with Session(engine) as session:
        ids = add_job(session, tmp_path)
        mark_running(session, ids, utc_now() - timedelta(hours=2))
        session.get(PublishJob, ids.publish).status = status
        session.commit()

    run_once(engine, tmp_path, ScriptedPublisher())

    state = snapshot(engine, ids)
    assert state.publish is PublishStatus.PUBLISH_UNVERIFIED and state.article is ArticleStatus.QUARANTINED
    assert "브라우저를 열기 전" not in state.message


# --- 락 임대 하한 -------------------------------------------------------------------------------------------------
def test_the_lock_ttl_has_a_floor_that_outlasts_a_normal_publish() -> None:
    """finding 8: 하트비트가 없으므로 60초 같은 짧은 임대는 정상 발행 도중 만료돼 두 번째 워커가 같은 글을 또 올린다."""
    with pytest.raises(ValidationError):
        make_settings(Path("."), publish_lock_ttl_seconds=599)

    assert make_settings(Path("."), publish_lock_ttl_seconds=600).publish_lock_ttl_seconds == 600
    assert make_settings(Path(".")).publish_lock_ttl_seconds == 1800


# --- 시간대에 의존하지 않는 naive 타임스탬프 검사 ---------------------------------------------------------------------
def test_naive_timestamps_are_never_converted_through_the_local_timezone() -> None:
    """M51: naive 값을 astimezone(로컬 시간대 해석)으로 바꾸면 UTC 이외 시간대에서만 틀려 한국 시간대 PC에서만 잡히던 버그. 시간대와 무관하게 막는다."""

    class LocalTimeTrap(datetime):
        def astimezone(self, tz=None):  # noqa: ANN001, ANN201
            raise AssertionError("naive datetime must be tagged as UTC, not interpreted as local time")

    naive = LocalTimeTrap(2026, 10, 1, 6, 0, 0)

    assert as_utc(naive) == datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
