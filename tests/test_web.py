from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.db.session import create_session_factory
from app.db.models import TopicCandidate, TopicCandidateStatus, utc_now
from app.llm.generator import GeneratedArticle
from app.topics.discovery import DiscoveredTopic, TopicDiscoveryError
from app.web.app import app


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "web_test.db"
    db_url = f"sqlite:///{db_path}"

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")

    test_factory = create_session_factory(db_url)
    with patch("app.web.app.session_factory", test_factory):
        yield TestClient(app)


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "티스토리 자동화 관리자 대시보드" in response.text
    assert "AI 주제 12개 추천받기" in response.text
    assert 'id="topic-candidate-list"' in response.text
    assert 'src="/static/js/main.js?v=' in response.text


def test_get_stats_api(client):
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_articles" in data
    assert "verified_articles" in data
    assert "pending_jobs" in data
    assert "failed_jobs" in data


def test_list_articles_api(client):
    response = client.get("/api/articles")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_generate_article_api(client):
    generated = GeneratedArticle(
        title="테스트 웹 생성 포스팅",
        body_html="<p>웹 생성 본문</p>",
        tags=["웹", "테스트"],
        summary="요약",
    )
    with patch("app.web.app.ArticleGenerator") as mock_gen_class:
        mock_instance = MagicMock()
        mock_instance.generate.return_value = generated
        mock_gen_class.return_value = mock_instance

        response = client.post(
            "/api/articles/generate",
            json={"topic": "테스트 웹 생성 주제", "category": "테스트"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["title"] == "테스트 웹 생성 포스팅"


def test_delete_article_api(client):
    generated = GeneratedArticle(
        title="삭제 대상 포스팅",
        body_html="<p>삭제 본문</p>",
        tags=["삭제"],
        summary="요약",
    )
    with patch("app.web.app.ArticleGenerator") as mock_gen_class:
        mock_instance = MagicMock()
        mock_instance.generate.return_value = generated
        mock_gen_class.return_value = mock_instance

        gen_resp = client.post(
            "/api/articles/generate",
            json={"topic": "삭제 테스트 주제"},
        )
        article_id = gen_resp.json()["article_id"]

        del_resp = client.delete(f"/api/articles/{article_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["success"] is True

        get_resp = client.get(f"/api/articles/{article_id}")
        assert get_resp.status_code == 404


def test_retry_article_api(client):
    generated = GeneratedArticle(
        title="재시도 대상 포스팅",
        body_html="<p>재시도 본문</p>",
        tags=["재시도"],
        summary="요약",
    )
    with patch("app.web.app.ArticleGenerator") as mock_gen_class:
        mock_instance = MagicMock()
        mock_instance.generate.return_value = generated
        mock_gen_class.return_value = mock_instance

        gen_resp = client.post(
            "/api/articles/generate",
            json={"topic": "재시도 테스트 주제"},
        )
        article_id = gen_resp.json()["article_id"]

        retry_resp = client.post(f"/api/articles/{article_id}/retry")
        assert retry_resp.status_code == 200
        assert retry_resp.json()["success"] is True

        detail_resp = client.get(f"/api/articles/{article_id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["status"] == "READY_TO_PUBLISH"


def test_run_worker_api(client):
    with patch("app.web.app.PublisherWorker") as mock_worker_class:
        mock_instance = MagicMock()
        mock_instance.run_once.return_value = False
        mock_worker_class.return_value = mock_instance

        response = client.post("/api/jobs/run-worker")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["handled"] is False


def test_discover_and_list_topic_candidates_api(client):
    candidates = [
        DiscoveredTopic(
            topic="정부24 모바일 신분증 발급 방법",
            topic_hash="a" * 64,
            category="정부24",
            reason="최근 이용 안내와 문의가 늘었습니다.",
            sources=[{"title": "공식 안내", "url": "https://www.gov.kr"}],
        ),
        DiscoveredTopic(
            topic="여름 기차 여행 짐 줄이는 방법",
            topic_hash="b" * 64,
            category="여행꿀팁",
            reason="휴가철 여행 준비 수요가 높습니다.",
            sources=[{"title": "여행 뉴스", "url": "https://example.com/travel"}],
        ),
    ]
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = candidates

        response = client.post("/api/topic-candidates/discover")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert data["candidates"][0]["sources"] == [{"title": "공식 안내", "url": "https://www.gov.kr"}]

    listed = client.get("/api/topic-candidates")
    assert listed.status_code == 200
    assert [candidate["topic"] for candidate in listed.json()["candidates"]] == [
        "정부24 모바일 신분증 발급 방법",
        "여름 기차 여행 짐 줄이는 방법",
    ]


def test_failed_discovery_keeps_latest_topic_candidates(client):
    candidate = DiscoveredTopic(
        topic="여름 기차 여행 짐 줄이는 방법",
        topic_hash="c" * 64,
        category="여행꿀팁",
        reason="휴가철 여행 준비 수요가 높습니다.",
        sources=[{"title": "여행 뉴스", "url": "https://example.com/travel"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        first = client.post("/api/topic-candidates/discover")

        mock_discoverer_class.return_value.discover.side_effect = TopicDiscoveryError("no valid candidates")
        failed = client.post("/api/topic-candidates/discover")

    assert first.status_code == 200
    assert failed.status_code == 502
    listed = client.get("/api/topic-candidates")
    assert listed.json()["count"] == 1
    assert listed.json()["candidates"][0]["topic"] == candidate.topic


def test_delete_topic_candidate_removes_candidate_from_latest_batch(client):
    candidate = DiscoveredTopic(
        topic="삭제할 추천 주제",
        topic_hash="9" * 64,
        category="생활꿀팁",
        reason="삭제 API를 검증합니다.",
        sources=[{"title": "검증 출처", "url": "https://example.com/delete"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        candidate_id = client.post("/api/topic-candidates/discover").json()["candidates"][0]["id"]

    response = client.delete(f"/api/topic-candidates/{candidate_id}")

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert client.get("/api/topic-candidates").json()["count"] == 0


def test_delete_generating_topic_candidate_returns_conflict(client):
    candidate = DiscoveredTopic(
        topic="생성 중인 추천 주제",
        topic_hash="8" * 64,
        category="생활꿀팁",
        reason="생성 선점 상태를 검증합니다.",
        sources=[{"title": "검증 출처", "url": "https://example.com/generating"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        candidate_id = client.post("/api/topic-candidates/discover").json()["candidates"][0]["id"]

    from app.web import app as web_module

    with web_module.session_factory() as session:
        stored = session.get(TopicCandidate, candidate_id)
        assert stored is not None
        stored.status = TopicCandidateStatus.GENERATING
        session.commit()

    response = client.delete(f"/api/topic-candidates/{candidate_id}")

    assert response.status_code == 409
    assert client.get("/api/topic-candidates").json()["count"] == 1


def test_generate_topic_candidate_creates_idempotent_private_publish_queue(client):
    candidate = DiscoveredTopic(
        topic="정부24 모바일 신분증 발급 방법",
        topic_hash="d" * 64,
        category="정부24",
        reason="최근 이용 안내와 문의가 늘었습니다.",
        sources=[{"title": "공식 안내", "url": "https://www.gov.kr"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        discovered = client.post("/api/topic-candidates/discover").json()
    candidate_id = discovered["candidates"][0]["id"]

    generated = GeneratedArticle(
        title="모바일 신분증 정부24 발급 방법",
        body_html="<p>초안 본문</p>",
        tags=["정부24", "모바일신분증"],
    )
    with patch("app.web.app.ArticleGenerator") as mock_generator_class:
        mock_generator_class.return_value.generate.return_value = generated
        created = client.post(f"/api/topic-candidates/{candidate_id}/generate-article")
        repeated = client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

    assert created.status_code == 200
    assert repeated.status_code == 200
    assert created.json()["article_id"] == repeated.json()["article_id"]
    assert mock_generator_class.return_value.generate.call_count == 1

    article = client.get(f"/api/articles/{created.json()['article_id']}")
    assert article.json()["status"] == "READY_TO_PUBLISH"
    assert client.get("/api/stats").json()["pending_jobs"] == 1
    assert client.post(f"/api/articles/{created.json()['article_id']}/retry").status_code == 200

    assert client.delete(f"/api/articles/{created.json()['article_id']}").status_code == 200
    restored = client.get("/api/topic-candidates").json()["candidates"][0]
    assert restored["status"] == "NEW"
    assert restored["article_id"] is None


def test_failed_topic_draft_generation_can_retry(client):
    candidate = DiscoveredTopic(
        topic="여름 기차 여행 짐 줄이는 방법",
        topic_hash="e" * 64,
        category="여행꿀팁",
        reason="휴가철 여행 준비 수요가 높습니다.",
        sources=[{"title": "여행 뉴스", "url": "https://example.com/travel"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        candidate_id = client.post("/api/topic-candidates/discover").json()["candidates"][0]["id"]

    with patch("app.web.app.ArticleGenerator") as mock_generator_class:
        mock_generator_class.return_value.generate.side_effect = ValueError("Sonar unavailable")
        failed = client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

        mock_generator_class.return_value.generate.side_effect = None
        mock_generator_class.return_value.generate.return_value = GeneratedArticle(
            title="기차 여행 짐 줄이는 방법",
            body_html="<p>초안 본문</p>",
            tags=["여행"],
        )
        retried = client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

    assert failed.status_code == 502
    assert retried.status_code == 200
    assert client.get("/api/topic-candidates").json()["candidates"][0]["status"] == "DRAFT_CREATED"


def test_stale_topic_draft_generation_can_retry(client):
    candidate = DiscoveredTopic(
        topic="여름 기차 여행 짐 줄이는 방법",
        topic_hash="f" * 64,
        category="여행꿀팁",
        reason="휴가철 여행 준비 수요가 높습니다.",
        sources=[{"title": "여행 뉴스", "url": "https://example.com/travel"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        candidate_id = client.post("/api/topic-candidates/discover").json()["candidates"][0]["id"]

    from app.web import app as web_module

    with web_module.session_factory() as session:
        stored = session.get(TopicCandidate, candidate_id)
        assert stored is not None
        stored.status = TopicCandidateStatus.GENERATING
        stored.updated_at = utc_now() - timedelta(minutes=3)
        session.commit()

    with patch("app.web.app.ArticleGenerator") as mock_generator_class:
        mock_generator_class.return_value.generate.return_value = GeneratedArticle(
            title="기차 여행 짐 줄이는 방법",
            body_html="<p>초안 본문</p>",
            tags=["여행"],
        )
        response = client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_active_topic_draft_generation_returns_conflict(client):
    candidate = DiscoveredTopic(
        topic="정부24 전자문서지갑 사용 방법",
        topic_hash="a" * 64,
        category="정부24",
        reason="전자문서 이용 수요가 높습니다.",
        sources=[{"title": "정부24 안내", "url": "https://example.com/gov24"}],
    )
    with patch("app.web.app.TopicDiscoverer") as mock_discoverer_class:
        mock_discoverer_class.return_value.discover.return_value = [candidate]
        candidate_id = client.post("/api/topic-candidates/discover").json()["candidates"][0]["id"]

    from app.web import app as web_module

    with web_module.session_factory() as session:
        stored = session.get(TopicCandidate, candidate_id)
        assert stored is not None
        stored.status = TopicCandidateStatus.GENERATING
        stored.updated_at = utc_now()
        session.commit()

    with patch("app.web.app.ArticleGenerator") as mock_generator_class:
        response = client.post(f"/api/topic-candidates/{candidate_id}/generate-article")

    assert response.status_code == 409
    mock_generator_class.return_value.generate.assert_not_called()
