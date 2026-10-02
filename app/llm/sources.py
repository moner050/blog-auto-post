"""글에 쓸 수 있는 출처(검증된 URL)를 모으고, 본문 링크 허용 정책과 '참고한 자료' 푸터를 만든다.

모델이 기억에 의존해 만든 링크(존재하지 않는 세부 URL 등)가 본문에 남지 않도록,
검색 결과로 확인된 URL과 공식 기관 대표 주소만 링크로 허용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
import re
from typing import Any, Iterable
from urllib.parse import ParseResult, urlparse

from app.llm.style import TECHNICAL
from app.llm.textutil import strip_invisible

# 공식 기관으로 보는 호스트. or.kr은 누구나 등록할 수 있는 조직 도메인이라 통째로 믿지 않고, 자주 인용되는 공공기관만 적는다.
OFFICIAL_HOST_SUFFIXES = ("go.kr", "gov.kr", "nhis.or.kr", "nps.or.kr", "comwel.or.kr", "kinfa.or.kr", "khug.or.kr")
# 개인·커뮤니티가 올린 글은 근거 자료로 내세우지 않는다(주제 영감으로만 쓴다).
UGC_HOST_SUFFIXES = (
    "blog.naver.com",
    "cafe.naver.com",
    "tistory.com",
    "brunch.co.kr",
    "dcinside.com",
    "clien.net",
    "ppomppu.co.kr",
    "fmkorea.com",
    "ruliweb.com",
    "theqoo.net",
    "instiz.net",
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
)
_CONTROL_CHARS = re.compile(r"[\x00-\x20\x7f]+")
# 정상적인 URL에는 날것으로 나오지 않고, 속성 값을 깨고 나오는 데 쓰이는 문자
_URL_BREAKERS = re.compile(r"[\"<>`]")
_TITLE_LIMIT = 80
MAX_FOOTER_SOURCES = 6
FOOTER_HEADING = "참고한 자료"


@dataclass(frozen=True)
class Source:
    title: str
    url: str


def clean_url(raw: str) -> str | None:
    """http(s) 주소만 통과시킨다. 제어문자·공백 난독화, 사용자 정보, 역슬래시가 있으면 거부한다."""
    if not isinstance(raw, str):
        return None
    value = _CONTROL_CHARS.sub("", strip_invisible(raw))
    if not value or "\\" in value or _URL_BREAKERS.search(value):
        return None
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        parsed.port  # 범위를 벗어났거나 숫자가 아닌 포트는 여기서 ValueError가 난다
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not host or "@" in parsed.netloc:
        return None
    return value


def host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def normalize_url(url: str) -> str:
    """같은 문서의 주소를 같은 키로 비교하기 위한 정규화(스킴·www·프래그먼트·끝 슬래시 무시, 기본이 아닌 포트는 구분)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host_of(url)}{_port_suffix(parsed)}{path}{query}"


def _port_suffix(parsed: ParseResult) -> str:
    try:
        port = parsed.port
    except ValueError:
        return ":invalid"
    return "" if port in (None, 80, 443) else f":{port}"


