from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update

from app.content.static import DraftArticleInput, StaticArticleInput, register_draft_article, register_private_article
from app.content.thumbnails import get_thumbnail_for_category
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

app = FastAPI(title="Tistory Automation Admin Dashboard", version="0.1.0")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

settings = Settings()
session_factory = create_session_factory(settings.database_url)
TOPIC_GENERATION_STALE_AFTER = timedelta(minutes=2)


class GenerateArticleRequest(BaseModel):
    topic: str
    category: str | None = None
    thumbnail_path: str | None = None


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
            "model": settings.perplexity_model,
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


@app.post("/api/topic-candidates/discover")
def discover_topic_candidates() -> dict[str, Any]:
    """Sonar를 한 번 호출해 검증 가능한 주제 후보 배치를 저장."""
    try:
        discovered = TopicDiscoverer(PerplexityClient(settings)).discover()
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


@app.post("/api/topic-candidates/{candidate_id}/generate-draft")
def generate_topic_candidate_draft(candidate_id: int) -> dict[str, Any]:
    """선택한 후보로만 Sonar 글을 생성해 DB 초안으로 저장."""
    with session_factory() as session:
        candidate = session.get(TopicCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="주제 후보를 찾을 수 없습니다.")
        if candidate.article_id is not None:
            return {
                "success": True,
                "article_id": candidate.article_id,
                "message": "이미 생성된 초안입니다.",
            }
        topic = candidate.topic
        category = candidate.category
        if candidate.status == TopicCandidateStatus.GENERATING and not _generation_is_stale(candidate):
            raise HTTPException(status_code=409, detail="이 주제는 현재 초안을 생성 중입니다.")
        if candidate.status not in {TopicCandidateStatus.NEW, TopicCandidateStatus.FAILED, TopicCandidateStatus.GENERATING}:
            raise HTTPException(status_code=409, detail="이 주제는 초안 생성 상태를 변경할 수 없습니다.")
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
                    "message": "이미 생성된 초안입니다.",
                }
            raise HTTPException(status_code=409, detail="이 주제는 현재 초안을 생성 중입니다.")
        session.commit()

    try:
        generated = ArticleGenerator(settings).generate(topic)
        thumbnail = get_thumbnail_for_category(category)
        with session_factory() as session:
            current = session.get(TopicCandidate, candidate_id)
            if current is None:
                raise ValueError("주제 후보가 삭제되었습니다.")
            if current.article_id is not None:
                return {
                    "success": True,
                    "article_id": current.article_id,
                    "message": "이미 생성된 초안입니다.",
                }
            registered = register_draft_article(
                session,
                DraftArticleInput(
                    title=generated.title,
                    body_html=generated.body_html,
                    tags=generated.tags,
                    category=current.category,
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
                "message": "초안이 DB에 저장되었습니다.",
            }
    except Exception as error:
        with session_factory() as session:
            current = session.get(TopicCandidate, candidate_id)
            if current is not None and current.article_id is None:
                current.status = TopicCandidateStatus.FAILED
                current.error_message = str(error)
                session.commit()
        raise HTTPException(status_code=502, detail=f"초안 생성 실패: {error}") from error


def _generation_is_stale(candidate: TopicCandidate) -> bool:
    if candidate.updated_at is None:
        return True
    updated_at = candidate.updated_at
    now = utc_now()
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=now.tzinfo)
    return now - updated_at >= TOPIC_GENERATION_STALE_AFTER


@app.delete("/api/articles/{article_id}")
def delete_article_endpoint(article_id: int) -> dict[str, Any]:
    """게시글 및 연관된 작업 큐 데이터 레코드 일체 삭제."""
    with session_factory() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="삭제할 게시글을 찾을 수 없습니다.")

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


@app.post("/api/articles/{article_id}/retry")
def retry_article_endpoint(article_id: int) -> dict[str, Any]:
    """실패/격리된 게시글을 다시 발행 대기(PENDING) 상태로 초기화."""
    with session_factory() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
        if article.status == ArticleStatus.DRAFT:
            raise HTTPException(status_code=409, detail="초안은 발행 재등록할 수 없습니다.")

        article.status = ArticleStatus.READY_TO_PUBLISH

        versions = session.scalars(select(ArticleVersion).where(ArticleVersion.article_id == article.id)).all()
        for version in versions:
            version.status = ArticleStatus.READY_TO_PUBLISH

            publish_jobs = session.scalars(select(PublishJob).where(PublishJob.article_version_id == version.id)).all()
            for pjob in publish_jobs:
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
        generator = ArticleGenerator(settings)
        generated = generator.generate(payload.topic.strip())
        target_category = payload.category or settings.tistory_allowed_category
        thumbnail = get_thumbnail_for_category(target_category, payload.thumbnail_path)

        with session_factory() as session:
            registered = register_private_article(
                session,
                StaticArticleInput(
                    title=generated.title,
                    body_html=generated.body_html,
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
            }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"포스팅 생성 중 오류 발생: {error}") from error


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
                "message": "발행 작업 1건이 성공적으로 처리되었습니다." if handled else "처리할 대기 작업이 없습니다.",
            }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"발행 워커 실행 오류: {error}") from error
