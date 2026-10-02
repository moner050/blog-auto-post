from __future__ import annotations

import base64
from datetime import timedelta
import mimetypes
from pathlib import Path
import secrets
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.content.static import StaticArticleInput, register_private_article
from app.content.thumbnails import get_thumbnail_for_category
from app.content.internal_links import attach_internal_links_to_body
from app.core.settings import Settings
from app.db.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    Job,
    JobRun,
    JobStatus,
    MediaAsset,
    PublishJob,
    PublishStatus,
    TopicCandidate,
    TopicCandidateStatus,
    utc_now,
)
from app.db.session import create_session_factory
from app.jobs.worker import PublisherWorker
from app.llm.client import PerplexityClient
from app.llm.generator import ArticleGenerator
from app.publishing.tistory import TistoryPublisher
from app.topics.discovery import TopicDiscoverer

BASE_DIR = Path(__file__).resolve().parent

# Windows는 레지스트리의 확장자 연결을 MIME 매핑으로 쓴다(.js가 text/plain인 PC가 있다). 보안 헤더 nosniff와 함께면 브라우저가
# main.js 실행을 거부해 대시보드가 통째로 멈추므로, 정적 파일의 JS·CSS 타입은 OS 설정과 무관하게 고정한다.
for _extension, _media_type in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css")):
    mimetypes.add_type(_media_type, _extension)

app = FastAPI(title="Tistory Automation Admin Dashboard", version="0.1.0")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

settings = Settings()
session_factory = create_session_factory(settings.database_url)
# 생성 중 표시를 '멈춘 것'으로 보는 기준. LLM 호출 1회의 최대 대기(article_timeout_seconds)보다 길어야 하고,
# 호출 사이마다 진행 신호(_heartbeat)가 갱신되므로 재작성이 붙어도 정상 진행 중인 작업을 가로채지 않는다.
TOPIC_GENERATION_STALE_AFTER = timedelta(seconds=max(120.0, settings.article_timeout_seconds + 45.0))

# --- 대시보드 보호 (W1) ---------------------------------------------------------------------------------------
# 이 서버는 운영자 PC에서 유료 LLM 호출·글 삭제·Playwright 발행을 실행한다. 그래서 방문한 아무 웹 페이지가 요청을 보내는 것(CSRF)과
# 공격자 도메인이 127.0.0.1로 풀리게 해 응답을 읽는 것(DNS 리바인딩)을 막고, 비밀번호가 있으면 Basic 인증을 요구한다.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in _SECURITY_HEADERS)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})
_DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_port(value: str) -> bool:
    return value == "" or (value.isascii() and value.isdigit())


def _parse_host(value: str) -> str | None:
    """Host 헤더(또는 허용 목록 항목)에서 포트를 뗀 소문자 호스트를 돌려준다. 해석할 수 없으면 None.

    Starlette의 TrustedHostMiddleware는 버전에 따라 ':'로 잘라 '[::1]:9000'을 '['로 읽어 IPv6 루프백을 거부하므로 직접 해석한다.
    """
    value = value.strip().lower()
    if value.startswith("["):  # IPv6 리터럴: [::1] 또는 [::1]:9000
        host, closing, rest = value[1:].partition("]")
        if not closing or not host or (rest and not (rest.startswith(":") and _is_port(rest[1:]))):
            return None
        return host
    if value.count(":") > 1:  # 대괄호 없이 적은 IPv6 리터럴(::1). 이 경우 포트는 붙을 수 없다
        return value
    host, colon, port = value.partition(":")
    if not host or (colon and not _is_port(port)):
        return None
    return host


def _allowed_hosts(current: Settings) -> frozenset[str]:
    raw = getattr(current, "dashboard_allowed_hosts", "")
    items = raw.split(",") if isinstance(raw, str) else []
    hosts = frozenset(host for host in (_parse_host(item) for item in items if item.strip()) if host)
    return hosts or _DEFAULT_ALLOWED_HOSTS  # 비워 두면 루프백만 허용(전부 막아 대시보드를 못 쓰게 되는 것보다 안전한 기본값)


