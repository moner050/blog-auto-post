"""블로그 내부 링크(Topic Cluster / Silo) 자동 연계 모듈.

기발행된 고수익 포스트들의 URL과 제목을 조회하여 본문 하단에
'함께 읽으면 도움 되는 관련 추천 가이드' 형태로 주입한다.
"""

from __future__ import annotations

import html
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ArticleVersion, PublishJob, PublishStatus


def get_internal_links_for_category(
    session: Session,
    category: str | None,
    exclude_title: str | None = None,
    limit: int = 3,
) -> list[dict[str, str]]:
    """동일/유사 카테고리의 발행 완료(VERIFIED)된 포스트 링크 조회 (최대 limit개)."""
    if limit <= 0:
        return []

    query = (
        select(ArticleVersion.title, PublishJob.result_url, PublishJob.category)
        .join(PublishJob, PublishJob.article_version_id == ArticleVersion.id)
        .where(
            PublishJob.status == PublishStatus.VERIFIED,
            PublishJob.result_url.isnot(None),
            PublishJob.result_url != "",
        )
        .order_by(PublishJob.created_at.desc())
    )

    rows = session.execute(query).all()
    if not rows:
        return []

    same_category_links: list[dict[str, str]] = []
    other_category_links: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    clean_exclude = exclude_title.strip() if exclude_title else ""

    for title, url, post_category in rows:
        if not title or not url or url in seen_urls:
            continue
        if clean_exclude and title.strip() == clean_exclude:
            continue

        item = {"title": title.strip(), "url": url.strip()}
        seen_urls.add(url)

        if category and post_category == category:
            same_category_links.append(item)
        else:
            other_category_links.append(item)

    # 1순위: 동일 카테고리 글, 2순위: 다른 고수익 카테고리 글 보충
    selected = same_category_links[:limit]
    if len(selected) < limit:
        needed = limit - len(selected)
        selected.extend(other_category_links[:needed])

    return selected


def render_internal_links_block(links: list[dict[str, str]]) -> str:
    """내부 링크 목록을 blockquote 기반 카드 형태로 렌더링."""
    if not links:
        return ""

    items = "\n".join(
        f'    <li><a href="{html.escape(item["url"])}">{html.escape(item["title"])}</a></li>'
        for item in links
    )
    return (
        "<blockquote>\n"
        "  <strong>📌 함께 읽으면 도움 되는 관련 추천 가이드</strong>\n"
        f"  <ul>\n{items}\n  </ul>\n"
        "</blockquote>"
    )


def attach_internal_links_to_body(
    body_html: str,
    session: Session,
    category: str | None,
    exclude_title: str | None = None,
    limit: int = 3,
) -> str:
    """본문 HTML에 연관 내부 링크 블록을 결합하여 반환."""
    links = get_internal_links_for_category(
        session, category=category, exclude_title=exclude_title, limit=limit
    )
    block = render_internal_links_block(links)
    if not block:
        return body_html

    cleaned_body = body_html.rstrip()
    return f"{cleaned_body}\n\n{block}"
