"""블로그 내부 링크(Topic Cluster / Silo) 자동 연계 모듈.

기발행된 고수익 포스트들의 URL과 제목을 조회하여 본문 끝(출처 목록 바로 앞)에
'함께 읽으면 도움 되는 관련 추천 가이드' 형태로 주입한다.

발행기는 모든 글을 비공개로 올리므로(VERIFIED = 비로그인 접근이 막혀 있음을 증명한 상태), DB 상태만으로는
독자가 열 수 있는 글인지 알 수 없다. 비공개 글로 가는 링크는 독자와 검색 로봇에게 404·로그인 화면을 보여 주는
깨진 내부 링크가 되므로, 기본값으로 비로그인 HTTP 요청에 200이 오는 글만 연결한다.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Callable, Iterable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import ArticleVersion, PublishJob, PublishStatus
from app.llm.sources import FOOTER_HEADING

_logger = logging.getLogger("tistory_automation")

INTERNAL_LINKS_HEADING = "📌 함께 읽으면 도움 되는 관련 추천 가이드"
# 공개 여부를 확인할 후보 수의 상한(limit의 배수). 글 1편을 만들 때 내부 링크 확인 요청이 무한히 늘지 않게 한다.
_CHECK_MULTIPLIER = 4
_PUBLIC_CHECK_TIMEOUT_SECONDS = 5.0
# 공개 여부 확인 전체에 쓰는 시간 상한. 글 생성 요청이 내부 링크 때문에 오래 멈추지 않게 한다.
_PUBLIC_CHECK_BUDGET_SECONDS = 15.0
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
_WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")

PublicChecker = Callable[[str], bool]


def is_publicly_reachable(url: str) -> bool:
    """비로그인 GET 요청에 리다이렉트 없이 200이 오면 공개 글로 본다. 오류·시간 초과는 비공개로 본다(링크를 넣지 않는 쪽이 안전)."""
    try:
        response = requests.get(
            url,
            timeout=_PUBLIC_CHECK_TIMEOUT_SECONDS,
            allow_redirects=False,
            headers={"User-Agent": _BROWSER_UA},
            stream=True,  # 본문은 읽지 않는다
        )
        response.close()
    except Exception:  # 네트워크 오류는 '공개를 증명하지 못함'이다
        return False
    return response.status_code == 200


def get_internal_links_for_category(
    session: Session,
    category: str | None,
    exclude_title: str | None = None,
    limit: int = 3,
    *,
    tags: Iterable[str] | None = None,
    is_public: PublicChecker | None = None,
) -> list[dict[str, str]]:
    """동일 카테고리의 발행 완료(VERIFIED) 포스트 링크를 관련도(태그·제목 키워드 겹침) → 최신순으로 최대 limit개 고른다.

    동일 카테고리 글이 부족하면 다른 카테고리 글로 채우되, 주제가 겹치는(관련도 > 0) 글만 쓴다.
    관계없는 글을 섞으면 토픽 클러스터 신호가 흐려지고 클릭도 일어나지 않는다.
    is_public을 주면 그 검사를 통과한 글만 쓴다.
    """
    if limit <= 0:
        return []

    query = (
        select(ArticleVersion.title, PublishJob.result_url, PublishJob.category, ArticleVersion.tags_json)
        .join(PublishJob, PublishJob.article_version_id == ArticleVersion.id)
        .where(
            PublishJob.status == PublishStatus.VERIFIED,
            PublishJob.result_url.isnot(None),
            PublishJob.result_url != "",
        )
        .order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
    )

    rows = session.execute(query).all()
    if not rows:
        return []

    keywords = _keywords([*(tags or ()), exclude_title or ""])
    clean_exclude = exclude_title.strip() if exclude_title else ""
    same: list[tuple[int, int, dict[str, str]]] = []
    other: list[tuple[int, int, dict[str, str]]] = []
    seen_urls: set[str] = set()

    for order, (title, url, post_category, post_tags) in enumerate(rows):
        url = (url or "").strip()
        if not title or not url or url in seen_urls:
            continue
        if clean_exclude and title.strip() == clean_exclude:
            continue
        seen_urls.add(url)
        relevance = len(keywords & _keywords([title, *(post_tags if isinstance(post_tags, list) else [])]))
        item = {"title": title.strip(), "url": url}
        if category and post_category == category:
            same.append((relevance, order, item))
        elif relevance > 0:
            other.append((relevance, order, item))

    # 관련도 높은 순, 같으면 최신순(order가 작을수록 최신)
    ranked = [item for _, _, item in sorted(same, key=lambda row: (-row[0], row[1]))]
    ranked += [item for _, _, item in sorted(other, key=lambda row: (-row[0], row[1]))]
    if is_public is None:
        return ranked[:limit]

    selected: list[dict[str, str]] = []
    deadline = time.monotonic() + _PUBLIC_CHECK_BUDGET_SECONDS
    for item in ranked[: limit * _CHECK_MULTIPLIER]:
        if time.monotonic() > deadline:
            log_event(_logger, "internal_links_public_check_budget_exceeded", selected=len(selected))
            break
        if is_public(item["url"]):
            selected.append(item)
            if len(selected) == limit:
                break
    return selected


def _keywords(texts: Iterable[str]) -> set[str]:
    return {word.lower() for text in texts if isinstance(text, str) for word in _WORD.findall(text)}


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
        f"  <strong>{INTERNAL_LINKS_HEADING}</strong>\n"
        f"  <ul>\n{items}\n  </ul>\n"
        "</blockquote>"
    )


# 생성기가 붙인 출처 목록(<h2>참고한 자료</h2>…) 또는 확인일 안내(<p>※ … 기준으로 …</p>)의 시작 위치
_FOOTER_SOURCES = re.compile(rf"<h2>{re.escape(FOOTER_HEADING)}</h2>")
_FOOTER_NOTICE = re.compile(r"<p>※ [^<]*기준으로[^<]*</p>\s*$")


def insert_before_footer(body_html: str, block: str) -> str:
    """block을 출처 목록·확인일 안내 바로 앞에 넣는다. 그런 꼬리가 없으면 본문 끝에 붙인다.

    추천 글이 출처 목록 뒤에 있으면 독자가 출처에서 읽기를 멈춰 클릭이 줄고, strip_footer가 꼬리를 찾지 못한다.
    """
    cleaned = body_html.rstrip()
    sources = list(_FOOTER_SOURCES.finditer(cleaned))  # 생성기가 붙인 목록은 마지막 것이다
    start = sources[-1].start() if sources else None
    if start is None and (notice := _FOOTER_NOTICE.search(cleaned)):
        start = notice.start()
    if start is None:
        return f"{cleaned}\n\n{block}"
    head, tail = cleaned[:start].rstrip(), cleaned[start:]
    return f"{head}\n\n{block}\n\n{tail}"


def attach_internal_links_to_body(
    body_html: str,
    session: Session,
    category: str | None,
    exclude_title: str | None = None,
    limit: int = 3,
    *,
    tags: Iterable[str] | None = None,
    require_public: bool = True,
    is_public: PublicChecker | None = None,
) -> str:
    """본문 HTML에 연관 내부 링크 블록을 결합하여 반환. 링크 조회가 실패해도 글 등록을 막지 않고 본문을 그대로 돌려준다."""
    checker = (is_public or is_publicly_reachable) if require_public else None
    try:
        links = get_internal_links_for_category(
            session, category=category, exclude_title=exclude_title, limit=limit, tags=tags, is_public=checker
        )
    except Exception as error:  # 내부 링크는 부가 기능이다
        log_event(_logger, "internal_links_failed", error=f"{type(error).__name__}: {error}")
        return body_html
    block = render_internal_links_block(links)
    if not block:
        return body_html
    return insert_before_footer(body_html, block)