def _is_same_origin(headers: Headers) -> bool:
    """상태를 바꾸는 요청이 대시보드 자신의 페이지에서 왔는지 본다. 이 헤더들을 보내지 않는 클라이언트(curl 등)는 통과시킨다."""
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.strip().lower() not in _SAME_ORIGIN_FETCH_SITES:
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return origin_host == headers.get("host", "").strip().lower()  # 'Origin: null'은 빈 netloc이라 여기서 거부된다


def _basic_auth_ok(authorization: str | None, user: str, password: str) -> bool:
    scheme, _, token = (authorization or "").strip().partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        supplied = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except ValueError:  # binascii.Error와 UnicodeDecodeError는 모두 ValueError의 하위 클래스
        return False
    supplied_user, _, supplied_password = supplied.partition(":")
    # 아이디와 비밀번호를 모두 상수 시간으로 비교한 뒤에 합친다(앞쪽이 틀렸다고 건너뛰지 않는다).
    user_ok = secrets.compare_digest(supplied_user.encode("utf-8"), user.encode("utf-8"))
    password_ok = secrets.compare_digest(supplied_password.encode("utf-8"), password.encode("utf-8"))
    return user_ok and password_ok


class DashboardGuardMiddleware:
    """대시보드 보호용 순수 ASGI 미들웨어(가장 바깥에 둔다). 위에서부터 차례로 검사한다.

    1. Host가 허용 목록(DASHBOARD_ALLOWED_HOSTS)에 없으면 400 - DNS 리바인딩 차단
    2. 상태를 바꾸는 요청(GET/HEAD/OPTIONS 외)의 Sec-Fetch-Site·Origin이 다른 출처를 가리키면 403 - CSRF 차단.
       브라우저는 Basic 인증 정보를 다른 사이트의 요청에도 자동으로 붙이므로 인증보다 먼저, 인증을 켜도 항상 검사한다.
    3. DASHBOARD_PASSWORD가 있으면 모든 경로(API·정적 파일·문서)에 HTTP Basic 인증을 요구하고 틀리면 401
    통과하든 거부되든 모든 응답에 보안 헤더(CSP 등)를 붙인다. 설정은 요청마다 읽어, 테스트에서 settings를 바꿔 끼울 수 있다.
    """

    def __init__(self, app: ASGIApp, get_settings: Callable[[], Settings]) -> None:
        self.app = app
        self.get_settings = get_settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [(name, value) for name, value in message.get("headers", []) if name.lower() not in _SECURITY_HEADER_NAMES]
                message = {**message, "headers": [*kept, *_SECURITY_HEADERS]}
            await send(message)

        rejection = self._check(scope)
        if rejection is not None:
            await rejection(scope, receive, send_with_security_headers)
        else:
            await self.app(scope, receive, send_with_security_headers)

    def _check(self, scope: Scope) -> Response | None:
        current = self.get_settings()
        headers = Headers(scope=scope)
        host = _parse_host(headers.get("host", ""))
        if host is None or host not in _allowed_hosts(current):
            return JSONResponse(
                {"detail": "허용되지 않은 Host입니다. 이 주소로 접속하려면 접속에 쓰는 호스트 이름을 DASHBOARD_ALLOWED_HOSTS에 추가하세요."},
                status_code=400,
            )
        if scope["method"] not in _SAFE_METHODS and not _is_same_origin(headers):
            return JSONResponse({"detail": "다른 사이트에서 보낸 요청은 허용되지 않습니다."}, status_code=403)
        password = getattr(current, "dashboard_password", "")
        if isinstance(password, str) and password:
            user = str(getattr(current, "dashboard_user", ""))
            if not _basic_auth_ok(headers.get("authorization"), user, password):
                return JSONResponse(
                    {"detail": "인증이 필요합니다."},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
                )
        return None


app.add_middleware(DashboardGuardMiddleware, get_settings=lambda: settings)


