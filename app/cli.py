from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import select
import uvicorn

from app.content.static import StaticArticleInput, register_private_article
from app.content.thumbnails import get_thumbnail_for_category
from app.core.logging import configure_json_logging, log_event
from app.core.settings import Settings
from app.db.models import ArticleVersion
from app.db.session import create_session_factory, ensure_sqlite_parent
from app.jobs.worker import PublisherWorker
from app.llm.audit import audit_articles, format_report
from app.llm.generator import ArticleGenerator, GeneratedArticle
from app.llm.sanitize import strip_tags
from app.publishing.tistory import TistoryPublisher


def _force_utf8_stdout() -> None:
    """Windows에서 출력을 파일로 돌리면 cp949로 잡혀 이모지에서 실패하므로 UTF-8로 맞춘다."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _check_dashboard_bind(host: str, settings: Settings, logger: logging.Logger) -> None:
    """루프백이 아닌 주소에 대시보드를 열려면 비밀번호(HTTP Basic)가 있어야 한다. 없으면 서버를 시작하지 않고 종료 코드 2로 끝낸다."""
    if host.strip().strip("[]").lower() in _LOOPBACK_HOSTS:
        return
    if not settings.dashboard_password:
        print(
            f"오류: 루프백(127.0.0.1, localhost, ::1)이 아닌 주소(--host {host})에 열면 같은 네트워크의 다른 기기도 대시보드에 접속할 수 있습니다. "
            "이 대시보드는 유료 LLM 호출·글 삭제·티스토리 발행을 실행하므로, .env에 DASHBOARD_PASSWORD를 먼저 설정해야 합니다. "
            "서버를 시작하지 않았습니다.",
            file=sys.stderr,
        )
        sys.exit(2)
    logger.warning(
        json.dumps(
            {
                "event": "dashboard_remote_access_enabled",
                "host": host,
                "dashboard_allowed_hosts": settings.dashboard_allowed_hosts,
                "message": "대시보드가 루프백이 아닌 주소에 열립니다(HTTP Basic 인증 사용). 접속에 쓰는 호스트 이름을 DASHBOARD_ALLOWED_HOSTS에 "
                "추가하세요. HTTP Basic은 암호화되지 않으므로 신뢰하는 네트워크나 TLS 프록시 뒤에서만 사용하세요.",
            },
            ensure_ascii=False,
        )
    )


def _format_dry_run(generated: GeneratedArticle) -> str:
    chars = len(re.sub(r"\s+", " ", strip_tags(generated.body_html, " ")).strip())
    lines = [
        f"# 제목: {generated.title}",
        f"# 태그: {', '.join(generated.tags)}",
        f"# 요약({len(generated.summary)}자): {generated.summary}",
        f"# 모드={generated.mode} 구조={generated.blueprint} 재작성={generated.revisions}회 본문={chars:,}자",
        "# 경고:" + ("" if generated.warnings else " 없음"),
        *(f"#   - {warning}" for warning in generated.warnings),
        "# 출처:" + ("" if generated.sources else " 없음"),
        *(f"#   - {source['title']}: {source['url']}" for source in generated.sources),
        "# (dry-run: 발행 큐에 등록하지 않았습니다)",
        "\n----- 본문 HTML -----",
        generated.body_html,
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="tistory-automation")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db")
    login_parser = subcommands.add_parser("login")
    login_parser.set_defaults(command="login")

    enqueue = subcommands.add_parser("enqueue-static")
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--html-file", type=Path, required=True)
    enqueue.add_argument("--thumbnail", type=Path, required=True)
    enqueue.add_argument("--tags", required=True, help="Comma-separated tags")
    enqueue.add_argument("--category")

    generate = subcommands.add_parser("generate-article")
    generate.add_argument("--topic", required=True, help="포스팅 주제 (예: 주민등록등본 PDF 저장 방법)")
    generate.add_argument("--thumbnail", type=Path, help="썸네일 이미지 경로")
    generate.add_argument("--category", help="티스토리 카테고리")
    generate.add_argument("--dry-run", action="store_true", help="글만 생성해 출력하고 발행 큐에는 등록하지 않음(품질 확인용)")

    show_prompt = subcommands.add_parser("show-prompt", help="글 생성에 쓰는 프롬프트를 API 호출 없이 그대로 출력")
    show_prompt.add_argument("--topic", required=True, help="포스팅 주제")
    show_prompt.add_argument("--category", help="카테고리 (예: 정부지원·민원)")

    subcommands.add_parser("audit-articles", help="저장된 글을 현재 스타일 규칙 기준으로 채점(개선 전후 비교용)")

    server_parser = subcommands.add_parser("server", help="관리자 웹 대시보드 서버 실행")
    server_parser.add_argument("--host", default="127.0.0.1", help="바인딩할 호스트 IP (기본값: 127.0.0.1)")
    server_parser.add_argument("--port", type=int, default=9000, help="포트 번호 (기본값: 9000)")
    server_parser.add_argument("--reload", action="store_true", help="코드 변경 시 자동 재로딩 모드 활성화")

    subcommands.add_parser("worker-once")
    args = parser.parse_args()
    settings = Settings()
    logger = configure_json_logging()

    if args.command == "init-db":
        ensure_sqlite_parent(settings.database_url)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(config, "head")
        log_event(logger, "database_initialized")
        return
    if args.command == "login":
        TistoryPublisher(settings).open_login()
        log_event(logger, "login_profile_updated")
        return
    if args.command == "show-prompt":
        _force_utf8_stdout()
        prepared = ArticleGenerator(settings).prepare(args.topic, category=args.category)
        profile = prepared.profile
        print(f"# 모드={profile.mode} 구조={profile.blueprint} 제목스타일={profile.title_style} 규칙파일={profile.source}")
        if profile.fallback_reason:
            print(f"# 주의: {profile.fallback_reason} (내장 기본 규칙 사용)")
        print(f"# system {len(prepared.system):,}자 / user {len(prepared.user):,}자")
        print("\n===== SYSTEM (instructions) =====\n" + prepared.system)
        print("\n===== USER (input) =====\n" + prepared.user)
        return
    if args.command == "server":
        _check_dashboard_bind(args.host, settings, logger)
        log_event(logger, "dashboard_server_starting", host=args.host, port=args.port, reload=args.reload)
        uvicorn.run("app.web.app:app", host=args.host, port=args.port, reload=args.reload)
        return

    if args.command == "generate-article" and args.dry_run:
        _force_utf8_stdout()
        generated = ArticleGenerator(settings).generate(
            args.topic, category=args.category or settings.tistory_allowed_category
        )
        print(_format_dry_run(generated))
        return

    factory = create_session_factory(settings.database_url)
    with factory() as session:
        if args.command == "enqueue-static":
            registered = register_private_article(
                session,
                StaticArticleInput(
                    title=args.title,
                    body_html=args.html_file.read_text(encoding="utf-8"),
                    tags=[tag.strip() for tag in args.tags.split(",") if tag.strip()],
                    category=args.category or settings.tistory_allowed_category,
                    target_blog_name=settings.tistory_expected_blog_name,
                    thumbnail_path=args.thumbnail,
                ),
            )
            session.commit()
            log_event(logger, "private_publish_enqueued", job_id=registered.job_id, article_id=registered.article_id)
            return

        if args.command == "audit-articles":
            _force_utf8_stdout()
            rows = list(session.execute(select(ArticleVersion).order_by(ArticleVersion.article_id, ArticleVersion.version_number)).scalars())
            latest = {version.article_id: version for version in rows}  # 글마다 마지막 버전만
            print(
                format_report(
                    audit_articles(
                        [(v.article_id, v.title, v.body_html, list(v.tags_json), v.category) for v in latest.values()],
                        settings,
                    )
                )
            )
            return

        if args.command == "generate-article":
            target_category = args.category or settings.tistory_allowed_category
            generator = ArticleGenerator(settings)
            generated = generator.generate(args.topic, category=target_category)
            thumbnail_path = get_thumbnail_for_category(target_category, args.thumbnail)

            registered = register_private_article(
                session,
                StaticArticleInput(
                    title=generated.title,
                    body_html=generated.body_html,
                    tags=generated.tags,
                    category=target_category,
                    target_blog_name=settings.tistory_expected_blog_name,
                    thumbnail_path=thumbnail_path,
                ),
            )
            session.commit()
            log_event(
                logger,
                "article_generated_and_enqueued",
                topic=args.topic,
                title=generated.title,
                job_id=registered.job_id,
                article_id=registered.article_id,
                thumbnail_path=str(thumbnail_path),
                mode=generated.mode,
                revisions=generated.revisions,
                warnings=generated.warnings,
            )
            return

        handled = PublisherWorker(session, settings, TistoryPublisher(settings)).run_once("publisher-cli")
        log_event(logger, "publisher_worker_finished", handled=handled)


if __name__ == "__main__":
    main()
