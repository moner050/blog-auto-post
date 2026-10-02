from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch

from alembic import command
from alembic.config import Config
import pytest

from app import cli
from app.content.static import StaticArticleInput, register_private_article
from app.core.settings import Settings
from app.db.session import create_session_factory
from app.llm.audit import audit_articles, format_report
from app.llm.generator import GeneratedArticle
from app.llm.sources import Source, render_footer
from tests.article_helpers import DEFAULT_TITLE, make_body

BAD_TITLE = "등본 발급 70% 달라졌다? 정부24에서 신청하는 방법 정리"


def test_audit_scores_stored_articles_and_reports_aggregates(mini_rules_path: Path) -> None:
    settings = Settings(_env_file=None, article_style_rules_path=mini_rules_path)
    bad_html = make_body(h2=2, paragraphs_per_h2=1, extra="<p>대박 무조건 대박</p>")

    rows = audit_articles(
        [
            (1, DEFAULT_TITLE, make_body(), ["가", "나", "다"], "정부지원·민원"),
            (2, BAD_TITLE, bad_html, ["가"], "정부지원·민원"),
        ],
        settings,
    )
    report = format_report(rows)

    assert rows[0].issues == [] and rows[1].errors >= 3
    assert "총 2편 / 오류가 있는 글 1편 (50%)" in report
    assert "[오류] title.number_unsupported: 1편" in report
    assert report.index("#2 |") < report.index("#1 |")  # 문제가 많은 글이 먼저
    assert "summary." not in report  # 저장된 글에는 요약문이 없어 채점에서 뺀다


def test_audit_does_not_count_the_generated_footer_as_part_of_the_article(mini_rules_path: Path) -> None:
    settings = Settings(_env_file=None, article_style_rules_path=mini_rules_path)
    # 소제목이 허용 최대(5개)이고 본문에 기준 시점이 없는 글. 푸터의 <h2>와 확인일 안내가 섞이면 결과가 달라진다.
    body = "".join(
        f"<h2>{icon}소제목 {n}</h2><p>{'내용을 자세히 설명해요. ' * 25}</p>"
        for n, icon in enumerate(["💡 ", "✅ ", "⚠️ ", "📌 ", ""], 1)
    ) + "<ol><li>하나</li></ol>"
    footer = render_footer([Source("정부24", "https://www.gov.kr/a")], date(2026, 10, 1), "lifestyle")
    title = "등본 발급 방법, 모르면 손해일까? 순서 정리해요"

    [row] = audit_articles([(1, title, f"{body}\n\n{footer}", ["가", "나", "다"], "정부지원·민원")], settings)

    assert row.h2 == 5
    assert [issue.code for issue in row.issues] == ["date.missing"]


def test_empty_audit_report() -> None:
    assert format_report([]) == "점검할 글이 없습니다."


def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings, *argv: str) -> str:
    monkeypatch.setattr(sys, "argv", ["tistory-automation", *argv])
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    cli.main()
    return capsys.readouterr().out


def test_show_prompt_prints_the_exact_prompts_without_calling_the_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mini_rules_path: Path
) -> None:
    settings = Settings(_env_file=None, perplexity_api_key="", article_style_rules_path=mini_rules_path)

    with patch("urllib.request.urlopen", side_effect=AssertionError("show-prompt must not call the API")):
        out = run_cli(monkeypatch, capsys, settings, "show-prompt", "--topic", "주민등록등본 발급 방법", "--category", "정부지원·민원")

    assert "# 모드=lifestyle 구조=how_to 제목스타일=clickbait" in out
    assert "===== SYSTEM (instructions) =====" in out and "미니 페르소나 지시문" in out
    assert "===== USER (input) =====\n다음 주제로" in out
    assert "[주제] 주민등록등본 발급 방법" in out and "[카테고리] 정부지원·민원" in out


def test_generate_article_dry_run_prints_the_article_and_never_touches_the_queue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mini_rules_path: Path
) -> None:
    settings = Settings(_env_file=None, article_style_rules_path=mini_rules_path, tistory_allowed_category="홈텍스")
    article = GeneratedArticle(
        title="드라이런 제목",
        body_html="<h2>소제목</h2><p>본문 문장입니다.</p>",
        tags=["가", "나"],
        summary="요약문",
        sources=[{"title": "정부24", "url": "https://www.gov.kr/a"}],
        warnings=["본문이 1,820자다."],
        mode="lifestyle",
        blueprint="how_to",
        revisions=1,
    )

    with patch("app.cli.ArticleGenerator") as generator_class, patch(
        "app.cli.create_session_factory", side_effect=AssertionError("dry-run must not open the database")
    ):
        generator_class.return_value.generate.return_value = article
        out = run_cli(monkeypatch, capsys, settings, "generate-article", "--topic", "주제", "--dry-run")

    assert generator_class.return_value.generate.call_args.kwargs == {"category": "홈텍스"}
    for expected in (
        "# 제목: 드라이런 제목",
        "# 태그: 가, 나",
        "# 요약(3자): 요약문",
        "재작성=1회 본문=",
        "#   - 본문이 1,820자다.",
        "#   - 정부24: https://www.gov.kr/a",
        "(dry-run: 발행 큐에 등록하지 않았습니다)",
        "<h2>소제목</h2>",
    ):
        assert expected in out, expected


def test_show_prompt_warns_when_style_rules_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    settings = Settings(_env_file=None, article_style_rules_path=tmp_path / "missing.yaml")

    out = run_cli(monkeypatch, capsys, settings, "show-prompt", "--topic", "주제")

    assert "# 주의: 스타일 규칙 파일을 찾을 수 없습니다" in out and "규칙파일=builtin" in out


def test_audit_articles_command_scores_the_stored_articles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, mini_rules_path: Path
) -> None:
    db_url = f"sqlite:///{tmp_path / 'cli.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(b"image")
    with create_session_factory(db_url)() as session:
        for title, body, tags, category in (
            (DEFAULT_TITLE, make_body(), ["가", "나", "다"], "정부지원·민원"),
            ("짧은 글", "<p>짧다</p>", ["가"], "생활꿀팁"),
        ):
            register_private_article(
                session,
                StaticArticleInput(
                    title=title,
                    body_html=body,
                    tags=tags,
                    category=category,
                    target_blog_name="blog",
                    thumbnail_path=thumbnail,
                ),
            )
        session.commit()
    settings = Settings(_env_file=None, database_url=db_url, article_style_rules_path=mini_rules_path)

    out = run_cli(monkeypatch, capsys, settings, "audit-articles")

    assert "총 2편 / 오류가 있는 글 1편 (50%)" in out
    assert "#2 | " in out and "짧은 글" in out