class GenerateArticleRequest(BaseModel):
    topic: str
    category: str | None = None
    thumbnail_path: str | None = None


class DiscoverTopicCandidatesRequest(BaseModel):
    focus_sns: bool = False
    novelty: bool = False


def _candidate_response(candidate: TopicCandidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "batch_id": candidate.batch_id,
        "topic": candidate.topic,
        "category": candidate.category,
        "reason": candidate.reason,
        "sources": candidate.sources_json,
        "status": candidate.status.value,
        "article_id": candidate.article_id,
        "error_message": candidate.error_message,
        # 서버 재시작 등으로 생성이 멈춘 채 GENERATING으로 남은 후보. UI는 FAILED처럼 다시 생성·삭제할 수 있게 한다.
        "stale": candidate.status == TopicCandidateStatus.GENERATING and _generation_is_stale(candidate),
        "created_at": candidate.created_at.strftime("%Y-%m-%d %H:%M:%S") if candidate.created_at else "",
    }


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    """대시보드 메인 HTML 페이지 렌더링."""
    main_js_path = BASE_DIR / "static" / "js" / "main.js"
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"main_js_version": main_js_path.stat().st_mtime_ns},
    )


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    """대시보드 핵심 통계 데이터 반환."""
    with session_factory() as session:
        total_articles = session.scalar(select(func.count(Article.id))) or 0
        verified_articles = session.scalar(select(func.count(Article.id)).where(Article.status == ArticleStatus.VERIFIED)) or 0
        pending_jobs = session.scalar(select(func.count(Job.id)).where(Job.status == JobStatus.PENDING)) or 0
        failed_jobs = session.scalar(select(func.count(Job.id)).where(Job.status.in_([JobStatus.FAILED, JobStatus.RETRY_WAIT]))) or 0

        return {
            "total_articles": total_articles,
            "verified_articles": verified_articles,
            "pending_jobs": pending_jobs,
            "failed_jobs": failed_jobs,
            "blog_name": settings.tistory_expected_blog_name or "미지정",
            "allowed_category": settings.tistory_allowed_category or "기본 카테고리",
            "model": settings.article_model.strip() or settings.perplexity_model,
        }


@app.get("/api/articles")
def list_articles() -> list[dict[str, Any]]:
    """생성된 포스팅 목록 최신순 반환 (실패 사유 포함)."""
    with session_factory() as session:
        articles = session.scalars(select(Article).order_by(Article.created_at.desc())).all()
        result = []
        for article in articles:
            latest_version = session.scalar(
                select(ArticleVersion)
                .where(ArticleVersion.article_id == article.id)
                .order_by(ArticleVersion.version_number.desc())
            )
            publish_job = session.scalar(
                select(PublishJob)
                .where(PublishJob.article_version_id == latest_version.id)
                .order_by(PublishJob.created_at.desc())
            ) if latest_version else None

            job = session.scalar(
                select(Job)
                .where(Job.job_type == "PUBLISH_TISTORY", Job.entity_id == publish_job.id)
                .order_by(Job.created_at.desc())
            ) if publish_job else None

            result.append(
                {
                    "id": article.id,
                    "title": latest_version.title if latest_version else "제목 없음",
                    "status": article.status.value,
                    "category": latest_version.category if latest_version else "",
                    "tags": latest_version.tags_json if latest_version else [],
                    "thumbnail_path": latest_version.thumbnail_path if latest_version else "",
                    "created_at": article.created_at.strftime("%Y-%m-%d %H:%M:%S") if article.created_at else "",
                    "result_url": publish_job.result_url if publish_job else None,
                    "publish_status": publish_job.status.value if publish_job else "NONE",
                    "last_error_code": job.last_error_code if job else None,
                    "last_error_message": job.last_error_message if job else None,
                }
            )
        return result


