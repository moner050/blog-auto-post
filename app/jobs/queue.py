from datetime import datetime, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Job, JobStatus


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite/MySQL은 timezone 없는 datetime을 돌려준다(저장은 UTC). 비교할 수 있게 UTC aware로 맞춘다."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class JobQueue:
    def __init__(self, session: Session):
        self.session = session

    def find_stale_running(self, older_than: datetime) -> list[Job]:
        """RUNNING인데 락 시각(locked_at, 없으면 started_at)이 older_than 이전인 잡들(락 임대 만료).

        락 시각이 하나도 없는 RUNNING 잡은 만료로 본다(정상 클레임은 항상 locked_at을 쓴다).
        다른 프로세스가 방금 클레임한 잡을 세션 캐시의 옛 값(PENDING·locked_at 없음)으로 오판하지 않도록
        행을 DB 값으로 다시 채운다(populate_existing).
        """
        cutoff = as_utc(older_than)
        running = self.session.scalars(
            select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.id).execution_options(populate_existing=True)
        ).all()
        stale: list[Job] = []
        for job in running:
            locked = as_utc(job.locked_at or job.started_at)
            if locked is None or locked <= cutoff:
                stale.append(job)
        return stale

    def claim_next(self, worker_id: str, now: datetime) -> Job | None:
        while True:
            candidate_id = self.session.scalar(
                select(Job.id)
                .where(Job.status.in_([JobStatus.PENDING, JobStatus.RETRY_WAIT]))
                .where(or_(Job.run_after.is_(None), Job.run_after <= now))
                .order_by(Job.priority.desc(), Job.id.asc())
                .limit(1)
            )
            if candidate_id is None:
                return None
            result = self.session.execute(
                update(Job)
                .where(Job.id == candidate_id, Job.status.in_([JobStatus.PENDING, JobStatus.RETRY_WAIT]))
                .values(
                    status=JobStatus.RUNNING,
                    locked_by=worker_id,
                    locked_at=now,
                    started_at=now,
                    attempt_count=Job.attempt_count + 1,
                )
            )
            self.session.commit()
            if result.rowcount == 1:
                return self.session.get(Job, candidate_id)
