from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
import uvicorn

from app.content.static import StaticArticleInput, register_private_article
from app.content.thumbnails import get_thumbnail_for_category
from app.core.logging import configure_json_logging, log_event
from app.core.settings import Settings
from app.db.session import create_session_factory, ensure_sqlite_parent
from app.jobs.worker import PublisherWorker
from app.llm.generator import ArticleGenerator
from app.publishing.tistory import TistoryPublisher


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
    if args.command == "server":
        log_event(logger, "dashboard_server_starting", host=args.host, port=args.port, reload=args.reload)
        uvicorn.run("app.web.app:app", host=args.host, port=args.port, reload=args.reload)
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

        if args.command == "generate-article":
            generator = ArticleGenerator(settings)
            generated = generator.generate(args.topic)
            target_category = args.category or settings.tistory_allowed_category
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
            )
            return

        handled = PublisherWorker(session, settings, TistoryPublisher(settings)).run_once("publisher-cli")
        log_event(logger, "publisher_worker_finished", handled=handled)


if __name__ == "__main__":
    main()