@app.get("/api/articles/{article_id}")
def get_article_detail(article_id: int) -> dict[str, Any]:
    """단일 게시글 상세, HTML 본문 및 실패 사유 데이터 반환."""
    with session_factory() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")

        version = session.scalar(
            select(ArticleVersion)
            .where(ArticleVersion.article_id == article.id)
            .order_by(ArticleVersion.version_number.desc())
        )
        if not version:
            raise HTTPException(status_code=404, detail="게시글 버전을 찾을 수 없습니다.")

        publish_job = session.scalar(
            select(PublishJob)
            .where(PublishJob.article_version_id == version.id)
            .order_by(PublishJob.created_at.desc())
        )

        job = session.scalar(
            select(Job)
            .where(Job.job_type == "PUBLISH_TISTORY", Job.entity_id == publish_job.id)
            .order_by(Job.created_at.desc())
        ) if publish_job else None

        return {
            "id": article.id,
            "title": version.title,
            "body_html": version.body_html,
            "tags": version.tags_json,
            "category": version.category,
            "thumbnail_path": version.thumbnail_path,
            "status": article.status.value,
            "created_at": article.created_at.strftime("%Y-%m-%d %H:%M:%S") if article.created_at else "",
            "result_url": publish_job.result_url if publish_job else None,
            "last_error_code": job.last_error_code if job else None,
            "last_error_message": job.last_error_message if job else None,
        }


@app.get("/api/topic-candidates")
def list_topic_candidates() -> dict[str, Any]:
    """가장 최근 성공한 AI 주제 추천 배치를 반환."""
    with session_factory() as session:
        latest = session.scalar(
            select(TopicCandidate).order_by(TopicCandidate.created_at.desc(), TopicCandidate.id.desc())
        )
        if latest is None:
            return {"batch_id": None, "count": 0, "candidates": []}
        candidates = session.scalars(
            select(TopicCandidate)
            .where(TopicCandidate.batch_id == latest.batch_id)
            .order_by(TopicCandidate.id.asc())
        ).all()
        return {
            "batch_id": latest.batch_id,
            "count": len(candidates),
            "candidates": [_candidate_response(candidate) for candidate in candidates],
        }


@app.delete("/api/topic-candidates/{candidate_id}")
def delete_topic_candidate(candidate_id: int) -> dict[str, bool]:
    """추천 후보만 삭제하며 연결된 글과 발행 작업은 유지한다."""
    with session_factory() as session:
        candidate = session.get(TopicCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="삭제할 주제 후보를 찾을 수 없습니다.")
        if candidate.status == TopicCandidateStatus.GENERATING and not _generation_is_stale(candidate):
            raise HTTPException(status_code=409, detail="글 생성 중인 주제 후보는 삭제할 수 없습니다.")
        session.delete(candidate)
        session.commit()
    return {"success": True}


@app.post("/api/topic-candidates/discover")
def discover_topic_candidates(
    payload: DiscoverTopicCandidatesRequest | None = None,
) -> dict[str, Any]:
    """Sonar를 한 번 호출해 검증 가능한 주제 후보 배치를 저장."""
    focus_sns = payload.focus_sns if payload else False
    novelty = payload.novelty if payload else False

    existing_topics: list[str] = []
    with session_factory() as session:
        recent = session.scalars(
            select(TopicCandidate.topic)
            .order_by(TopicCandidate.id.desc())
            .limit(30)
        ).all()
        existing_topics = list(recent)

    try:
        discovered = TopicDiscoverer(PerplexityClient(settings)).discover(
            focus_sns=focus_sns,
            novelty=novelty,
            existing_topics=existing_topics,
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"주제 후보 수집 실패: {error}") from error

    batch_id = str(uuid4())
    with session_factory() as session:
        candidates = [
            TopicCandidate(
                batch_id=batch_id,
                topic=item.topic,
                topic_hash=item.topic_hash,
                category=item.category,
                reason=item.reason,
                sources_json=item.sources,
                status=TopicCandidateStatus.NEW,
            )
            for item in discovered
        ]
        session.add_all(candidates)
        session.commit()
        return {
            "batch_id": batch_id,
            "count": len(candidates),
            "candidates": [_candidate_response(candidate) for candidate in candidates],
        }


