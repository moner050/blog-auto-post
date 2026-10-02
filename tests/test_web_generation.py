"""대시보드의 글 생성 연동: 후보 문맥 전달, 진행 신호(하트비트), 경고 전달, stale 기준."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.core.settings import Settings
from app.db.models import TopicCandidate, TopicCandidateStatus, utc_now
from app.db.session import create_session_factory
from app.llm.generator import GeneratedArticle
from app.web import app as web_module
from app.web.app import app

SOURCES = [{"title": "정부24 안내", "url": "https://www.gov.kr/portal/service/serviceInfo/PTR000050"}]


@pytest.fixture
def web_client(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'web_generation.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")
    with patch("app.web.app.session_factory", create_session_factory(db_url)):
        yield TestClient(app)


def add_candidate(**overrides) -> int:
    values = {
        "batch_id": str(uuid4()),
        "topic": "DSR 규제한다더니 신규 대출 70%가 예외? 지금 대출받을 때 꼭 확인할 것",
        "topic_hash": uuid4().hex + uuid4().hex,
        "category": "대출·금융",
        "reason": "최신 가계대출 통계가 나왔습니다.",
        "sources_json": SOURCES,
        "status": TopicCandidateStatus.NEW,
        **overrides,
    }
    with web_module.session_factory() as session:
        candidate = TopicCandidate(**values)
        session.add(candidate)
        session.commit()
        return candidate.id


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def generated(**overrides) -> GeneratedArticle:
    return GeneratedArticle(
        **{"title": "생성된 제목", "body_html": "<p>본문</p>", "tags": ["대출", "DSR"], **overrides}
    )


def test_topic_candidate_generation_passes_the_whole_candidate_to_the_generator(web_client) -> None:
    candidate_id = add_candidate()

    with patch("app.web.app.ArticleGenerator") as generator_class:
        generator_class.return_value.generate.return_value = generated(warnings=["본문이 1,820자다."])
        response = web_client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

    assert response.status_code == 200
    assert response.json()["warnings"] == ["본문이 1,820자다."]
    call = generator_class.return_value.generate.call_args
    assert call.args == ("DSR 규제한다더니 신규 대출 70%가 예외? 지금 대출받을 때 꼭 확인할 것",)
    assert call.kwargs["category"] == "대출·금융"
    assert call.kwargs["reason"] == "최신 가계대출 통계가 나왔습니다."
    assert call.kwargs["sources"] == SOURCES
    assert callable(call.kwargs["on_progress"])


def test_heartbeat_refreshes_only_candidates_that_are_still_generating(web_client) -> None:
    old = utc_now() - timedelta(seconds=100)
    generating = add_candidate(status=TopicCandidateStatus.GENERATING, updated_at=old)
    idle = add_candidate(status=TopicCandidateStatus.NEW, updated_at=old)

    web_module._heartbeat(generating)("revise")
    web_module._heartbeat(idle)("revise")

    with web_module.session_factory() as session:
        refreshed, untouched = session.get(TopicCandidate, generating), session.get(TopicCandidate, idle)
        assert utc_now() - as_utc(refreshed.updated_at) < timedelta(seconds=10)
        assert utc_now() - as_utc(untouched.updated_at) > timedelta(seconds=90)


def test_manual_article_generation_uses_the_category_and_returns_warnings(web_client) -> None:
    with patch("app.web.app.ArticleGenerator") as generator_class:
        generator_class.return_value.generate.return_value = generated(warnings=["경고"])
        response = web_client.post("/api/articles/generate", json={"topic": "주민등록등본 발급 방법", "category": "정부지원·민원"})

    assert response.status_code == 200 and response.json()["warnings"] == ["경고"]
    call = generator_class.return_value.generate.call_args
    assert call.args == ("주민등록등본 발급 방법",) and call.kwargs == {"category": "정부지원·민원"}


def test_a_failed_generation_is_recorded_and_can_be_retried(web_client) -> None:
    candidate_id = add_candidate()

    with patch("app.web.app.ArticleGenerator") as generator_class:
        generator_class.return_value.generate.side_effect = ValueError("응답이 길이 제한으로 잘렸습니다.")
        failed = web_client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

    assert failed.status_code == 502 and "길이 제한" in failed.json()["detail"]
    candidate = web_client.get("/api/topic-candidates").json()["candidates"][0]
    assert candidate["status"] == "FAILED" and "길이 제한" in candidate["error_message"]


def test_stale_window_boundaries_and_why_the_heartbeat_is_needed() -> None:
    window = web_module.TOPIC_GENERATION_STALE_AFTER
    timeout = web_module.settings.article_timeout_seconds
    now = utc_now()

    def generating(updated_at: datetime) -> TopicCandidate:
        return TopicCandidate(status=TopicCandidateStatus.GENERATING, updated_at=updated_at)

    # 경계: 기준에서 5초 안쪽은 진행 중, 5초 바깥은 멈춘 것으로 본다(naive 시각도 같은 기준으로 읽는다).
    assert not web_module._generation_is_stale(generating(now - window + timedelta(seconds=5)))
    assert web_module._generation_is_stale(generating(now - window - timedelta(seconds=5)))
    assert not web_module._generation_is_stale(generating((now - window + timedelta(seconds=5)).replace(tzinfo=None)))
    assert web_module._generation_is_stale(generating(None))
    # 호출 1회가 끝나기 전에는 stale이 아니다. 하지만 작성+재작성(호출 2회)을 하트비트 없이 기다릴 만큼 길지는 않다.
    # 그래서 호출 직전마다 하트비트(updated_at 갱신)가 있어야 정상 진행 중인 작업을 가로채지 않는다.
    assert window.total_seconds() > timeout
    assert window.total_seconds() < 2 * timeout


def test_stats_show_the_article_model_override(web_client) -> None:
    with patch("app.web.app.settings", Settings(_env_file=None, article_model="low")):
        assert web_client.get("/api/stats").json()["model"] == "low"
    with patch("app.web.app.settings", Settings(_env_file=None, perplexity_model="sonar")):
        assert web_client.get("/api/stats").json()["model"] == "sonar"
