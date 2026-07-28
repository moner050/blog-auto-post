from __future__ import annotations

from unittest.mock import MagicMock, patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.db.session import create_session_factory
from app.llm.generator import GeneratedArticle
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