@app.post("/api/topic-candidates/{candidate_id}/generate-article")
def generate_topic_candidate_article(candidate_id: int) -> dict[str, Any]:
    """선택한 후보(주제·이유·출처·카테고리)로만 글을 생성해 비공개 발행 대기열에 등록."""
    with session_factory() as session:
        candidate = session.get(TopicCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="주제 후보를 찾을 수 없습니다.")
        if candidate.article_id is not None:
            return {
                "success": True,
                "article_id": candidate.article_id,
                "message": "이미 생성된 글입니다.",
            }
        topic = candidate.topic
        category = candidate.category
        reason = candidate.reason
        sources = list(candidate.sources_json or [])
        if candidate.status == TopicCandidateStatus.GENERATING and not _generation_is_stale(candidate):
            raise HTTPException(status_code=409, detail="이 주제는 현재 글을 생성 중입니다.")
        if candidate.status not in {TopicCandidateStatus.NEW, TopicCandidateStatus.FAILED, TopicCandidateStatus.GENERATING}:
            raise HTTPException(status_code=409, detail="이 주제는 글 생성 상태를 변경할 수 없습니다.")
        stale_cutoff = utc_now() - TOPIC_GENERATION_STALE_AFTER
        claimable = or_(
            TopicCandidate.status.in_([TopicCandidateStatus.NEW, TopicCandidateStatus.FAILED]),
            and_(
                TopicCandidate.status == TopicCandidateStatus.GENERATING,
                or_(TopicCandidate.updated_at.is_(None), TopicCandidate.updated_at <= stale_cutoff),
            ),
        )
        claimed = session.execute(
            update(TopicCandidate)
            .where(
                TopicCandidate.id == candidate_id,
                TopicCandidate.article_id.is_(None),
                claimable,
            )
            .values(
                status=TopicCandidateStatus.GENERATING,
                error_message=None,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            current = session.get(TopicCandidate, candidate_id)
            if current is not None and current.article_id is not None:
                return {
                    "success": True,
                    "article_id": current.article_id,
                    "message": "이미 생성된 글입니다.",
                }
            raise HTTPException(status_code=409, detail="이 주제는 현재 글을 생성 중입니다.")
        session.commit()

    try:
        generated = ArticleGenerator(settings).generate(
            topic,
            category=category,
            reason=reason,
            sources=sources,
            on_progress=_heartbeat(candidate_id),
        )
        thumbnail = get_thumbnail_for_category(category)
        with session_factory() as session:
            current = session.get(TopicCandidate, candidate_id)
            if current is None:
                raise ValueError("주제 후보가 삭제되었습니다.")
            if current.article_id is not None:
                return {
                    "success": True,
                    "article_id": current.article_id,
                    "message": "이미 생성된 글입니다.",
                }
            final_body_html = attach_internal_links_to_body(
                generated.body_html,
                session=session,
                category=current.category,
                exclude_title=generated.title,
            )
            registered = register_private_article(
                session,
                StaticArticleInput(
                    title=generated.title,
                    body_html=final_body_html,
                    tags=generated.tags,
                    category=current.category,
                    target_blog_name=settings.tistory_expected_blog_name,
                    thumbnail_path=thumbnail,
                ),
            )
            current.article_id = registered.article_id
            current.status = TopicCandidateStatus.DRAFT_CREATED
            current.error_message = None
            session.commit()
            return {
                "success": True,
                "article_id": registered.article_id,
                "message": "글이 비공개 발행 대기열에 등록되었습니다.",
                "warnings": generated.warnings,
            }
    except Exception as error:
        with session_factory() as session:
            current = session.get(TopicCandidate, candidate_id)
            if current is not None and current.article_id is None:
                current.status = TopicCandidateStatus.FAILED
                current.error_message = str(error)
                session.commit()
        raise HTTPException(status_code=502, detail=f"글 생성 실패: {error}") from error


def _heartbeat(candidate_id: int) -> Callable[[str], None]:
    """글 생성 단계(작성·재작성) 사이마다 updated_at을 갱신해, 진행 중인 작업이 stale로 오인되지 않게 한다."""

    def beat(stage: str) -> None:
        with session_factory() as session:
            session.execute(
                update(TopicCandidate)
                .where(
                    TopicCandidate.id == candidate_id,
                    TopicCandidate.status == TopicCandidateStatus.GENERATING,
                )
                .values(updated_at=utc_now())
                .execution_options(synchronize_session=False)
            )
            session.commit()

    return beat


def _generation_is_stale(candidate: TopicCandidate) -> bool:
    if candidate.updated_at is None:
        return True
    updated_at = candidate.updated_at
    now = utc_now()
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=now.tzinfo)
    return now - updated_at >= TOPIC_GENERATION_STALE_AFTER


def _publish_state(session: Session, article_id: int) -> tuple[list[PublishJob], list[Job]]:
    """글에 딸린 발행 작업(PublishJob, 최신순)과 큐 작업(Job) 전체."""
    publish_jobs = list(
        session.scalars(
            select(PublishJob)
            .join(ArticleVersion, PublishJob.article_version_id == ArticleVersion.id)
            .where(ArticleVersion.article_id == article_id)
            .order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
        )
    )
    if not publish_jobs:
        return publish_jobs, []
    jobs = list(
        session.scalars(
            select(Job).where(Job.job_type == "PUBLISH_TISTORY", Job.entity_id.in_([item.id for item in publish_jobs]))
        )
    )
    return publish_jobs, jobs


def _retry_conflict(
    article: Article, versions: list[ArticleVersion], publish_jobs: list[PublishJob], jobs: list[Job]
) -> str | None:
    """다시 등록하면 티스토리에 같은 글이 중복으로 올라갈 수 있는 상태면 그 이유를 돌려준다(없으면 None)."""
    if any(job.status == JobStatus.RUNNING for job in jobs) or any(
        item.status == PublishStatus.PUBLISHING for item in publish_jobs
    ):
        return "발행 작업이 실행 중이거나 도중에 멈춘 포스팅입니다. 다시 등록하면 같은 글이 티스토리에 중복으로 올라갈 수 있습니다."
    if publish_jobs and publish_jobs[0].status == PublishStatus.PUBLISH_UNVERIFIED:
        return (
            "발행 결과를 확인하지 못한 포스팅입니다. 티스토리에 이미 올라가 있을 수 있어 다시 등록하면 중복 글이 생길 수 있으니, "
            "티스토리 글 목록을 먼저 확인하세요."
        )
    if (
        article.status == ArticleStatus.VERIFIED
        or any(version.status == ArticleStatus.VERIFIED for version in versions)
        or any(item.status == PublishStatus.VERIFIED for item in publish_jobs)
    ):
        return "이미 발행이 확인된 포스팅입니다. 다시 등록하면 티스토리에 같은 글이 중복으로 올라갑니다."
    known_urls = [item.result_url for item in publish_jobs if item.result_url]
    if known_urls:
        # 글 주소가 기록돼 있다면 글이 이미 만들어진 것이다. 상태가 FAILED 등으로 보이더라도 다시 등록하면 중복 글이 생긴다.
        return (
            f"이미 티스토리에 글이 만들어진 기록(post URL: {known_urls[0]})이 있는 포스팅입니다. "
            "다시 등록하면 같은 글이 중복으로 올라갈 수 있으니, 티스토리 글 목록을 먼저 확인하세요."
        )
    return None


def _archive_previous_post_url(publish_job: PublishJob) -> None:
    """다시 등록하기 전에 이전 시도로 만들어진 글 주소를 verification_details 이력으로 옮기고 result_url을 비운다.

    워커는 result_url이 남아 있는 발행 건을 다시 발행하지 않는다(중복 방지). 이전 글(공개일 수도 있다)의 주소는 이력에 남겨
    사람이 티스토리 글 관리에서 찾아 정리할 수 있게 한다.
    """
    if not publish_job.result_url:
        return
    details = dict(publish_job.verification_details or {})
    history = list(details.get("previous_post_urls") or [])
    history.append(
        {
            "url": publish_job.result_url,
            "status": publish_job.status.value,
            "replaced_at": utc_now().isoformat(),
        }
    )
    details["previous_post_urls"] = history
    publish_job.verification_details = details  # 새 dict를 대입해야 JSON 컬럼의 변경이 감지된다
    publish_job.result_url = None


@app.delete("/api/articles/{article_id}")
def delete_article_endpoint(article_id: int) -> dict[str, Any]:
    """게시글 및 연관된 작업 큐 데이터 레코드 일체 삭제."""
    with session_factory() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="삭제할 게시글을 찾을 수 없습니다.")

        # 워커가 지금 이 글을 발행 중이면 행을 지우지 않는다(워커가 사라진 행을 갱신하지 못하고, 글은 이미 올라갈 수 있다).
        _, related_jobs = _publish_state(session, article.id)
        if any(job.status == JobStatus.RUNNING for job in related_jobs):
            raise HTTPException(status_code=409, detail="발행 작업이 실행 중인 포스팅은 삭제할 수 없습니다. 작업이 끝난 뒤 다시 시도하세요.")

        versions = session.scalars(select(ArticleVersion).where(ArticleVersion.article_id == article.id)).all()
        topic_candidates = session.scalars(
            select(TopicCandidate).where(TopicCandidate.article_id == article.id)
        ).all()
        for candidate in topic_candidates:
            candidate.article_id = None
            candidate.status = TopicCandidateStatus.NEW
            candidate.error_message = None
        for version in versions:
            media_assets = session.scalars(select(MediaAsset).where(MediaAsset.article_version_id == version.id)).all()
            for asset in media_assets:
                session.delete(asset)

            publish_jobs = session.scalars(select(PublishJob).where(PublishJob.article_version_id == version.id)).all()
            for pjob in publish_jobs:
                jobs = session.scalars(select(Job).where(Job.job_type == "PUBLISH_TISTORY", Job.entity_id == pjob.id)).all()
                for job in jobs:
                    runs = session.scalars(select(JobRun).where(JobRun.job_id == job.id)).all()
                    for run in runs:
                        session.delete(run)
                    session.delete(job)
                session.delete(pjob)

            session.delete(version)

        session.delete(article)
        session.commit()

        return {
            "success": True,
            "message": f"{article_id}번 포스팅 데이터가 성공적으로 삭제되었습니다.",
        }


