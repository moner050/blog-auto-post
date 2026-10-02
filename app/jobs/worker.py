from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.core.settings import Settings
from app.db.models import ArticleStatus, Job, JobRun, JobStatus, PublishJob, PublishStatus, utc_now
from app.jobs.queue import JobQueue
from app.publishing.client import PublisherClient, PublisherFailure
from app.publishing.guards import PublicationDraft, PublicationPreflightError, validate_preflight

# 저장소 루트(app/jobs/worker.py → parents[2]). 상대 경로로 저장된 썸네일을 작업 디렉터리와 무관하게 찾는 데 쓴다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("tistory_automation")

# 실패 후 Article/ArticleVersion을 QUARANTINED(사람이 확인할 때까지 자동 처리 금지)로 두는 코드
_QUARANTINE_CODES = frozenset(
    {"PUBLISH_UNVERIFIED", "UI_BROKEN", "AUTH_REQUIRED", "UNEXPECTED_ERROR", "INTERRUPTED", "STALE_RUNNING"}
)
# 글이 만들어졌을 리 없는 실패 코드: 브라우저를 열기 전에 막혔거나, 어댑터가 최종 클릭 전이라고 보증한 것.
# 브라우저 단계(PUBLISHING)에서 이 밖의 이유로 끝난 잡은 글이 이미 있을 수 있다고 보고 PUBLISH_UNVERIFIED로 멈춘다.
_NO_POST_CODES = frozenset({"PREFLIGHT_FAILED", "BROWSER_LAUNCH_FAILED", "UI_BROKEN", "AUTH_REQUIRED"})
# 거절만 하고 아무것도 시도하지 않은 실패: 발행 건·글의 상태(이미 사실을 담고 있다)를 바꾸지 않는다.
_DUPLICATE_GUARD = "DUPLICATE_GUARD"
# 이 상태의 발행 건은 글이 이미 있을 수 있어 워커가 다시 발행하지 않는다(대시보드 force 재등록은 PENDING으로 되돌려 둔다).
_ALREADY_PUBLISHED_STATUSES = (PublishStatus.PUBLISHING, PublishStatus.PUBLISH_UNVERIFIED, PublishStatus.VERIFIED)


def resolve_thumbnail_path(stored_path: str) -> Path:
    """DB에 저장된 썸네일 경로를 실제 파일 경로로 푼다.

    절대 경로는 그대로 쓴다. 상대 경로는 작업 디렉터리 기준으로 파일이 있으면 그걸(절대 경로로) 쓰고,
    없으면 저장소 루트 기준으로 찾는다(다른 디렉터리에서 워커를 띄워도 잡이 소진되지 않게).
    어디에도 없으면 원래 경로를 그대로 돌려줘 프리플라이트가 '썸네일 없음'으로 막게 한다.
    """
    path = Path(stored_path)
    if path.is_absolute():
        return path
    if path.exists():
        return path.resolve()
    rooted = PROJECT_ROOT / path
    return rooted if rooted.exists() else path