def _host_matches(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def is_official_host(host: str) -> bool:
    return _host_matches(host, OFFICIAL_HOST_SUFFIXES)


def is_ugc_host(host: str) -> bool:
    return _host_matches(host, UGC_HOST_SUFFIXES)


def merge_sources(*groups: Iterable[Any]) -> list[Source]:
    """dict({title, url}) 또는 Source 목록을 합쳐 유효한 http(s) 출처만 중복 없이 반환한다."""
    merged: list[Source] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or ():
            url_raw = item.url if isinstance(item, Source) else (item.get("url") if isinstance(item, dict) else None)
            title_raw = item.title if isinstance(item, Source) else (item.get("title") if isinstance(item, dict) else "")
            url = clean_url(url_raw) if url_raw else None
            if url is None:
                continue
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            title = re.sub(r"\s+", " ", strip_invisible(title_raw)).strip() if isinstance(title_raw, str) else ""
            merged.append(Source(title=title[:_TITLE_LIMIT] or host_of(url), url=url))
    return merged


def usable_sources(sources: Iterable[Source]) -> list[Source]:
    """근거로 내세울 수 있는 출처(커뮤니티·SNS 제외)."""
    return [source for source in sources if not is_ugc_host(host_of(source.url))]


@dataclass(frozen=True)
class LinkPolicy:
    """본문 <a> 링크 허용 기준: 검증된 출처 URL이거나, 출처 사이트 또는 공식 기관의 대표(루트) 주소."""

    urls: frozenset[str] = frozenset()
    hosts: frozenset[str] = frozenset()

    @classmethod
    def from_sources(cls, sources: Iterable[Source]) -> LinkPolicy:
        items = list(sources)
        return cls(
            urls=frozenset(normalize_url(source.url) for source in items),
            hosts=frozenset(host_of(source.url) for source in items),
        )

    def allows(self, url: str) -> bool:
        cleaned = clean_url(url)
        if cleaned is None:
            return False
        if normalize_url(cleaned) in self.urls:
            return True
        parsed = urlparse(cleaned)
        is_root = parsed.path in ("", "/") and not parsed.query and _port_suffix(parsed) == ""
        host = host_of(cleaned)
        return is_root and (host in self.hosts or is_official_host(host))


def rank_sources(sources: Iterable[Source]) -> list[Source]:
    """독자에게 보일 출처 순서: 커뮤니티·SNS는 빼고, 공식 기관 자료를 앞에 둔다(그 안에서는 원래 순서 유지)."""
    return sorted(usable_sources(sources), key=lambda source: not is_official_host(host_of(source.url)))


def render_footer(sources: Iterable[Source], today: date, mode: str, max_items: int = MAX_FOOTER_SOURCES) -> str:
    """'참고한 자료' 목록과 확인일 안내를 만든다. 공식 기관 자료를 앞에 둔다."""
    ranked = rank_sources(sources)
    stamp = f"{today.year}년 {today.month}월 {today.day}일"
    if mode == TECHNICAL:
        notice = f"※ {stamp} 기준으로 정리한 내용이다. 버전과 설정에 따라 달라질 수 있으므로 공식 문서를 함께 확인한다."
    else:
        notice = f"※ {stamp} 기준으로 확인한 내용이에요. 제도와 조건은 바뀔 수 있으니 신청 전에 공식 안내를 꼭 다시 확인해 주세요."
    parts: list[str] = []
    if ranked:
        items = "\n".join(_footer_item(source) for source in ranked[:max_items])
        parts.append(f"<h2>{FOOTER_HEADING}</h2>\n<ul>\n{items}\n</ul>")
    parts.append(f"<p>{notice}</p>")
    return "\n".join(parts)


def link_rel(url: str) -> str:
    """외부 링크의 rel 값. 공식 기관이 아닌 곳으로는 검색 점수를 넘기지 않도록 nofollow를 붙인다."""
    return "noopener noreferrer" if is_official_host(host_of(url)) else "noopener noreferrer nofollow"


def _footer_item(source: Source) -> str:
    # 제목은 검색 결과를 쓴 쪽이 정하므로(공식 안내처럼 꾸밀 수 있다), 실제 호스트를 옆에 함께 보여 준다.
    host = host_of(source.url)
    return (
        f'<li><a href="{escape(source.url, quote=True)}" target="_blank" rel="{link_rel(source.url)}">'
        f"{escape(strip_invisible(source.title), quote=False)}</a> ({escape(host, quote=False)})</li>"
    )


_FOOTER = re.compile(
    rf"(?:<h2>{FOOTER_HEADING}</h2>\s*<ul>.*?</ul>\s*)?<p>※ [^<]*기준으로[^<]*</p>\s*$",
    re.DOTALL,
)


def strip_footer(html: str) -> str:
    """render_footer가 글 끝에 붙인 '참고한 자료'·확인일 안내를 떼어 낸다(저장된 글을 본문만으로 점검할 때 쓴다)."""
    return _FOOTER.sub("", html.rstrip(), count=1).rstrip()
