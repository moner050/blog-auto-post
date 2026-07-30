from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, TopicCandidate, TopicCandidateStatus


def test_topic_candidate_persists_sources_and_new_status(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'topic_candidates.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        candidate = TopicCandidate(
            batch_id="batch-1",
            topic="정부24 모바일 신분증 발급",
            topic_hash="f" * 64,
            category="정부24",
            reason="최근 모바일 신분증 이용 안내가 화제입니다.",
            sources_json=[{"title": "공식 안내", "url": "https://www.gov.kr"}],
            status=TopicCandidateStatus.NEW,
        )
        session.add(candidate)
        session.commit()

        stored = session.get(TopicCandidate, candidate.id)
        assert stored is not None
        assert stored.status == TopicCandidateStatus.NEW
        assert stored.sources_json == [{"title": "공식 안내", "url": "https://www.gov.kr"}]
