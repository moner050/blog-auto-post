from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Job, JobStatus


class JobQueue:
    def __init__(self, session: Session):
        self.session = session

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