class PublisherWorker:
    def __init__(self, session: Session, settings: Settings, publisher: PublisherClient):
        self.session = session
        self.settings = settings
        self.publisher = publisher
        # 마지막 run_once의 결과: "SUCCEEDED" | "FAILED" | "DISABLED"(킬 스위치) | "NONE"(처리할 잡 없음). 실행 전에는 None.
        self.last_outcome: str | None = None
        # 이번 실행에서 발행기가 돌려준 글 주소. DB 쓰기가 실패해 롤백돼도 실패 기록에 주소를 남기려고 메모리에도 둔다.
        self._known_post_url: str | None = None

    def run_once(self, worker_id: str) -> bool:
        """대기 잡 1건을 처리한다. 잡을 처리했으면(성공이든 실패든) True, 할 일이 없거나 비활성화면 False."""
        self.last_outcome = None
        self._known_post_url = None
        # 킬 스위치는 잡을 건드리기 전에 본다. 꺼져 있으면 클레임하지 않으므로 다시 켰을 때 큐가 그대로 재개된다.
        if not (self.settings.auto_publish_enabled and self.settings.tistory_production_enabled):
            self.last_outcome = "DISABLED"
            return False
        self._recover_stale_jobs()
        job = JobQueue(self.session).claim_next(worker_id, utc_now())
        if job is None:
            self.last_outcome = "NONE"
            return False
        run = JobRun(job_id=job.id, worker_id=worker_id, status=JobStatus.RUNNING)
        self.session.add(run)
        # 실행 기록도 커밋해 둔다(브라우저 단계 중에 다른 세션에서 보이고, 워커가 죽어도 남는다).
        self.session.commit()
        try:
            self._publish(job)
        except PublicationPreflightError as error:
            self._finish_failure(job, run, "PREFLIGHT_FAILED", str(error), retryable=False)
        except PublisherFailure as error:
            self._finish_failure(
                job, run, error.code, str(error), retryable=error.retryable, post_url=error.post_url
            )
        except Exception as error:
            self._finish_failure(job, run, "UNEXPECTED_ERROR", f"{type(error).__name__}: {error}", retryable=False)
        except BaseException as error:
            # KeyboardInterrupt/SystemExit 등: DB를 일관된 상태로 남기고 그대로 다시 던진다.
            self._record_interruption(job, run, error)
            raise
        else:
            job.status = JobStatus.SUCCEEDED
            job.finished_at = utc_now()
            run.status = JobStatus.SUCCEEDED
            run.finished_at = utc_now()
            self.session.commit()
            self.last_outcome = "SUCCEEDED"
            return True
        self.last_outcome = "FAILED"
        return True

    def _recover_stale_jobs(self) -> list[int]:
        """락 임대(publish_lock_ttl_seconds)가 만료된 RUNNING 잡을 수동 확인 상태로 돌린다.

        워커가 죽었다면 브라우저 단계에서 이미 글이 발행됐을 수 있다. 그래서 절대 PENDING으로 되돌리지 않고
        FAILED/STALE_RUNNING + PublishJob PUBLISH_UNVERIFIED + Article·Version QUARANTINED로 멈춘다.
        예외: 발행 건이 아직 PENDING이고 글 주소도 없으면 워커가 PUBLISHING을 커밋하기 전(= 브라우저를 열기 전)에 죽은 것이라
        글이 있을 수 없다. 이때는 잡만 FAILED로 닫고 글·발행 건은 그대로 둔다(대시보드에서 force 없이 다시 등록할 수 있다).
        """
        now = utc_now()
        cutoff = now - timedelta(seconds=self.settings.publish_lock_ttl_seconds)
        stale = JobQueue(self.session).find_stale_running(cutoff)
        for job in stale:
            publish_job = (
                self.session.get(PublishJob, job.entity_id) if job.job_type == "PUBLISH_TISTORY" else None
            )
            never_started = (
                publish_job is not None and publish_job.status is PublishStatus.PENDING and not publish_job.result_url
            )
            if never_started:
                message = (
                    f"RUNNING 상태로 {self.settings.publish_lock_ttl_seconds}초 넘게 멈춘 작업입니다 "
                    f"(worker={job.locked_by}, locked_at={job.locked_at}). 발행 단계에 들어가기 전(브라우저를 열기 전)에 멈춘 것이라 "
                    "글은 만들어지지 않았습니다. 대시보드에서 다시 등록할 수 있습니다."
                )
            else:
                message = (
                    f"RUNNING 상태로 {self.settings.publish_lock_ttl_seconds}초 넘게 멈춘 작업입니다 "
                    f"(worker={job.locked_by}, locked_at={job.locked_at}). 워커가 비정상 종료됐다면 글이 이미 발행됐을 수 있어 "
                    "자동으로 다시 실행하지 않습니다. Tistory 글 관리에서 같은 제목의 글이 있는지, 공개 상태는 아닌지 확인하세요."
                )
            if publish_job is not None and publish_job.result_url:
                message += f" (post URL: {publish_job.result_url})"
            job.status = JobStatus.FAILED
            job.finished_at = now
            job.last_error_code = "STALE_RUNNING"
            job.last_error_message = message
            open_runs = self.session.scalars(
                select(JobRun).where(JobRun.job_id == job.id, JobRun.finished_at.is_(None))
            ).all()
            for open_run in open_runs:
                open_run.status = JobStatus.FAILED
                open_run.finished_at = now
                open_run.error_code = "STALE_RUNNING"
                open_run.error_message = message
            if publish_job is not None and not never_started:
                publish_job.status = PublishStatus.PUBLISH_UNVERIFIED
                self._set_article_status(publish_job, ArticleStatus.QUARANTINED)
        if stale:
            self.session.commit()
        return [job.id for job in stale]

    def _publish(self, job: Job) -> None:
        if job.job_type != "PUBLISH_TISTORY":
            raise PublisherFailure("UNSUPPORTED_JOB", f"unsupported job type: {job.job_type}")
        publish_job = self.session.get(PublishJob, job.entity_id)
        if publish_job is None:
            raise PublisherFailure("PUBLISH_JOB_NOT_FOUND", "publish job does not exist")
        self._refuse_if_possibly_published(publish_job)
        version = publish_job.article_version
        draft = PublicationDraft(
            title=version.title,
            body_html=version.body_html,
            tags=list(version.tags_json),
            category=publish_job.category,
            target_blog_name=publish_job.target_blog_name,
            visibility=publish_job.visibility,
            thumbnail_path=resolve_thumbnail_path(version.thumbnail_path),
        )
        validate_preflight(self.settings, draft)
        publish_job.status = PublishStatus.PUBLISHING
        self._set_article_status(publish_job, ArticleStatus.PUBLISHING)
        # write-ahead: 브라우저 단계(수 분)에 들어가기 전에 PUBLISHING을 커밋한다. 그래야 워커가 죽어도 흔적이 남고,
        # 열린 쓰기 트랜잭션이 DB 락을 쥔 채 브라우저를 돌리는 일도 없다.
        self.session.commit()
        post = self.publisher.publish(draft)
        # 여기부터는 글이 존재한다. 주소를 메모리와 로그에 먼저 남겨 두면, 아래에서 DB 쓰기나 검증이 어떻게 실패해도 주소를 잃지 않는다.
        self._known_post_url = post.url
        log_event(logger, "post_created", job_id=job.id, publish_job_id=publish_job.id, url=post.url)
        try:
            # 글 주소를 알게 된 즉시 저장·커밋한다. 이후 검증이 실패해도 (공개일 수 있는) 글의 주소가 남는다.
            publish_job.result_url = post.url
            self.session.commit()
            evidence = self.publisher.verify_private(post, draft)
        except PublisherFailure as error:
            # 글이 이미 있으므로 어떤 코드로 실패했든 '확인하지 못함'이다(UI_BROKEN 같은 코드는 글이 없다는 뜻이라 오해를 부른다).
            message = str(error) if error.code == "PUBLISH_UNVERIFIED" else f"[{error.code}] {error}"
            raise PublisherFailure("PUBLISH_UNVERIFIED", message, post_url=error.post_url or post.url) from error
        except Exception as error:
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"글이 만들어진 뒤 확인 단계에서 오류가 발생했습니다: {type(error).__name__}: {error}",
                post_url=post.url,
            ) from error
        details = {"visibility": "PRIVATE", "title": draft.title}
        if isinstance(evidence, str):
            # 어떤 근거로 비공개라고 판단했는지(예: 'HTTP 404')를 남긴다. 수동 드라이런 때 확인하기 위한 값이다.
            details["evidence"] = evidence
        publish_job.status = PublishStatus.VERIFIED
        publish_job.verification_details = details
        self._set_article_status(publish_job, ArticleStatus.VERIFIED)

    @staticmethod
    def _refuse_if_possibly_published(publish_job: PublishJob) -> None:
        """이미 글이 있을 수 있는 발행 건(글 주소가 남아 있거나 PUBLISHING·PUBLISH_UNVERIFIED·VERIFIED)은 다시 발행하지 않는다.

        대시보드의 force 재등록은 이전 글 주소를 이력으로 옮기고 상태를 PENDING으로 되돌려 두므로 이 검사를 통과한다.
        큐를 직접 되돌리는 등 다른 경로로 다시 들어온 잡이 같은 글을 또 올리지 못하게 하는 마지막 방어선이다.
        """
        if publish_job.result_url or publish_job.status in _ALREADY_PUBLISHED_STATUSES:
            raise PublisherFailure(
                _DUPLICATE_GUARD,
                f"이 발행 건은 이미 글이 만들어졌을 수 있어(상태: {publish_job.status.value}) 다시 발행하지 않습니다. "
                "Tistory 글 관리에서 확인한 뒤, 중복을 감수하고 다시 올리려면 대시보드에서 강제 재등록(force)을 사용하세요.",
                post_url=publish_job.result_url,
            )

    def _finish_failure(
        self,
        job: Job,
        run: JobRun,
        code: str,
        message: str,
        retryable: bool,
        post_url: str | None = None,
    ) -> None:
        # 실패 직전에 DB 오류 등으로 트랜잭션이 깨졌더라도 마지막으로 커밋된 상태에서 다시 시작한다.
        self.session.rollback()
        publish_job = self.session.get(PublishJob, job.entity_id) if job.job_type == "PUBLISH_TISTORY" else None
        was_publishing = publish_job is not None and publish_job.status is PublishStatus.PUBLISHING
        known_url = post_url or self._known_post_url
        url = known_url or (publish_job.result_url if publish_job is not None else None)
        # 글이 이미 있을 수 있는가: 글 주소를 알고 있거나, 브라우저 단계(PUBLISHING)에서 '글이 없다'고 보증되지 않은 이유로 끝났다.
        post_may_exist = bool(url) or (was_publishing and code not in _NO_POST_CODES)
        # 글이 이미 있을 수 있는 실패는 자동 재시도하지 않는다(중복 글 방지).
        should_retry = (
            retryable and not post_may_exist and code != "PUBLISH_UNVERIFIED" and job.attempt_count < job.max_attempts
        )
        if url and url not in message:
            message = f"{message} (post URL: {url})"
        if publish_job is not None and code != _DUPLICATE_GUARD:
            if known_url and not publish_job.result_url:
                publish_job.result_url = known_url
            if should_retry:
                # 같은 잡이 다시 실행될 것이므로 대기 상태로 되돌린다.
                publish_job.status = PublishStatus.PENDING
                self._set_article_status(publish_job, ArticleStatus.READY_TO_PUBLISH)
            elif post_may_exist:
                publish_job.status = PublishStatus.PUBLISH_UNVERIFIED
                self._set_article_status(publish_job, ArticleStatus.QUARANTINED)
            else:
                publish_job.status = self._publish_status_for(code, was_publishing)
                if code in ("PREFLIGHT_FAILED", "BROWSER_LAUNCH_FAILED"):
                    # 발행이 막혔거나 브라우저가 뜨지 못했을 뿐 글은 만들어지지 않았다: 발행 대기 상태로 되돌린다.
                    self._set_article_status(publish_job, ArticleStatus.READY_TO_PUBLISH)
                elif code in _QUARANTINE_CODES and (code != "INTERRUPTED" or was_publishing):
                    self._set_article_status(publish_job, ArticleStatus.QUARANTINED)
        now = utc_now()
        job.status = JobStatus.RETRY_WAIT if should_retry else JobStatus.FAILED
        job.run_after = now + timedelta(seconds=30) if should_retry else job.run_after
        job.finished_at = now if not should_retry else None
        job.last_error_code = code
        job.last_error_message = message
        run.status = job.status
        run.finished_at = now
        run.error_code = code
        run.error_message = message
        self.session.commit()

    def _record_interruption(self, job: Job, run: JobRun, error: BaseException) -> None:
        """KeyboardInterrupt 등으로 중단됐을 때 잡을 FAILED/INTERRUPTED로 남긴다. 기록 실패가 원래 중단을 가리지 않게 한다."""
        self.last_outcome = "FAILED"
        try:
            self._finish_failure(
                job,
                run,
                "INTERRUPTED",
                f"작업이 {type(error).__name__}로 중단됐습니다. 브라우저 단계에서 중단됐다면 글이 이미 발행됐을 수 있으니 "
                "Tistory 글 관리에서 확인하세요.",
                retryable=False,
            )
        except Exception:
            # DB까지 못 쓰는 상황이면 RUNNING으로 남고, 다음 run_once의 만료 복구(STALE_RUNNING)가 정리한다.
            pass

    def _set_article_status(self, publish_job: PublishJob, status: ArticleStatus) -> None:
        version = publish_job.article_version
        version.status = status
        version.article.status = status

    @staticmethod
    def _publish_status_for(code: str, was_publishing: bool = False) -> PublishStatus:
        if code == "AUTH_REQUIRED":
            return PublishStatus.AUTH_REQUIRED
        if code == "UI_BROKEN":
            return PublishStatus.UI_BROKEN
        if code in ("PUBLISH_UNVERIFIED", "STALE_RUNNING"):
            return PublishStatus.PUBLISH_UNVERIFIED
        if code == "INTERRUPTED" and was_publishing:
            # 브라우저 단계에서 중단됐다면 글이 이미 있을 수 있다.
            return PublishStatus.PUBLISH_UNVERIFIED
        if code == "PREFLIGHT_FAILED":
            return PublishStatus.BLOCKED
        return PublishStatus.FAILED