@app.post("/api/articles/{article_id}/retry", response_model=None)
def retry_article_endpoint(article_id: int, force: bool = False) -> dict[str, Any] | JSONResponse:
    """실패/격리된 게시글을 다시 발행 대기(PENDING) 상태로 초기화.

    이미 발행이 확인됐거나(VERIFIED), 발행 작업이 실행 중이거나, 결과를 확인하지 못한(PUBLISH_UNVERIFIED) 글, 티스토리 글 주소가
    기록된 글은 다시 등록하면 티스토리에 중복 글이 생길 수 있어 409로 거절한다. 그 위험을 알고도 등록하려면 ?force=true를 붙이며,
    이때 이전 글 주소는 verification_details.previous_post_urls 이력으로 옮기고 result_url을 비운다(워커의 중복 방지 검사를 통과하도록).
    """
    with session_factory() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
        if article.status == ArticleStatus.DRAFT:
            raise HTTPException(status_code=409, detail="초안은 발행 재등록할 수 없습니다.")

        versions = session.scalars(select(ArticleVersion).where(ArticleVersion.article_id == article.id)).all()
        publish_jobs, related_jobs = _publish_state(session, article.id)
        conflict = _retry_conflict(article, list(versions), publish_jobs, related_jobs)
        if conflict and not force:
            return JSONResponse(status_code=409, content={"detail": conflict, "requires_force": True})

        article.status = ArticleStatus.READY_TO_PUBLISH

        for version in versions:
            version.status = ArticleStatus.READY_TO_PUBLISH

            publish_jobs = session.scalars(select(PublishJob).where(PublishJob.article_version_id == version.id)).all()
            for pjob in publish_jobs:
                _archive_previous_post_url(pjob)
                pjob.status = PublishStatus.PENDING

                jobs = session.scalars(select(Job).where(Job.job_type == "PUBLISH_TISTORY", Job.entity_id == pjob.id)).all()
                for job in jobs:
                    job.status = JobStatus.PENDING
                    job.attempt_count = 0
                    job.last_error_code = None
                    job.last_error_message = None
                    job.run_after = utc_now()

        session.commit()
        return {
            "success": True,
            "message": f"{article_id}번 포스팅이 다시 발행 대기 상태로 설정되었습니다.",
        }


