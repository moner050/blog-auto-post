"""이미 저장된 글을 현재 스타일 규칙·검증 기준으로 채점해, 글쓰기 품질의 현재 수준을 숫자로 보여 준다."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from app.core.settings import Settings
from app.llm.sanitize import sanitize_html
from app.llm.sources import strip_footer
from app.llm.style import load_style_profile
from app.llm.validation import ERROR, Issue, score, validate_article

# 저장된 글에는 요약문이 없어 채점할 수 없다.
_SKIPPED_PREFIXES = ("summary.",)
_EXAMPLE_CHARS = 70
_WORST_LIMIT = 15


@dataclass(frozen=True)
class AuditRow:
    article_id: int
    title: str
    chars: int
    h2: int
    issues: list[Issue]

    @property
    def errors(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == ERROR)


def audit_articles(
    articles: Iterable[tuple[int, str, str, list[str], str]],
    settings: Settings,
) -> list[AuditRow]:
    """(글 ID, 제목, 본문 HTML, 태그, 카테고리) 목록을 채점한다."""
    rows: list[AuditRow] = []
    for article_id, title, body_html, tags, category in articles:
        profile = load_style_profile(
            settings.article_style_rules_path, title, category, title_style=settings.article_title_style
        )
        # 생성 단계가 글 끝에 붙인 '참고한 자료'·확인일 안내는 본문이 아니므로 빼고 센다(소제목 수·기준 시점 점검이 틀어진다).
        body = sanitize_html(strip_footer(body_html), title=title)
        issues = [
            issue
            for issue in validate_article(title=title, summary="", tags=list(tags), body=body, profile=profile)
            if not issue.code.startswith(_SKIPPED_PREFIXES)
        ]
        rows.append(AuditRow(article_id, title, len(body.text), body.h2_count, issues))
    return rows


def format_report(rows: list[AuditRow]) -> str:
    if not rows:
        return "점검할 글이 없습니다."
    with_errors = sum(1 for row in rows if row.errors)
    lines = [
        f"글 품질 점검: 총 {len(rows)}편 / 오류가 있는 글 {with_errors}편 ({with_errors / len(rows):.0%})",
        f"평균 본문 길이 {sum(row.chars for row in rows) // len(rows):,}자 / 평균 소제목 {sum(row.h2 for row in rows) / len(rows):.1f}개",
        "",
        "항목별 발생 글 수:",
    ]
    counts: Counter[str] = Counter()
    first: dict[str, Issue] = {}
    for row in rows:
        for code in {issue.code for issue in row.issues}:
            counts[code] += 1
        for issue in row.issues:
            first.setdefault(issue.code, issue)
    for code, count in sorted(counts.items(), key=lambda item: (first[item[0]].severity != ERROR, -item[1], item[0])):
        issue = first[code]
        label = "오류" if issue.severity == ERROR else "경고"
        lines.append(f"  [{label}] {code}: {count}편 - {issue.message[:_EXAMPLE_CHARS]}")
    if not counts:
        lines.append("  (없음)")
    lines += ["", f"문제가 많은 글 상위 {min(_WORST_LIMIT, len(rows))}편:"]
    for row in sorted(rows, key=lambda r: (-score(r.issues), r.article_id))[:_WORST_LIMIT]:
        warns = len(row.issues) - row.errors
        lines.append(f"  #{row.article_id} | {row.chars:,}자 | 소제목 {row.h2} | 오류 {row.errors} 경고 {warns} | {row.title[:40]}")
    return "\n".join(lines)
