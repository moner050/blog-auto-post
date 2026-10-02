from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import random
import re
from typing import Any
import unicodedata
from urllib.parse import urlparse

from app.llm.client import PerplexityClient
from app.llm.textutil import strip_invisible


_logger = logging.getLogger("tistory_automation")

MAX_CANDIDATES = 12
# Candidates read before validation. Invalid ones must not use up the slots of valid ones that follow.
MAX_RAW_CANDIDATES = 36
# topic_candidates.topic is VARCHAR(255); keep a margin so a runaway string cannot fail the insert.
MAX_TOPIC_CHARS = 200

ALLOWED_CATEGORIES = (
    "생활꿀팁",
    "정부지원·민원",
    "대출·금융",
    "세금·환급",
    "교통·카드혜택",
    "여행·할인",
)

TOPIC_DISCOVERY_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "topic_candidates",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "array",
                    "maxItems": MAX_CANDIDATES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["topic", "category", "reason", "citation_indices"],
                        "properties": {
                            "topic": {"type": "string"},
                            "category": {"type": "string", "enum": list(ALLOWED_CATEGORIES)},
                            "reason": {"type": "string"},
                            "citation_indices": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                }
            },
        },
    },
}

RANDOM_SEARCH_ANGLES = [
    "recent loan regulations, ISA tax-saving account updates, and real estate/housing policy changes",
    "new government subsidies, civil service updates, and practical money-saving financial hacks",
    "trending community discussions, real consumer reactions, and viral lifestyle tips",
    "transportation and credit card benefit perks, app shortcuts, and seasonal travel discounts",
    "emerging consumer issues, hot policy debates, and high-search-volume practical how-to guides",
]


class TopicDiscoveryError(ValueError):
    """Raised when no safe topic candidate can be retained from Sonar's response."""


@dataclass(frozen=True)
class DiscoveredTopic:
    topic: str
    topic_hash: str
    category: str
    reason: str
    sources: list[dict[str, str]]


class TopicDiscoverer:
    """Collect and validate blog topic candidates grounded in recent Sonar search results."""

    def __init__(self, client: PerplexityClient):
        self.client = client

    def discover(
        self,
        focus_sns: bool = False,
        novelty: bool = False,
        existing_topics: list[str] | None = None,
    ) -> list[DiscoveredTopic]:
        temperature = 0.9 if novelty else 0.7
        completion = self.client.completion_response(
            messages=_build_messages(focus_sns=focus_sns, novelty=novelty, existing_topics=existing_topics),
            temperature=temperature,
            max_tokens=2500,
            response_format=TOPIC_DISCOVERY_RESPONSE_FORMAT,
            search_recency_filter="week",
            search_language_filter=["ko"],
        )
        candidates, salvaged = _load_candidates(
            completion.content, truncated=completion.finish_reason == "length"
        )
        if salvaged:
            _logger.warning(
                "topic discovery response was damaged (finish_reason=%s); recovered %d complete candidates",
                completion.finish_reason,
                len(candidates),
            )
        citations = completion.citations or _search_result_urls(completion.search_results)

        discovered: list[DiscoveredTopic] = []
        seen_hashes: set[str] = set()
        dropped: Counter[str] = Counter()
        for item in candidates[:MAX_RAW_CANDIDATES]:
            try:
                candidate = _validate_candidate(item, citations, completion.search_results)
            except _Rejected as rejected:
                dropped[rejected.reason] += 1
                continue
            if candidate.topic_hash in seen_hashes:
                dropped["duplicate topic"] += 1
                continue
            seen_hashes.add(candidate.topic_hash)
            discovered.append(candidate)
            if len(discovered) == MAX_CANDIDATES:
                break

        summary = ", ".join(f"{reason} x{count}" for reason, count in dropped.items())
        if discovered and dropped:
            _logger.warning("topic discovery kept %d candidates; dropped: %s", len(discovered), summary)
        if not discovered:
            raise TopicDiscoveryError(
                "Sonar returned no valid topic candidates" + (f" (dropped: {summary})" if dropped else "")
            )
        return discovered