@app.post("/api/articles/generate")
def generate_article_endpoint(payload: GenerateArticleRequest) -> dict[str, Any]:
    """주제를 입력받아 Perplexity LLM 포스팅 생성 및 DB 발행 큐 등록."""
    if not payload.topic.strip():
        raise HTTPException(status_code=400, detail="포스팅 주제(topic)를 입력해야 합니다.")

    try:
        target_category = payload.category or settings.tistory_allowed_category
        generator = ArticleGenerator(settings)
        generated = generator.generate(payload.topic.strip(), category=target_category)
        thumbnail = get_thumbnail_for_category(target_category, payload.thumbnail_path)

        with session_factory() as session:
            final_body_html = attach_internal_links_to_body(
                generated.body_html,
                session=session,
                category=target_category,
                exclude_title=generated.title,
            )
            registered = register_private_article(
                session,
                StaticArticleInput(
                    title=generated.title,
                    body_html=final_body_html,
                    tags=generated.tags,
                    category=target_category,
                    target_blog_name=settings.tistory_expected_blog_name,
                    thumbnail_path=thumbnail,
                ),
            )
            session.commit()
            return {
                "success": True,
                "message": "포스팅 생성이 완료되어 발행 큐에 등록되었습니다.",
                "article_id": registered.article_id,
                "job_id": registered.job_id,
                "title": generated.title,
                "thumbnail_path": str(thumbnail),
                "warnings": generated.warnings,
            }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"포스팅 생성 중 오류 발생: {error}") from error


