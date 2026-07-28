from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.db.models import ArticleStatus, Job, JobRun, JobStatus, PublishJob, PublishStatus, utc_now
from app.jobs.queue import JobQueue
from app.publishing.client import PublisherClient, PublisherFailure
from app.publishing.guards import PublicationDraft, PublicationPreflightError, validate_preflight


class PublisherWorker:
    def __init__(self, session: Session, settings: Settings, publisher: PublisherClient):
        self.session = session
        self.settings = settings
        self.publisher = publisher

    def run_once(self, worker_id: str) -> bool:
        job = JobQueue(self.session).claim_next(worker_id, utc_now())
        if job is None:
            return False
        run = JobRun(job_id=job.id, worker_id=worker_id, status=JobStatus.RUNNING)
        self.session.add(run)
        self.session.flush()
        try:
            self._publish(job)
        except PublicationPreflightError as error:
            self._finish_failure(job, run, "PREFLIGHT_FAILED", str(error), retryable=False)
        except PublisherFailure as error:
            self._finish_failure(job, run, error.code, str(error), retryable=error.retryable)
        except Exception as error:
            self._finish_failure(job, run, "UNEXPECTED_ERROR", str(error), retryable=False)
        else:
            job.status = JobStatus.SUCCEEDED
            job.finished_at = utc_now()
            run.status = JobStatus.SUCCEEDED
            run.finished_at = utc_now()
            self.session.commit()
        return True

    def _publish(self, job: Job) -> None:
        if job.job_type != "PUBLISH_TISTORY":
            raise PublisherFailure("UNSUPPORTED_JOB", f"unsupported job type: {job.job_type}")
        publish_job = self.session.get(PublishJob, job.entity_id)
        if publish_job is None:
            raise PublisherFailure("PUBLISH_JOB_NOT_FOUND", "publish job does not exist")
        version = publish_job.article_version
        draft = PublicationDraft(
            title=version.title,
            body_html=version.body_html,
            tags=list(version.tags_json),
            category=publish_job.category,
            target_blog_name=publish_job.target_blog_name,
            visibility=publish_job.visibility,
            thumbnail_path=__import__("pathlib").Path(version.thumbnail_path),
        )
        validate_preflight(self.settings, draft)
        publish_job.status = PublishStatus.PUBLISHING
        version.status = ArticleStatus.PUBLISHING
        version.article.status = ArticleStatus.PUBLISHING
        self.session.flush()
        post = self.publisher.publish(draft)
        self.publisher.verify_private(post, draft)
        publish_job.status = PublishStatus.VERIFIED
        publish_job.result_url = post.url
        publish_job.verification_details = {"visibility": "PRIVATE", "title": draft.title}
        version.status = ArticleStatus.VERIFIED
        version.article.status = ArticleStatus.VERIFIED

    def _finish_failure(self, job: Job, run: JobRun, code: str, message: str, retryable: bool) -> None:
        publish_job = self.session.get(PublishJob, job.entity_id)
        if publish_job is not None:
            publish_job.status = self._publish_status_for(code)
        should_retry = retryable and job.attempt_count < job.max_attempts
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

    @staticmethod
    def _publish_status_for(code: str) -> PublishStatus:
        if code == "AUTH_REQUIRED":
            return PublishStatus.AUTH_REQUIRED
        if code == "UI_BROKEN":
            return PublishStatus.UI_BROKEN
        if code == "PUBLISH_UNVERIFIED":
            return PublishStatus.PUBLISH_UNVERIFIED
        if code == "PREFLIGHT_FAILED":
            return PublishStatus.BLOCKED
        return PublishStatus.FAILED