def _build_messages(
    focus_sns: bool = False,
    novelty: bool = False,
    existing_topics: list[str] | None = None,
) -> list[dict[str, str]]:
    categories = ", ".join(ALLOWED_CATEGORIES)
    current_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    random_angle = random.choice(RANDOM_SEARCH_ANGLES)

    if focus_sns:
        system_prompt = (
            f"As of {current_date_str}, recommend viral, high-CTR, and highly engaging Korean blog topic candidates with heavy focus on online community discussions, forum posts, and social media user feedback. "
            "Craft catchy, clickworthy titles that immediately trigger readers' curiosity. Follow the JSON Schema exactly. Do not write URLs; put only Sonar's 1-based source numbers in citation_indices."
        )
        user_prompt = (
            f"Search for real-time trending online community discussions, forum posts, viral social media trends, and news from the last 7 days as of {current_date_str} "
            "(such as DCInside, Clien, Ppomppu, Ruliweb, FMKorea, Blind, Naver Cafe, Instagram, YouTube, X/Twitter). "
            f"Explore current hot topics including: {random_angle}. "
            "Craft highly engaging, clickworthy, and viral Korean topic titles that compel readers to click—including recent loan regulations (대출 규제/DSR/LTV), ISA tax-saving account updates (ISA 개정), "
            "real estate policies (부동산/주택 정책), government subsidies (정부지원·민원), tax refunds (세금·환급), card/transport perks (교통·카드혜택), travel discounts (여행·할인), and practical life hacks (생활꿀팁). "
        )
    else:
        system_prompt = (
            f"As of {current_date_str}, recommend viral, high-CTR, and highly engaging Korean blog topic candidates from recent web search results and news. "
            "Craft catchy, clickworthy titles that immediately trigger readers' curiosity. Follow the JSON Schema exactly. Do not write URLs; put only Sonar's 1-based source numbers in citation_indices."
        )
        user_prompt = (
            f"Find hot Korean news, policy updates, and trending discussions from the last 7 days as of {current_date_str}. "
            f"Explore key current issues such as: {random_angle}. "
            "Craft high-CTR, viral, and catchy Korean topic titles that readers cannot resist clicking—including recent loan regulations (대출 규제/DSR), ISA account updates (ISA 세제 혜택), "
            "real estate policies (부동산 정책), government support (정부지원·민원), tax refunds (세금·환급), card/transport perks (교통·카드혜택), travel discounts (여행·할인), and practical daily tips (생활꿀팁). "
        )

    if novelty:
        user_prompt += (
            "Make the topic titles extra intriguing, provocative, and highly clickworthy, highlighting surprising angles that maximize reader curiosity and click-through rates. "
        )
    else:
        user_prompt += (
            "Ensure topics are catchy, practical, timely, and naturally mapped across the requested categories. "
        )

    user_prompt += (
        f"Return up to 12 Korean candidate topics, at most 2 per requested category ({categories}), as the JSON schema. "
        "Each candidate must include one or more citation_indices corresponding to the source list returned by Sonar."
    )

    if existing_topics:
        filtered_existing = [t.strip() for t in existing_topics if t and isinstance(t, str)][:25]
        if filtered_existing:
            topics_list_str = "\n- ".join(filtered_existing)
            user_prompt += (
                "\n\nTry to avoid exact duplicate topics from the following list:\n- "
                + topics_list_str
            )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