_WORKER_OUTCOME_MESSAGES = {
    "SUCCEEDED": "발행 작업 1건이 성공적으로 처리되었습니다.",
    "FAILED": "발행 작업이 실패했습니다. 포스팅 목록의 실패 사유를 확인하세요.",
    "DISABLED": "발행이 비활성화되어 있어 작업을 실행하지 않았습니다.",
    "NONE": "처리할 대기 작업이 없습니다.",
}


def _worker_message(handled: bool, outcome: object) -> str:
    """run_once()는 실패한 작업도 True로 돌려주므로, 결과 문구는 워커의 last_outcome으로 정한다(없으면 handled로 판단)."""
    if isinstance(outcome, str) and outcome.strip().upper() in _WORKER_OUTCOME_MESSAGES:
        return _WORKER_OUTCOME_MESSAGES[outcome.strip().upper()]
    return _WORKER_OUTCOME_MESSAGES["SUCCEEDED"] if handled else _WORKER_OUTCOME_MESSAGES["NONE"]


@app.post("/api/jobs/run-worker")
def run_worker_endpoint() -> dict[str, Any]:
    """Playwright 티스토리 비공개 포스팅 발행 워커 1회 즉시 실행."""
    try:
        with session_factory() as session:
            worker = PublisherWorker(session, settings, TistoryPublisher(settings))
            handled = worker.run_once("web-dashboard")
            return {
                "success": True,
                "handled": handled,
                "message": _worker_message(handled, getattr(worker, "last_outcome", None)),
            }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"발행 워커 실행 오류: {error}") from error
