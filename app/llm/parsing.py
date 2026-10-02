"""모델 출력(태그 구조 또는 JSON)을 글 필드로 해석하고 제목·요약·태그를 정리한다.

긴 HTML을 JSON 문자열에 넣으면 따옴표 이스케이프가 자주 깨지므로 태그 구조를 기본 출력 형식으로 쓰고,
JSON은 모델이 형식을 어겼을 때를 위한 폴백으로만 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import itertools
import json
import re
from typing import Any

from app.llm.sanitize import strip_citation_markers, strip_tags
from app.llm.textutil import strip_invisible

MAX_TITLE_CHARS = 255  # article_versions.title 컬럼 폭
MAX_SUMMARY_INPUT_CHARS = 5_000  # 요약·태그는 짧은 필드라 이보다 긴 입력은 처리하지 않고 자른다
MAX_TAG_INPUT_CHARS = 2_000
MAX_TOPIC_INPUT_CHARS = 2_000  # 대체 태그는 주제의 앞부분 단어만 쓰므로 그보다 긴 입력은 읽지 않는다
MAX_TAG_INPUT_ITEMS = 50
MAX_TAGS = 10
MAX_TAG_CHARS = 30
_FALLBACK_TAGS = 5
_TAG_STOPWORDS = frozenset(
    {
        "방법", "하는", "하면", "알아보기", "정리", "확인", "지금", "모르면", "손해", "대한", "위한", "있는", "없는",
        "어떻게", "무엇", "총정리", "가이드", "추천", "최신", "완벽", "핵심", "먼저", "꼭", "이것", "바로",
    }
)
# 이모지만 센다: 이모지 문자(U+1F000대), 선택자 없이도 이모지로 그려지는 기호(✅ ⭐ ❌ 등), 이모지 표시 선택자(VS16)가 붙은 글자(⚠️ ✔️).
# ★ ♥ ✓ ⌘ 같은 일반 기호와 방향 화살표는 이모지가 아니다.
_VS16 = chr(0xFE0F)
_EMOJI_PRESENTATION_BMP = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3), (0x25FD, 0x25FE), (0x2614, 0x2615),
    (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5),
    (0x26FA, 0x26FA), (0x26FD, 0x26FD), (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
)
_EMOJI_RE = re.compile(
    "|".join(
        [
            f"[{chr(0x1F000)}-{chr(0x1FAFF)}]",
            "[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in _EMOJI_PRESENTATION_BMP) + "]",
            rf"[^\s{_VS16}]{_VS16}",
        ]
    )
)
_EMOJI_JOINERS = re.compile(f"[{_VS16}{chr(0x200D)}{chr(0x20E3)}]")
_QUOTE_PAIRS = {'"': '"', "'": "'", "`": "`", "“": "”", "‘": "’", "「": "」", "『": "』"}
_LEADING_MARKUP = re.compile(r"^(?:[#>*\-]+\s+)+")
_EMPHASIS = re.compile(r"(\*{1,3}|__)(?=\S)(.+?)(?<=\S)\1")
# 필드 추출은 '여는 태그 → 그 뒤 첫 닫는 태그'를 리터럴로 찾는다. 지연 매칭(.*?) 정규식은 닫는 태그 없이
# 여는 태그가 반복되는 입력에서 제곱 시간이 된다.
_OPEN_RES = {name: re.compile(f"<article_{name}>", re.IGNORECASE) for name in ("title", "summary", "tags", "sources")}
_CLOSE_RES = {name: re.compile(f"</article_{name}>", re.IGNORECASE) for name in ("title", "summary", "tags", "sources")}
MAX_SOURCE_IDS = 20
_BODY_OPEN = re.compile(r"<article_body>", re.IGNORECASE)
_BODY_CLOSE = re.compile(r"</article_body>", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```[A-Za-z]*[ \t]*\n(.*)\n```\s*$", re.DOTALL)


class ArticleParseError(ValueError):
    """모델 응답에서 제목·본문을 찾지 못했을 때."""


class ArticleTruncatedError(ArticleParseError):
    """응답이 길이 제한으로 잘려 글이 완성되지 않았을 때."""


@dataclass(frozen=True)
class ParsedArticle:
    title: str
    summary: str
    tags: list[str]
    body_html: str
    source_ids: tuple[int, ...] = ()  # 글의 근거로 실제 쓴 검색 결과 번호(<article_sources>)


def parse_article(text: str, *, truncated: bool = False) -> ParsedArticle:
    """모델 응답을 해석한다. truncated=True이면 닫는 태그가 없는 응답을 잘린 것으로 본다."""
    cleaned = _strip_outer_fence(text.strip())
    opened = _BODY_OPEN.search(cleaned)
    if opened:
        rest = cleaned[opened.end():]
        closed = _BODY_CLOSE.search(rest)
        if closed is None and truncated:
            raise ArticleTruncatedError("응답이 길이 제한으로 잘려 본문이 완성되지 않았습니다.")
        body = (rest[: closed.start()] if closed else rest).strip()
        head = cleaned[: opened.start()]
        title = _field(head, "title") or _field(cleaned, "title")
        if not title:
            raise ArticleParseError("응답에 제목(<article_title>)이 없습니다.")
        if not body:
            raise ArticleParseError("응답에 본문(<article_body>)이 비어 있습니다.")
        return ParsedArticle(
            title=title,
            summary=_field(head, "summary") or _field(cleaned, "summary"),
            tags=_split_tags(_field(head, "tags") or _field(cleaned, "tags")),
            body_html=body,
            source_ids=_parse_ids(_field(head, "sources")),
        )

    payload = _extract_json_object(cleaned)
    if payload is not None:
        title = _first_text(payload, "title")
        body = _first_text(payload, "body_html", "body", "content", "html")
        if title and body:
            tags = payload.get("tags")
            return ParsedArticle(
                title=title,
                summary=_first_text(payload, "summary", "description"),
                tags=[str(tag) for tag in tags] if isinstance(tags, list) else _split_tags(str(tags or "")),
                body_html=body,
            )
    if truncated:
        raise ArticleTruncatedError("응답이 길이 제한으로 잘려 해석할 수 없습니다.")
    raise ArticleParseError(f"응답 형식을 해석하지 못했습니다: {cleaned[:100]}")


def format_article_block(
    title: str, summary: str, tags: list[str], body_html: str, source_ids: tuple[int, ...] = ()
) -> str:
    """수정 요청에 넣을 초안을 출력 형식(태그 구조)으로 만든다."""
    sources = f"<article_sources>{', '.join(str(number) for number in source_ids)}</article_sources>\n" if source_ids else ""
    return (
        f"<article_title>{title}</article_title>\n"
        f"<article_summary>{summary}</article_summary>\n"
        f"<article_tags>{', '.join(tags)}</article_tags>\n"
        f"{sources}"
        f"<article_body>\n{body_html}\n</article_body>"
    )


def count_emoji(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


def strip_emoji(text: str) -> str:
    return _EMOJI_JOINERS.sub("", _EMOJI_RE.sub("", text))


def normalize_title(raw: str) -> str:
    """제목에서 태그·출처 번호·마크다운 기호·이모지를 걷어내고 반복 부호를 정리한다."""
    title = strip_invisible(html.unescape(raw[: MAX_TITLE_CHARS * 20]))  # 엔티티·태그가 줄어들 여지만 두고 미리 자른다
    title = strip_tags(title)
    title = strip_citation_markers(title)
    # '## 제목', '- 제목'처럼 마크다운 기호 뒤에 공백이 있을 때만 지운다('-5도 한파', '#1 추천'의 기호는 제목의 일부다).
    title = _EMPHASIS.sub(r"\2", _LEADING_MARKUP.sub("", title.strip()))
    title = strip_emoji(title)
    title = re.sub(r"([!?！？])\1+", r"\1", title)
    title = re.sub(r"\s+", " ", title).strip()
    return _unwrap_quotes(title)[:MAX_TITLE_CHARS].strip()


def _unwrap_quotes(title: str) -> str:
    """제목 전체가 따옴표 한 쌍으로 감싸졌을 때만 벗긴다('따옴표' 제목 '끝'처럼 서로 다른 쌍의 따옴표는 둔다)."""
    if len(title) >= 2 and _QUOTE_PAIRS.get(title[0]) == title[-1]:
        inner = title[1:-1]
        if title[0] not in inner and title[-1] not in inner:
            return inner.strip()
    return title


def normalize_summary(raw: str) -> str:
    text = strip_invisible(html.unescape(strip_tags(raw[:MAX_SUMMARY_INPUT_CHARS])))
    return re.sub(r"\s+", " ", strip_citation_markers(text)).strip()


def normalize_tags(raw: list[str] | str | None) -> list[str]:
    items = raw[:MAX_TAG_INPUT_ITEMS] if isinstance(raw, list) else _split_tags((raw or "")[:MAX_TAG_INPUT_CHARS])
    tags: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = strip_invisible(html.unescape(strip_tags(str(item))))
        tag = re.sub(r"\s+", " ", strip_citation_markers(tag)).strip().lstrip("#").strip()
        key = tag.lower()
        if not tag or len(tag) > MAX_TAG_CHARS or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags[:MAX_TAGS]


def derive_tags(topic: str, category: str | None = None) -> list[str]:
    """모델이 태그를 주지 않았을 때 주제 문장에서 핵심 단어로 대체 태그를 만든다."""
    words: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"[0-9A-Za-z가-힣]+", topic[:MAX_TOPIC_INPUT_CHARS]):
        if len(word) >= 2 and word not in _TAG_STOPWORDS and word not in seen:
            seen.add(word)
            words.append(word)
            if len(words) == _FALLBACK_TAGS:
                break
    tags = words[:_FALLBACK_TAGS]
    if category and category.strip() and category.strip() not in tags:
        tags.append(category.strip())
    return normalize_tags(tags)


def _strip_outer_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def _field(text: str, name: str) -> str:
    opener = _OPEN_RES[name].search(text)
    if opener is None:
        return ""
    closer = _CLOSE_RES[name].search(text, opener.end())
    return text[opener.end(): closer.start()].strip() if closer else ""


def _parse_ids(raw: str) -> tuple[int, ...]:
    """'1, 3, 4' 같은 번호 목록을 순서를 지켜 중복 없이 정수로 바꾼다."""
    ids: list[int] = []
    for match in re.findall(r"\d{1,3}", raw):
        number = int(match)
        if number >= 1 and number not in ids:
            ids.append(number)
    return tuple(ids[:MAX_SOURCE_IDS])


def _split_tags(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\n|;]+", raw) if part.strip()]


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for match in itertools.islice(re.finditer(r"\{", text), 50):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except (json.JSONDecodeError, RecursionError):  # 깊이가 한도를 넘는 중첩은 JSON이 아니라고 본다
            continue
        if isinstance(value, dict):
            return value
    return None
