"""대시보드 보호(W1·W2·W3·P3 웹 부분): Host·같은 출처·Basic 인증·보안 헤더, 미리보기 sandbox, 재등록·삭제 가드, 워커 결과 문구, stale 후보, CLI 바인딩 가드.

서버가 유료 LLM 호출·글 삭제·Playwright 발행을 운영자 PC에서 실행하므로, 이 파일의 테스트는 "차단된 요청에서는 유료 호출이 0번"이라는 점을 함께 확인한다.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.content.static import DraftArticleInput, StaticArticleInput, register_draft_article, register_private_article
from app.core.settings import Settings
from app.db.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    Job,
    JobStatus,
    PublishJob,
    PublishStatus,
    TopicCandidate,
    TopicCandidateStatus,
    utc_now,
)
from app.db.session import create_session_factory
from app.jobs.worker import PublisherWorker
from app.llm.generator import GeneratedArticle
from app.topics.discovery import DiscoveredTopic
from app.web import app as web_module
from app.web.app import app

MAIN_JS = web_module.BASE_DIR / "static" / "js" / "main.js"

PASSWORD = "s3cret:비밀번호!"  # 콜론과 비ASCII가 들어 있어도 인증이 되어야 한다


# ---------------------------------------------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """마이그레이션을 마친 빈 SQLite를 모듈에서 한 번만 만들고, 테스트마다 복사해서 쓴다."""
    path = tmp_path_factory.mktemp("web_security") / "template.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    return path


@pytest.fixture
def web_client(migrated_db: Path, tmp_path: Path):
    db_path = tmp_path / "web_security.db"
    shutil.copy(migrated_db, db_path)
    factory = create_session_factory(f"sqlite:///{db_path}")
    try:
        with patch("app.web.app.session_factory", factory):
            yield TestClient(app)
    finally:
        factory.kw["bind"].dispose()  # 열린 SQLite 연결이 남으면 Windows에서 임시 폴더 정리를 막는다


def configured(**overrides):
    """대시보드 설정을 바꿔 끼운다(미들웨어는 요청마다 settings를 읽는다)."""
    return patch("app.web.app.settings", Settings(_env_file=None, **overrides))


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def add_article(
    tmp_path: Path,
    title: str,
    *,
    article: ArticleStatus = ArticleStatus.READY_TO_PUBLISH,
    version: ArticleStatus | None = None,  # 지정하지 않으면 글 상태와 같다
    publish: PublishStatus = PublishStatus.PENDING,
    job: JobStatus = JobStatus.PENDING,
    error_code: str | None = None,
) -> SimpleNamespace:
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"image")
    with web_module.session_factory() as session:
        registered = register_private_article(
            session,
            StaticArticleInput(
                title=title,
                body_html=f"<p>{title} 본문</p>",
                tags=["보안"],
                category="생활꿀팁",
                target_blog_name="blog",
                thumbnail_path=thumbnail,
            ),
        )
        session.get(Article, registered.article_id).status = article
        session.get(ArticleVersion, registered.article_version_id).status = version or article
        session.get(PublishJob, registered.publish_job_id).status = publish
        stored_job = session.get(Job, registered.job_id)
        stored_job.status = job
        stored_job.attempt_count = 1
        stored_job.last_error_code = error_code
        stored_job.last_error_message = "이전 실패 사유" if error_code else None
        session.commit()
    return registered


def article_state(registered: SimpleNamespace) -> tuple[ArticleStatus, PublishStatus, JobStatus]:
    with web_module.session_factory() as session:
        return (
            session.get(Article, registered.article_id).status,
            session.get(PublishJob, registered.publish_job_id).status,
            session.get(Job, registered.job_id).status,
        )


def add_candidate(**overrides) -> int:
    values = {
        "batch_id": "c" * 36,  # 한 테스트의 후보는 같은 배치(목록 API는 최신 배치만 돌려준다)
        "topic": "보안 점검용 주제",
        "topic_hash": uuid4().hex + uuid4().hex,
        "category": "생활꿀팁",
        "reason": "이유",
        "sources_json": [{"title": "출처", "url": "https://example.com/source"}],
        "status": TopicCandidateStatus.NEW,
        **overrides,
    }
    with web_module.session_factory() as session:
        candidate = TopicCandidate(**values)
        session.add(candidate)
        session.commit()
        return candidate.id


@pytest.fixture
def paid_calls():
    """유료 LLM·워커·발행기를 가짜로 바꾼다. 차단된 요청이라면 이 객체들은 한 번도 만들어지면 안 된다."""
    with (
        patch("app.web.app.TopicDiscoverer") as discoverer,
        patch("app.web.app.ArticleGenerator") as generator,
        patch("app.web.app.PublisherWorker") as worker,
        patch("app.web.app.TistoryPublisher") as publisher,
        patch("app.web.app.PerplexityClient") as perplexity,
    ):
        discoverer.return_value.discover.return_value = [
            DiscoveredTopic(
                topic="통과한 요청이 저장한 주제",
                topic_hash="a" * 64,
                category="생활꿀팁",
                reason="이유",
                sources=[{"title": "출처", "url": "https://example.com/source"}],
            )
        ]
        generator.return_value.generate.return_value = GeneratedArticle(
            title="통과한 요청이 만든 글", body_html="<p>본문</p>", tags=["보안"]
        )
        worker.return_value.run_once.return_value = True
        worker.return_value.last_outcome = "SUCCEEDED"
        yield SimpleNamespace(
            discoverer=discoverer, generator=generator, worker=worker, publisher=publisher, perplexity=perplexity
        )


def assert_no_paid_calls(paid: SimpleNamespace) -> None:
    for name, mock in vars(paid).items():
        assert not mock.called, f"차단됐어야 할 요청이 {name}까지 실행됐다"


@pytest.fixture
def targets(web_client: TestClient, tmp_path: Path) -> dict[str, str]:
    """상태를 바꾸는 POST 4종이 가리킬 실제 후보·글을 만들어 두고 경로를 돌려준다."""
    candidate_id = add_candidate()
    article = add_article(tmp_path, "재등록 대상 글")
    return {
        "run-worker": "/api/jobs/run-worker",
        "discover": "/api/topic-candidates/discover",
        "generate-article": f"/api/topic-candidates/{candidate_id}/generate-article",
        "retry": f"/api/articles/{article.article_id}/retry",
    }


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def tags_of(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


def parse_csp(value: str) -> dict[str, list[str]]:
    policy: dict[str, list[str]] = {}
    for directive in value.split(";"):
        if directive.strip():
            name, *sources = directive.split()
            policy[name] = sources
    return policy


# ---------------------------------------------------------------------------------------------------------------
# W1.1 Host 검사(DNS 리바인딩)
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host",
    [
        "rebind.attacker.example",
        "rebind.attacker.example:9000",
        "127.0.0.1.attacker.example",
        "localhost.attacker.example:9000",
        "localhost:9000@attacker.example",
        "[::2]:9000",
        "[::1",
        "[::1]x",
    ],
)
def test_foreign_host_is_rejected_with_400(web_client, host) -> None:
    for path in ("/", "/api/stats", "/static/js/main.js", "/nope"):
        response = web_client.get(path, headers={"Host": host})
        assert response.status_code == 400, path


def test_the_host_rejection_tells_the_operator_which_setting_to_change(web_client) -> None:
    response = web_client.get("/api/stats", headers={"Host": "192.168.0.10:9000"})  # 예: LAN IP나 0.0.0.0으로 접속한 경우

    assert response.status_code == 400
    assert "DASHBOARD_ALLOWED_HOSTS" in response.json()["detail"]


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:9000", "localhost", "LOCALHOST:9000", "Localhost", "[::1]", "[::1]:9000", "::1", "testserver"],
)
def test_allowed_hosts_are_accepted_regardless_of_case_port_and_ipv6_brackets(web_client, host) -> None:
    assert web_client.get("/api/stats", headers={"Host": host}).status_code == 200


def test_dns_rebinding_cannot_read_or_delete_even_with_same_origin_looking_headers(web_client, tmp_path) -> None:
    """리바인딩된 공격자 페이지는 Host·Origin·Sec-Fetch-Site가 모두 '같은 출처'로 보인다. 막는 것은 Host 검사뿐이다."""
    article = add_article(tmp_path, "리바인딩으로 읽히면 안 되는 글")
    candidate_id = add_candidate()
    rebound = {
        "Host": "rebind.attacker.example:9000",
        "Origin": "http://rebind.attacker.example:9000",
        "Sec-Fetch-Site": "same-origin",
    }

    assert web_client.get("/api/articles", headers=rebound).status_code == 400
    leaked = web_client.get(f"/api/articles/{article.article_id}", headers=rebound)
    assert leaked.status_code == 400 and "본문" not in leaked.text
    assert web_client.delete(f"/api/topic-candidates/{candidate_id}", headers=rebound).status_code == 400
    assert web_client.delete(f"/api/articles/{article.article_id}", headers=rebound).status_code == 400

    assert web_client.get(f"/api/articles/{article.article_id}").status_code == 200
    assert web_client.get("/api/topic-candidates").json()["count"] == 1


def test_allowed_hosts_come_from_the_current_settings(web_client) -> None:
    with configured(dashboard_allowed_hosts="dash.example, [::1] ,LAN-PC:9000"):
        assert web_client.get("/api/stats", headers={"Host": "dash.example:8080"}).status_code == 200
        assert web_client.get("/api/stats", headers={"Host": "lan-pc"}).status_code == 200
        assert web_client.get("/api/stats", headers={"Host": "[::1]:9000"}).status_code == 200
        assert web_client.get("/api/stats", headers={"Host": "127.0.0.1:9000"}).status_code == 400
        assert web_client.get("/api/stats", headers={"Host": "testserver"}).status_code == 400
    with configured(dashboard_allowed_hosts=" , "):  # 비우면 루프백 기본값으로 돌아간다(전부 막지도, 전부 열지도 않는다)
        assert web_client.get("/api/stats", headers={"Host": "localhost:9000"}).status_code == 200
        assert web_client.get("/api/stats", headers={"Host": "attacker.example"}).status_code == 400


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost", "localhost"),
        ("LocalHost:9000", "localhost"),
        ("127.0.0.1:80", "127.0.0.1"),
        ("localhost:", "localhost"),
        ("[::1]:9000", "::1"),
        ("[::1]", "::1"),
        ("::1", "::1"),
        ("[FE80::1]:1", "fe80::1"),
        ("localhost:90x0", None),
        ("localhost:9000@evil.example", None),
        ("[::1", None),
        ("[]:9000", None),
        ("[::1]9000", None),
        ("", None),
        (":9000", None),
    ],
)
def test_parse_host_handles_ports_and_ipv6_literals(value, expected) -> None:
    assert web_module._parse_host(value) == expected


# ---------------------------------------------------------------------------------------------------------------
# W1.2 같은 출처 검사(CSRF): 브라우저가 다른 사이트에서 보낼 수 있는 body 없는 POST 4종
# ---------------------------------------------------------------------------------------------------------------
CROSS_SITE_HEADERS = {
    "sec-fetch-cross-site": {"Sec-Fetch-Site": "cross-site"},
    "sec-fetch-same-site": {"Sec-Fetch-Site": "same-site"},  # 같은 호스트의 다른 포트도 다른 출처다
    "foreign-origin": {"Origin": "https://attacker.example"},
    "origin-null": {"Origin": "null"},
    "localhost-vs-127": {"Origin": "http://localhost:9000", "Host": "127.0.0.1:9000"},
    "both": {"Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.example"},
    "origin-with-other-port": {"Origin": "http://testserver:9000"},
}


@pytest.mark.parametrize("endpoint", ["run-worker", "discover", "generate-article", "retry"])
@pytest.mark.parametrize("headers", CROSS_SITE_HEADERS.values(), ids=CROSS_SITE_HEADERS.keys())
def test_cross_site_post_is_blocked_before_any_paid_or_worker_call(web_client, paid_calls, targets, endpoint, headers) -> None:
    response = web_client.post(targets[endpoint], headers=headers)

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]
    assert_no_paid_calls(paid_calls)


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH"])
def test_every_state_changing_method_is_checked(web_client, paid_calls, targets, method) -> None:
    response = web_client.request(method, targets["run-worker"], headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert_no_paid_calls(paid_calls)


def test_json_generation_endpoint_is_protected_even_without_a_preflight(web_client, paid_calls) -> None:
    """text/plain·Content-Type 없는 JSON 본문은 preflight 없이 보낼 수 있다. 최신 FastAPI의 strict_content_type은 이를 422로 막지만 fastapi>=0.100에서는 뚫렸다."""
    body = '{"topic": "공격자가 정한 주제"}'
    for content_type in ("text/plain", "application/json", None):
        headers = {"Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.example"}
        if content_type:
            headers["Content-Type"] = content_type
        assert web_client.post("/api/articles/generate", content=body, headers=headers).status_code == 403
    assert_no_paid_calls(paid_calls)

    same_origin = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert web_client.post("/api/articles/generate", json={"topic": "정상 주제"}, headers=same_origin).status_code == 200
    paid_calls.generator.return_value.generate.assert_called_once()


def test_blocked_cross_site_requests_change_nothing(web_client, paid_calls, tmp_path) -> None:
    candidate_id = add_candidate()
    verified = add_article(tmp_path, "삭제되면 안 되는 글", article=ArticleStatus.VERIFIED, publish=PublishStatus.VERIFIED, job=JobStatus.SUCCEEDED)
    attack = {"Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.example"}

    assert web_client.delete(f"/api/articles/{verified.article_id}", headers=attack).status_code == 403
    assert web_client.delete(f"/api/topic-candidates/{candidate_id}", headers=attack).status_code == 403
    assert web_client.post(f"/api/articles/{verified.article_id}/retry?force=true", headers=attack).status_code == 403

    assert article_state(verified) == (ArticleStatus.VERIFIED, PublishStatus.VERIFIED, JobStatus.SUCCEEDED)
    assert web_client.get("/api/topic-candidates").json()["count"] == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl·TestClient·CLI 도구처럼 아무 헤더도 보내지 않는 클라이언트
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://testserver"},
        {"Origin": "http://TESTSERVER"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    ],
)
@pytest.mark.parametrize("endpoint", ["run-worker", "discover", "generate-article", "retry"])
def test_same_origin_or_header_less_post_keeps_working(web_client, paid_calls, targets, endpoint, headers) -> None:
    response = web_client.post(targets[endpoint], headers=headers)

    assert response.status_code == 200, response.text
    if endpoint == "run-worker":
        paid_calls.worker.return_value.run_once.assert_called_once()
    if endpoint == "discover":
        paid_calls.discoverer.return_value.discover.assert_called_once()
    if endpoint == "generate-article":
        paid_calls.generator.return_value.generate.assert_called_once()


def test_the_dashboard_pages_own_requests_are_same_origin(web_client, paid_calls, targets) -> None:
    """브라우저가 대시보드 UI에서 보내는 요청(Host=127.0.0.1:9000, Origin 동일, Sec-Fetch-Site=same-origin)은 통과한다."""
    own = {"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:9000", "Sec-Fetch-Site": "same-origin"}

    assert web_client.post(targets["run-worker"], headers=own).status_code == 200
    ipv6 = {"Host": "[::1]:9000", "Origin": "http://[::1]:9000", "Sec-Fetch-Site": "same-origin"}
    assert web_client.post(targets["discover"], headers=ipv6).status_code == 200
    assert web_client.post(targets["discover"], headers={**ipv6, "Origin": "http://[::1]:9001"}).status_code == 403


def test_get_is_never_blocked_by_the_origin_guard(web_client) -> None:
    foreign = {"Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.example"}

    for path in ("/", "/api/stats", "/api/articles", "/api/topic-candidates", "/static/css/style.css"):
        assert web_client.get(path, headers=foreign).status_code == 200, path
    assert web_client.head("/static/css/style.css", headers=foreign).status_code == 200
    assert web_client.options("/api/stats", headers=foreign).status_code != 403  # 막는 것은 CORS 없는 라우터(405)이지 가드가 아니다


# ---------------------------------------------------------------------------------------------------------------
# W1.3 선택적 HTTP Basic 인증
# ---------------------------------------------------------------------------------------------------------------
PROTECTED_GET_PATHS = ["/", "/api/stats", "/api/articles", "/api/topic-candidates", "/static/css/style.css", "/static/js/main.js", "/docs", "/openapi.json"]


@pytest.mark.parametrize("path", PROTECTED_GET_PATHS)
def test_password_protects_every_route_including_static_and_docs(web_client, path) -> None:
    with configured(dashboard_password=PASSWORD):
        denied = web_client.get(path)
        allowed = web_client.get(path, headers=basic("admin", PASSWORD))

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == 'Basic realm="dashboard"'
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic",
        "Basic ",
        "Basic !!!not-base64!!!",
        "Bearer " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode(),
        "Basic " + base64.b64encode(b"admin").decode(),  # 콜론이 없다
        "Basic " + base64.b64encode(f"admin:{PASSWORD}x".encode()).decode(),
        "Basic " + base64.b64encode(f"admin:{PASSWORD[:-1]}".encode()).decode(),
        "Basic " + base64.b64encode(f"root:{PASSWORD}".encode()).decode(),
        "Basic " + base64.b64encode(f"ADMIN:{PASSWORD}".encode()).decode(),
        "Basic " + base64.b64encode(f"admin:{PASSWORD.upper()}".encode()).decode(),
        "Basic " + base64.b64encode(b"\xff\xfe:\xff").decode(),  # UTF-8이 아니다
        "Basic " + base64.b64encode(b":").decode(),
    ],
)
def test_wrong_or_malformed_credentials_get_401(web_client, authorization) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    with configured(dashboard_password=PASSWORD):
        response = web_client.get("/api/stats", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="dashboard"'


def test_correct_credentials_work_with_colon_non_ascii_and_a_lowercase_scheme(web_client) -> None:
    with configured(dashboard_password=PASSWORD, dashboard_user="운영자"):
        assert web_client.get("/api/stats", headers=basic("운영자", PASSWORD)).status_code == 200
        lowercase = {"Authorization": basic("운영자", PASSWORD)["Authorization"].replace("Basic", "basic")}
        assert web_client.get("/api/stats", headers=lowercase).status_code == 200
        assert web_client.get("/api/stats", headers=basic("admin", PASSWORD)).status_code == 401


def test_without_a_password_nothing_requires_authentication(web_client) -> None:
    with configured(dashboard_password=""):
        assert web_client.get("/api/stats").status_code == 200
        assert web_client.get("/api/stats", headers={"Authorization": "Basic garbage"}).status_code == 200


def test_credentials_are_compared_in_constant_time(web_client) -> None:
    with configured(dashboard_password=PASSWORD), patch("app.web.app.secrets.compare_digest", wraps=secrets.compare_digest) as compare:
        assert web_client.get("/api/stats", headers=basic("틀린 아이디", "틀린 비밀번호")).status_code == 401

    assert compare.call_count == 2  # 아이디가 틀려도 비밀번호까지 비교한다(앞쪽 실패로 건너뛰지 않는다)


def test_authentication_applies_to_posts_and_runs_nothing_when_denied(web_client, paid_calls, targets) -> None:
    with configured(dashboard_password=PASSWORD):
        for path in targets.values():
            assert web_client.post(path).status_code == 401
        assert web_client.delete("/api/topic-candidates/1").status_code == 401
        assert_no_paid_calls(paid_calls)
        assert web_client.post(targets["run-worker"], headers=basic("admin", PASSWORD)).status_code == 200


def test_origin_guard_stays_active_with_valid_credentials_because_browsers_send_them_cross_site(web_client, paid_calls, targets) -> None:
    credentials = basic("admin", PASSWORD)
    with configured(dashboard_password=PASSWORD):
        for endpoint, path in targets.items():
            blocked = web_client.post(path, headers={**credentials, "Sec-Fetch-Site": "cross-site"})
            assert blocked.status_code == 403, endpoint
            foreign = web_client.post(path, headers={**credentials, "Origin": "https://attacker.example"})
            assert foreign.status_code == 403, endpoint
        assert_no_paid_calls(paid_calls)

        # 인증 창이 다른 사이트의 요청으로 뜨지 않도록, 교차 출처 요청은 인증보다 먼저 거부한다
        unauthenticated = web_client.post(targets["run-worker"], headers={"Sec-Fetch-Site": "cross-site"})
        assert unauthenticated.status_code == 403 and "www-authenticate" not in unauthenticated.headers


def test_host_check_runs_before_authentication(web_client) -> None:
    with configured(dashboard_password=PASSWORD):
        assert web_client.get("/api/stats", headers={"Host": "rebind.attacker.example"}).status_code == 400
        assert web_client.get("/api/stats", headers={**basic("admin", PASSWORD), "Host": "rebind.attacker.example"}).status_code == 400


# ---------------------------------------------------------------------------------------------------------------
# W1.4 보안 헤더
# ---------------------------------------------------------------------------------------------------------------
def assert_security_headers(response) -> None:
    policy = parse_csp(response.headers["content-security-policy"])
    assert policy["default-src"] == ["'self'"]
    assert policy["script-src"] == ["'self'"]  # 인라인·eval·외부 스크립트 모두 불허
    assert policy["connect-src"] == ["'self'"]
    assert policy["frame-ancestors"] == ["'none'"]
    assert policy["base-uri"] == ["'none'"]
    assert policy["form-action"] == ["'self'"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("path", ["/", "/api/stats", "/api/articles", "/static/css/style.css", "/static/js/main.js", "/nope", "/docs"])
def test_every_response_carries_the_security_headers(web_client, path) -> None:
    assert_security_headers(web_client.get(path))


def test_rejection_responses_carry_the_security_headers_too(web_client, paid_calls, targets) -> None:
    assert_security_headers(web_client.get("/api/stats", headers={"Host": "attacker.example"}))  # 400
    assert_security_headers(web_client.post(targets["run-worker"], headers={"Sec-Fetch-Site": "cross-site"}))  # 403
    with configured(dashboard_password=PASSWORD):
        assert_security_headers(web_client.get("/api/stats"))  # 401


def test_static_scripts_and_styles_are_served_with_types_that_survive_nosniff(web_client) -> None:
    """nosniff와 함께면 브라우저는 text/plain인 main.js를 실행하지 않는다(Windows 레지스트리가 .js를 text/plain으로 매핑하는 PC에서 실제로 대시보드가 멈췄다)."""
    script = web_client.get("/static/js/main.js")
    style = web_client.get("/static/css/style.css")

    assert script.headers["x-content-type-options"] == "nosniff"
    assert script.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript"}
    assert style.headers["content-type"].split(";")[0] == "text/css"
    assert web_client.get("/").headers["content-type"].startswith("text/html")


def test_script_and_style_mime_types_are_pinned_at_import_whatever_the_os_registry_says() -> None:
    code = (
        "import mimetypes\n"
        "mimetypes.add_type('text/plain', '.js'); mimetypes.add_type('text/plain', '.css')  # 레지스트리가 잘못된 PC를 흉내 낸다\n"
        "import app.web.app\n"
        "print(mimetypes.guess_type('main.js')[0], mimetypes.guess_type('style.css')[0])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["text/javascript", "text/css"]


def test_a_downstream_csp_cannot_weaken_the_policy_or_be_duplicated() -> None:
    async def downstream(scope, receive, send) -> None:
        headers = [(b"content-security-policy", b"default-src *"), (b"x-frame-options", b"SAMEORIGIN"), (b"x-other", b"kept")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = web_module.DashboardGuardMiddleware(downstream, get_settings=lambda: Settings(_env_file=None))
    response = TestClient(guarded).get("/")

    assert response.text == "ok" and response.headers["x-other"] == "kept"
    assert response.headers.get_list("content-security-policy") == [web_module.CONTENT_SECURITY_POLICY]
    assert response.headers.get_list("x-frame-options") == ["DENY"]


def test_csp_allows_everything_the_dashboard_template_loads(web_client) -> None:
    html = web_client.get("/").text
    csp = web_client.get("/").headers["content-security-policy"]
    policy = parse_csp(csp)

    origins = set(re.findall(r"https://[a-z0-9.-]+", html))
    assert "https://fonts.googleapis.com" in origins and "https://fonts.gstatic.com" in origins  # 템플릿이 쓰는 외부 자원
    for origin in origins:
        assert origin in csp, f"{origin}을(를) 템플릿이 쓰는데 CSP가 허용하지 않는다"
    assert "https://fonts.googleapis.com" in policy["style-src"] and "'unsafe-inline'" in policy["style-src"]  # JS가 style= 속성을 쓴다
    assert policy["font-src"] == ["https://fonts.gstatic.com"]

    parsed = tags_of(html)
    assert not [attrs for tag, attrs in parsed if tag == "script" and not attrs.get("src")], "인라인 <script>는 script-src 'self'에서 막힌다"
    assert not [name for _, attrs in parsed for name in attrs if name.startswith("on")], "인라인 이벤트 핸들러는 script-src 'self'에서 막힌다"
    assert not re.search(r"\son(?:click|change|submit|error|load|mouse\w+)\s*=", MAIN_JS.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------
# W2 미리보기 XSS: sandbox iframe, result_url
# ---------------------------------------------------------------------------------------------------------------
def test_preview_is_rendered_in_a_sandboxed_iframe_without_scripts_or_same_origin(web_client) -> None:
    html = web_client.get("/").text
    frames = [attrs for tag, attrs in tags_of(html) if tag == "iframe"]

    assert [frame.get("id") for frame in frames] == ["modal-html-content"]
    frame = frames[0]
    assert "sandbox" in frame, "sandbox 속성이 없으면 모든 권한이 열린다"
    tokens = (frame["sandbox"] or "").split()
    assert not {token for token in tokens if token.startswith("allow-")}, "미리보기에는 어떤 allow-* 도 필요 없다"
    assert "allow-scripts" not in tokens and "allow-same-origin" not in tokens
    assert frame.get("referrerpolicy") == "no-referrer"
    assert not [attrs for tag, attrs in tags_of(html) if tag == "div" and attrs.get("id") == "modal-html-content"]


def test_main_js_no_longer_writes_model_html_into_the_dashboard_document() -> None:
    script = MAIN_JS.read_text(encoding="utf-8")

    assert not re.search(r"innerHTML\s*=\s*data\.body_html", script)
    assert not re.search(r"innerHTML[^;\n]*body_html", script)
    assert not re.search(r"(?:outerHTML|insertAdjacentHTML|document\.write)[^;\n]*body_html", script)
    assert re.search(r"modalHtmlContent\.srcdoc\s*=\s*buildPreviewDocument\(data\.body_html\)", script)
    assert "<base" not in script, "CSP base-uri 'none'이므로 <base>는 쓰지 않는다"


def run_js_helpers(names: list[str], expression: str):
    """main.js의 순수 함수만 뽑아 node로 실행한다(DOM이 필요 없는 것들). node가 없으면 건너뛴다."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node가 없어 JS 동작 테스트를 건너뜁니다")
    script = MAIN_JS.read_text(encoding="utf-8")
    functions = []
    for name in names:
        match = re.search(rf"^    function {name}\(.*?^    \}}\n", script, re.S | re.M)
        assert match, f"main.js에서 {name}()을 찾지 못했다"
        functions.append(match.group(0))
    program = "\n".join(functions) + f"\nconsole.log(JSON.stringify({expression}));\n"
    done = subprocess.run([node, "-e", program], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_stale_helpers_in_main_js_lock_only_the_candidates_that_are_really_generating() -> None:
    result = run_js_helpers(
        ["isGeneratingNow", "isRetryable", "candidateStatusLabel"],
        """[{status: 'GENERATING', stale: false}, {status: 'GENERATING', stale: true}, {status: 'GENERATING'}, {status: 'FAILED'},
            {status: 'NEW'}, {status: 'DRAFT_CREATED'}].map(c => [isGeneratingNow(c), isRetryable(c), candidateStatusLabel(c)])""",
    )

    assert result == [
        [True, False, "GENERATING"],  # 진행 중: 버튼 잠금
        [False, True, "생성 멈춤"],  # 멈춘 후보: FAILED처럼 다시 생성·삭제
        [True, False, "GENERATING"],  # stale 필드가 없는 응답은 예전처럼 진행 중으로 본다
        [False, True, "FAILED"],
        [False, False, "NEW"],
        [False, False, "글 생성 완료"],
    ]


def test_preview_document_wraps_the_body_without_adding_scripts_or_a_base_element() -> None:
    document, empty = run_js_helpers(
        ["buildPreviewDocument"], "[buildPreviewDocument('<p id=\"x\">본문</p>'), buildPreviewDocument(null)]"
    )

    assert document.startswith("<!DOCTYPE html>") and '<p id="x">본문</p>' in document and document.endswith("</body></html>")
    assert "<script" not in document and "<base" not in document  # 래퍼가 스스로 스크립트나 <base>를 넣지 않는다(CSP base-uri 'none')
    assert "null" not in empty and "undefined" not in empty


def test_link_helpers_in_main_js_only_let_http_urls_into_attributes() -> None:
    safe, bad, escaped = run_js_helpers(
        ["safeExternalUrl", "escapeHtml"],
        """[['https://blog.example/entry/1', 'http://a.example/?q=1'].map(safeExternalUrl),
            ['javascript:alert(1)', 'data:text/html,x', 'vbscript:x', '//evil', 'not a url', null, undefined, ''].map(safeExternalUrl),
            escapeHtml('a"b<c>&d')]""",
    )

    assert safe == ["https://blog.example/entry/1", "http://a.example/?q=1"]
    assert bad == [None] * 8
    assert escaped == "a&quot;b&lt;c&gt;&amp;d"


def test_main_js_asks_again_before_sending_a_forced_retry() -> None:
    script = MAIN_JS.read_text(encoding="utf-8")

    start = script.index("data.requires_force")
    guard = script[start : script.index("/retry?force=true", start)]
    assert re.search(r"if\s*\(\s*!\s*confirm\(.*\)\s*\)\s*return\s*;", guard), "중복 위험을 확인받지 않고 force=true를 보낸다"
    assert script.count("/retry?force=true") == 1, "force는 서버가 requires_force로 요구한 뒤에만 보낸다"


def test_main_js_only_renders_validated_result_urls() -> None:
    script = MAIN_JS.read_text(encoding="utf-8")

    assert 'href="${item.result_url}"' not in script
    assert re.search(r"safeExternalUrl\(item\.result_url\)", script)
    assert re.search(r'href="\$\{escapeHtml\(resultUrl\)\}"', script)


# ---------------------------------------------------------------------------------------------------------------
# P3 웹: 재등록(retry)·삭제 가드
# ---------------------------------------------------------------------------------------------------------------
def test_retry_of_a_verified_article_is_refused_with_a_korean_reason(web_client, tmp_path) -> None:
    article = add_article(tmp_path, "이미 발행된 글", article=ArticleStatus.VERIFIED, publish=PublishStatus.VERIFIED, job=JobStatus.SUCCEEDED)

    response = web_client.post(f"/api/articles/{article.article_id}/retry")

    assert response.status_code == 409
    assert "이미 발행이 확인된" in response.json()["detail"] and "중복" in response.json()["detail"]
    assert response.json()["requires_force"] is True
    assert article_state(article) == (ArticleStatus.VERIFIED, PublishStatus.VERIFIED, JobStatus.SUCCEEDED)


@pytest.mark.parametrize(
    "states",
    [
        {"article": ArticleStatus.VERIFIED, "version": ArticleStatus.READY_TO_PUBLISH},  # 글만 VERIFIED
        {"version": ArticleStatus.VERIFIED},  # 버전만 VERIFIED
        {"publish": PublishStatus.VERIFIED},  # 발행 작업만 VERIFIED
    ],
    ids=["article-verified", "version-verified", "publish-job-verified"],
)
def test_any_verified_marker_blocks_the_retry(web_client, tmp_path, states) -> None:
    article = add_article(tmp_path, "일부만 VERIFIED인 글", job=JobStatus.SUCCEEDED, **states)

    assert web_client.post(f"/api/articles/{article.article_id}/retry").status_code == 409


def test_retry_of_a_running_publish_job_is_refused(web_client, tmp_path) -> None:
    article = add_article(tmp_path, "지금 발행 중인 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISHING, job=JobStatus.RUNNING)

    response = web_client.post(f"/api/articles/{article.article_id}/retry")

    assert response.status_code == 409 and "실행 중" in response.json()["detail"]
    assert article_state(article) == (ArticleStatus.PUBLISHING, PublishStatus.PUBLISHING, JobStatus.RUNNING)


def test_a_running_queue_job_alone_is_enough_to_refuse_the_retry(web_client, tmp_path) -> None:
    article = add_article(tmp_path, "큐 작업만 RUNNING", job=JobStatus.RUNNING)

    assert web_client.post(f"/api/articles/{article.article_id}/retry").status_code == 409


def test_retry_when_the_latest_publish_job_is_unverified_is_refused(web_client, tmp_path) -> None:
    article = add_article(
        tmp_path, "발행 결과를 확인하지 못한 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISH_UNVERIFIED, job=JobStatus.FAILED, error_code="PUBLISH_UNVERIFIED"
    )

    response = web_client.post(f"/api/articles/{article.article_id}/retry")

    assert response.status_code == 409 and "확인하지 못한" in response.json()["detail"]
    assert article_state(article) == (ArticleStatus.PUBLISHING, PublishStatus.PUBLISH_UNVERIFIED, JobStatus.FAILED)


def test_only_the_latest_publish_job_decides_whether_a_post_may_exist(web_client, tmp_path) -> None:
    article = add_article(tmp_path, "예전 시도만 미확인인 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISH_UNVERIFIED, job=JobStatus.FAILED)
    with web_module.session_factory() as session:
        newer = PublishJob(
            article_version_id=article.article_version_id,
            target_blog_name="blog",
            category="생활꿀팁",
            visibility="PRIVATE",
            status=PublishStatus.FAILED,
            created_at=utc_now() + timedelta(minutes=5),
        )
        session.add(newer)
        session.flush()
        session.add(Job(job_type="PUBLISH_TISTORY", entity_id=newer.id, status=JobStatus.FAILED, max_attempts=2))
        session.commit()

    assert web_client.post(f"/api/articles/{article.article_id}/retry").status_code == 200


@pytest.mark.parametrize(
    "states",
    [
        {"article": ArticleStatus.VERIFIED, "publish": PublishStatus.VERIFIED, "job": JobStatus.SUCCEEDED},
        {"article": ArticleStatus.PUBLISHING, "publish": PublishStatus.PUBLISHING, "job": JobStatus.RUNNING},
        {"article": ArticleStatus.PUBLISHING, "publish": PublishStatus.PUBLISH_UNVERIFIED, "job": JobStatus.FAILED},
    ],
    ids=["verified", "running", "unverified"],
)
def test_force_true_is_the_explicit_duplicate_acknowledgement(web_client, tmp_path, states) -> None:
    article = add_article(tmp_path, "강제로 다시 등록할 글", error_code="UNKNOWN", **states)

    assert web_client.post(f"/api/articles/{article.article_id}/retry").status_code == 409
    response = web_client.post(f"/api/articles/{article.article_id}/retry?force=true")

    assert response.status_code == 200 and response.json()["success"] is True
    assert article_state(article) == (ArticleStatus.READY_TO_PUBLISH, PublishStatus.PENDING, JobStatus.PENDING)
    with web_module.session_factory() as session:
        job = session.get(Job, article.job_id)
        assert job.attempt_count == 0 and job.last_error_code is None and job.last_error_message is None


def test_force_does_not_make_drafts_retryable_and_unknown_articles_stay_404(web_client, tmp_path) -> None:
    thumbnail = tmp_path / "draft.png"
    thumbnail.write_bytes(b"image")
    with web_module.session_factory() as session:
        draft = register_draft_article(
            session,
            DraftArticleInput(title="초안", body_html="<p>초안</p>", tags=["초안"], category="생활꿀팁", thumbnail_path=thumbnail),
        )
        session.commit()

    assert web_client.post(f"/api/articles/{draft.article_id}/retry?force=true").status_code == 409
    assert web_client.post("/api/articles/9999/retry?force=true").status_code == 404


def test_retry_of_failed_or_fresh_articles_works_exactly_as_before(web_client, tmp_path) -> None:
    fresh = add_article(tmp_path, "새 글")
    failed = add_article(tmp_path, "실패한 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.UI_BROKEN, job=JobStatus.FAILED, error_code="UI_BROKEN")

    assert web_client.post(f"/api/articles/{fresh.article_id}/retry").status_code == 200
    response = web_client.post(f"/api/articles/{failed.article_id}/retry")

    assert response.status_code == 200
    assert article_state(failed) == (ArticleStatus.READY_TO_PUBLISH, PublishStatus.PENDING, JobStatus.PENDING)
    with web_module.session_factory() as session:
        job = session.get(Job, failed.job_id)
        assert job.attempt_count == 0 and job.last_error_code is None


# ---------------------------------------------------------------------------------------------------------------
# 2차 독립 리뷰 반영: 글 주소가 남아 있으면 재등록 경고, force 재등록은 이전 주소를 이력으로 보존
# ---------------------------------------------------------------------------------------------------------------
KNOWN_POST_URL = "https://blog.tistory.com/101"


def record_post_url(article, url: str = KNOWN_POST_URL) -> None:
    with web_module.session_factory() as session:
        session.get(PublishJob, article.publish_job_id).result_url = url
        session.commit()


@pytest.mark.parametrize("status", [PublishStatus.FAILED, PublishStatus.UI_BROKEN, PublishStatus.PENDING])
def test_retry_is_refused_when_a_post_url_is_recorded_even_if_the_status_looks_retryable(web_client, tmp_path, status) -> None:
    """E8: 검증 단계의 날 오류가 FAILED로 기록되면 '다시 시도'가 경고 없이 열려 같은 글이 중복으로 올라갔다."""
    article = add_article(tmp_path, "글 주소가 기록된 실패 글", article=ArticleStatus.QUARANTINED, publish=status, job=JobStatus.FAILED, error_code="UNEXPECTED_ERROR")
    record_post_url(article)

    response = web_client.post(f"/api/articles/{article.article_id}/retry")

    assert response.status_code == 409 and response.json()["requires_force"] is True
    assert KNOWN_POST_URL in response.json()["detail"] and "중복" in response.json()["detail"]
    assert article_state(article) == (ArticleStatus.QUARANTINED, status, JobStatus.FAILED)


def test_force_retry_moves_the_previous_post_url_into_the_history(web_client, tmp_path) -> None:
    """W5: 재등록하면 다음 발행이 result_url을 덮어써 이전(공개일 수도 있는) 글의 주소가 어디에도 남지 않았다."""
    article = add_article(tmp_path, "강제 재등록 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISH_UNVERIFIED, job=JobStatus.FAILED)
    record_post_url(article)

    assert web_client.post(f"/api/articles/{article.article_id}/retry").status_code == 409
    response = web_client.post(f"/api/articles/{article.article_id}/retry?force=true")

    assert response.status_code == 200
    assert article_state(article) == (ArticleStatus.READY_TO_PUBLISH, PublishStatus.PENDING, JobStatus.PENDING)
    with web_module.session_factory() as session:
        publish = session.get(PublishJob, article.publish_job_id)
        assert publish.result_url is None  # 워커의 중복 방지 검사를 통과하도록 비운다
        [entry] = publish.verification_details["previous_post_urls"]
        assert entry["url"] == KNOWN_POST_URL and entry["status"] == PublishStatus.PUBLISH_UNVERIFIED.value
        assert entry["replaced_at"]


def test_force_retry_appends_to_an_existing_history_and_keeps_other_details(web_client, tmp_path) -> None:
    article = add_article(tmp_path, "두 번 재등록한 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISH_UNVERIFIED, job=JobStatus.FAILED)
    with web_module.session_factory() as session:
        publish = session.get(PublishJob, article.publish_job_id)
        publish.result_url = "https://blog.tistory.com/202"
        publish.verification_details = {
            "evidence": "HTTP 404",
            "previous_post_urls": [{"url": KNOWN_POST_URL, "status": "PUBLISH_UNVERIFIED", "replaced_at": "2026-10-01T00:00:00+00:00"}],
        }
        session.commit()

    assert web_client.post(f"/api/articles/{article.article_id}/retry?force=true").status_code == 200

    with web_module.session_factory() as session:
        details = session.get(PublishJob, article.publish_job_id).verification_details
        assert details["evidence"] == "HTTP 404"
        assert [item["url"] for item in details["previous_post_urls"]] == [KNOWN_POST_URL, "https://blog.tistory.com/202"]


def test_a_retry_without_a_recorded_post_url_leaves_the_details_untouched(web_client, tmp_path) -> None:
    article = add_article(tmp_path, "주소 없는 실패 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.UI_BROKEN, job=JobStatus.FAILED, error_code="UI_BROKEN")
    with web_module.session_factory() as session:
        session.get(PublishJob, article.publish_job_id).verification_details = {"note": "그대로"}
        session.commit()

    assert web_client.post(f"/api/articles/{article.article_id}/retry").status_code == 200

    with web_module.session_factory() as session:
        publish = session.get(PublishJob, article.publish_job_id)
        assert publish.verification_details == {"note": "그대로"} and publish.result_url is None


def test_a_force_retried_publish_job_passes_the_workers_duplicate_guard(web_client, tmp_path) -> None:
    """웹의 force 재등록과 워커의 중복 방지가 맞물린다: 재등록한 발행 건은 다시 발행되고, 이전 주소는 이력에 남는다."""
    article = add_article(tmp_path, "맞물리는 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISH_UNVERIFIED, job=JobStatus.FAILED)
    record_post_url(article)
    assert web_client.post(f"/api/articles/{article.article_id}/retry?force=true").status_code == 200
    with web_module.session_factory() as session:
        publish = session.get(PublishJob, article.publish_job_id)

        PublisherWorker._refuse_if_possibly_published(publish)  # 예외가 없어야 한다


def test_delete_is_refused_while_a_related_job_is_running(web_client, tmp_path) -> None:
    running = add_article(tmp_path, "발행 중인 글", article=ArticleStatus.PUBLISHING, publish=PublishStatus.PUBLISHING, job=JobStatus.RUNNING)

    response = web_client.delete(f"/api/articles/{running.article_id}")

    assert response.status_code == 409 and "실행 중" in response.json()["detail"]
    assert web_client.delete(f"/api/articles/{running.article_id}?force=true").status_code == 409  # 삭제에는 force가 없다
    assert article_state(running) == (ArticleStatus.PUBLISHING, PublishStatus.PUBLISHING, JobStatus.RUNNING)
    assert web_client.get(f"/api/articles/{running.article_id}").status_code == 200


def test_delete_still_works_for_every_state_without_a_running_job(web_client, tmp_path) -> None:
    for title, article_status, publish_status, job_status in [
        ("대기 글", ArticleStatus.READY_TO_PUBLISH, PublishStatus.PENDING, JobStatus.PENDING),
        ("실패 글", ArticleStatus.PUBLISHING, PublishStatus.FAILED, JobStatus.FAILED),
        ("발행된 글", ArticleStatus.VERIFIED, PublishStatus.VERIFIED, JobStatus.SUCCEEDED),
    ]:
        article = add_article(tmp_path, title, article=article_status, publish=publish_status, job=job_status)
        assert web_client.delete(f"/api/articles/{article.article_id}").status_code == 200, title
        assert web_client.get(f"/api/articles/{article.article_id}").status_code == 404


# ---------------------------------------------------------------------------------------------------------------
# P3 웹: run-worker 결과 문구
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("outcome", "handled", "message"),
    [
        ("SUCCEEDED", True, "발행 작업 1건이 성공적으로 처리되었습니다."),
        ("FAILED", True, "발행 작업이 실패했습니다. 포스팅 목록의 실패 사유를 확인하세요."),
        ("DISABLED", False, "발행이 비활성화되어 있어 작업을 실행하지 않았습니다."),
        ("NONE", False, "처리할 대기 작업이 없습니다."),
        # 값이 문자열이 아니면(없음·MagicMock 등) 이전처럼 handled로만 판단한다
        (None, True, "발행 작업 1건이 성공적으로 처리되었습니다."),
        (None, False, "처리할 대기 작업이 없습니다."),
        (MagicMock(), False, "처리할 대기 작업이 없습니다."),
        (5, True, "발행 작업 1건이 성공적으로 처리되었습니다."),
        (["FAILED"], True, "발행 작업 1건이 성공적으로 처리되었습니다."),  # 해시할 수 없는 값이어도 500이 되면 안 된다
        ("알 수 없는 값", True, "발행 작업 1건이 성공적으로 처리되었습니다."),
    ],
    ids=["succeeded", "failed", "disabled", "none", "missing-handled", "missing-idle", "mock", "int", "list", "unknown"],
)
def test_run_worker_message_follows_the_workers_last_outcome(web_client, outcome, handled, message) -> None:
    with patch("app.web.app.PublisherWorker") as worker_class, patch("app.web.app.TistoryPublisher"):
        worker_class.return_value.run_once.return_value = handled
        worker_class.return_value.last_outcome = outcome

        response = web_client.post("/api/jobs/run-worker")

    assert response.status_code == 200
    assert response.json() == {"success": True, "handled": handled, "message": message}


def test_run_worker_without_a_last_outcome_attribute_falls_back_to_handled(web_client) -> None:
    class LegacyWorker:
        def __init__(self, *args) -> None:
            pass

        def run_once(self, worker_id: str) -> bool:
            return True

    with patch("app.web.app.PublisherWorker", LegacyWorker), patch("app.web.app.TistoryPublisher"):
        response = web_client.post("/api/jobs/run-worker")

    assert response.json()["message"] == "발행 작업 1건이 성공적으로 처리되었습니다."


# ---------------------------------------------------------------------------------------------------------------
# W3 stale GENERATING 복구
# ---------------------------------------------------------------------------------------------------------------
def long_ago() -> datetime:
    return utc_now() - web_module.TOPIC_GENERATION_STALE_AFTER - timedelta(seconds=30)


def test_candidates_report_whether_a_generating_row_is_stale(web_client) -> None:
    stuck = add_candidate(topic="멈춘 후보", status=TopicCandidateStatus.GENERATING, updated_at=long_ago())
    working = add_candidate(topic="진행 중 후보", status=TopicCandidateStatus.GENERATING, updated_at=utc_now())
    old_failed = add_candidate(topic="오래된 실패 후보", status=TopicCandidateStatus.FAILED, updated_at=long_ago())
    fresh = add_candidate(topic="새 후보")

    listed = {item["id"]: item for item in web_client.get("/api/topic-candidates").json()["candidates"]}

    assert listed[stuck]["stale"] is True and listed[stuck]["status"] == "GENERATING"
    assert listed[working]["stale"] is False
    assert listed[old_failed]["stale"] is False  # stale은 GENERATING에만 의미가 있다
    assert listed[fresh]["stale"] is False


def test_discover_responses_include_the_stale_flag(web_client, paid_calls) -> None:
    response = web_client.post("/api/topic-candidates/discover")

    assert [item["stale"] for item in response.json()["candidates"]] == [False]


def test_a_stale_generating_candidate_can_be_deleted_but_a_working_one_cannot(web_client) -> None:
    stuck = add_candidate(topic="삭제할 멈춘 후보", status=TopicCandidateStatus.GENERATING, updated_at=long_ago())
    working = add_candidate(topic="지우면 안 되는 진행 중 후보", status=TopicCandidateStatus.GENERATING, updated_at=utc_now())

    assert web_client.delete(f"/api/topic-candidates/{working}").status_code == 409
    assert web_client.delete(f"/api/topic-candidates/{stuck}").json() == {"success": True}

    remaining = [item["id"] for item in web_client.get("/api/topic-candidates").json()["candidates"]]
    assert remaining == [working]


def test_a_stale_generating_candidate_can_be_generated_again(web_client, paid_calls) -> None:
    stuck = add_candidate(status=TopicCandidateStatus.GENERATING, updated_at=long_ago())

    response = web_client.post(f"/api/topic-candidates/{stuck}/generate-article")

    assert response.status_code == 200
    assert web_client.get("/api/topic-candidates").json()["candidates"][0]["stale"] is False


def test_main_js_treats_a_stale_generating_candidate_like_a_failed_one() -> None:
    script = MAIN_JS.read_text(encoding="utf-8")

    assert "candidate.stale" in script
    assert script.count("} else if (isGeneratingNow(candidate)) {") == 2  # 목록과 상세 모달 모두
    assert script.count("const label = isRetryable(candidate) ?") == 2
    assert "} else if (candidate.status === 'GENERATING') {" not in script, "stale 여부를 보지 않고 버튼을 잠그는 분기가 남아 있다"


# ---------------------------------------------------------------------------------------------------------------
# W1.5 CLI server 바인딩 가드
# ---------------------------------------------------------------------------------------------------------------
def run_cli(*argv: str, password: str = ""):
    from app import cli  # 늦게 가져온다: cli가 끌어오는 다른 모듈의 import 오류가 이 파일의 나머지 테스트 수집까지 막지 않게

    logger = MagicMock(spec=logging.Logger)
    with (
        patch.object(sys, "argv", ["tistory-automation", *argv]),
        patch("app.cli.Settings", return_value=Settings(_env_file=None, dashboard_password=password)),
        patch("app.cli.configure_json_logging", return_value=logger),
        patch("app.cli.uvicorn.run") as serve,
    ):
        try:
            cli.main()
            code = None
        except SystemExit as exit_signal:
            code = exit_signal.code
    return SimpleNamespace(code=code, serve=serve, logger=logger)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "::", "my-pc.local", "127.0.0.2", ""])
def test_server_refuses_a_non_loopback_host_without_a_password(host, capsys) -> None:
    result = run_cli("server", "--host", host, password="")

    assert result.code == 2
    result.serve.assert_not_called()
    error = capsys.readouterr().err
    assert "DASHBOARD_PASSWORD" in error and "루프백" in error


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]"])
def test_server_starts_on_loopback_without_a_password(host, capsys) -> None:
    result = run_cli("server", "--host", host, "--port", "9100")

    assert result.code is None
    result.serve.assert_called_once()
    assert result.serve.call_args.kwargs["host"] == host and result.serve.call_args.kwargs["port"] == 9100
    result.logger.warning.assert_not_called()
    assert capsys.readouterr().err == ""


def test_server_default_host_is_loopback_and_needs_no_password() -> None:
    result = run_cli("server")

    assert result.code is None and result.serve.call_args.kwargs["host"] == "127.0.0.1"


def test_server_allows_a_remote_host_with_a_password_and_warns() -> None:
    result = run_cli("server", "--host", "0.0.0.0", password=PASSWORD)

    assert result.code is None
    result.serve.assert_called_once()
    warning = result.logger.warning.call_args.args[0]
    assert "dashboard_remote_access_enabled" in warning and "DASHBOARD_ALLOWED_HOSTS" in warning
    assert PASSWORD not in warning  # 비밀번호는 로그에 남기지 않는다