class _Rejected(Exception):
    """A candidate that cannot be kept. ``reason`` is the short label shown in logs and error messages."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_CANDIDATES_KEY = re.compile(r'"candidates"\s*:\s*\[')
# Separators models write in place of the middle dot in category names such as "정부지원·민원".
_CATEGORY_SEPARATORS = re.compile(r"[\s·ㆍ・･‧∙•⋅/&,_-]+")


def _load_candidates(raw_content: object, truncated: bool = False) -> tuple[list[object], bool]:
    """Return the raw candidate list and whether it had to be salvaged from damaged JSON."""
    text = _strip_fences(raw_content) if isinstance(raw_content, str) else ""
    if not text:
        raise TopicDiscoveryError("Sonar topic response is empty")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as error:
        salvaged = _salvage_candidates(text)
        if not salvaged:
            hint = " (cut off by the output token limit)" if truncated else ""
            raise TopicDiscoveryError(f"Sonar topic response is not valid JSON{hint}") from error
        return salvaged, True
    if isinstance(parsed, list):
        return parsed, False
    if not isinstance(parsed, dict):
        raise TopicDiscoveryError("Sonar topic response must be an object")
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        raise TopicDiscoveryError("Sonar topic response has no candidates")
    return candidates, False


def _strip_fences(raw_content: str) -> str:
    cleaned = raw_content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _salvage_candidates(text: str) -> list[object]:
    """Read the complete candidate objects out of JSON that is cut off or has stray text around it."""
    match = _CANDIDATES_KEY.search(text)
    if match:
        position = match.end()
    elif text.startswith("["):
        position = 1
    else:
        return []
    decoder = json.JSONDecoder()
    items: list[object] = []
    while len(items) < MAX_RAW_CANDIDATES:
        while position < len(text) and text[position] in " \t\r\n,":
            position += 1
        if position >= len(text) or text[position] == "]":
            break
        try:
            item, position = decoder.raw_decode(text, position)
        except (json.JSONDecodeError, RecursionError):
            break
        items.append(item)
    return items


def _search_result_urls(search_results: list[dict[str, Any]]) -> list[str]:
    """Fallback citation list. Entries without a URL stay as blanks so the 1-based numbering keeps its positions."""
    return [
        result["url"] if isinstance(result, dict) and isinstance(result.get("url"), str) else ""
        for result in search_results
    ]


def _validate_candidate(
    item: object,
    citations: list[str],
    search_results: list[dict[str, Any]],
) -> DiscoveredTopic:
    if not isinstance(item, dict):
        raise _Rejected("not an object")
    topic = _normalize(item.get("topic"))
    if not topic:
        raise _Rejected("missing topic")
    if len(topic) > MAX_TOPIC_CHARS:
        raise _Rejected("topic too long")
    category = _canonical_category(item.get("category"))
    if category is None:
        raise _Rejected("unknown category")
    reason = _normalize(item.get("reason"))
    if not reason:
        raise _Rejected("missing reason")
    sources = _sources_for_indices(item.get("citation_indices"), citations, search_results)
    if not sources:
        raise _Rejected("no usable sources")
    return DiscoveredTopic(
        topic=topic,
        topic_hash=sha256(topic.lower().encode("utf-8")).hexdigest(),
        category=category,
        reason=reason,
        sources=sources,
    )


def _sources_for_indices(
    indices: object, citations: list[str], search_results: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Map 1-based citation numbers to sources, keeping every usable one.

    A number above the source count, a non-numeric entry, or a non-http(s) URL only drops that source.
    A number below 1 means the model counted from zero, so the other numbers are probably shifted as well
    and the whole candidate is rejected rather than attached to the wrong sources.
    """
    if not isinstance(indices, list):
        indices = [indices]
    title_by_url = {
        result.get("url"): result.get("title")
        for result in search_results
        if isinstance(result, dict) and isinstance(result.get("url"), str) and isinstance(result.get("title"), str)
    }
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for value in indices:
        index = _as_index(value)
        if index is None:
            continue
        if index < 1:
            raise _Rejected("citation number below 1")
        if index > len(citations):
            continue
        cited = citations[index - 1]
        url = cited.strip() if isinstance(cited, str) else ""
        try:
            parsed_url = urlparse(url)
        except ValueError:
            continue
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({"title": title_by_url.get(url) or parsed_url.netloc, "url": url})
    return sources


def _as_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdecimal() and len(value.strip()) <= 9:
        return int(value)
    return None


def _category_key(value: str) -> str:
    # NFKC rewrites some separators (ㆍ becomes a Hangul vowel, ･ becomes ・), so strip before and after it.
    stripped = _CATEGORY_SEPARATORS.sub("", value)
    return _CATEGORY_SEPARATORS.sub("", unicodedata.normalize("NFKC", stripped))


_CATEGORY_BY_KEY = {_category_key(name): name for name in ALLOWED_CATEGORIES}


def _canonical_category(value: object) -> str | None:
    """Map a category the model wrote to the allowed name, tolerating spacing and separator variants only."""
    return _CATEGORY_BY_KEY.get(_category_key(value)) if isinstance(value, str) else None


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", strip_invisible(value)).strip()
