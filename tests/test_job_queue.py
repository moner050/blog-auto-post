from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, Job, JobStatus
from app.jobs.queue import JobQueue


def test_claiming_same_due_job_from_two_sessions_returns_it_once(tmp_path) -> None:
    """Dropping the conditional pending update would let multiple workers publish once."""
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            Job(
                job_type="PUBLISH_TISTORY",
                entity_id=1,
                payload_json={},
                status=JobStatus.PENDING,
                priority=90,
                run_after=now - timedelta(seconds=1),
                max_attempts=2,
            )
        )
        session.commit()

    first_session = Session(engine)
    second_session = Session(engine)
    try:
        first = JobQueue(first_session).claim_next("worker-a", now)
        second = JobQueue(second_session).claim_next("worker-b", now)

        assert first is not None
        assert second is None
        assert first.status is JobStatus.RUNNING
        assert first.locked_by == "worker-a"
    finally:
        first_session.close()
        second_session.close()
