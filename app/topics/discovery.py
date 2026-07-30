from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
from typing import Any
from urllib.parse import urlparse

from app.llm.client import PerplexityClient


ALLOWED_CATEGORIES = (
    "홈택스",
    "정부정책",
    "정부24",
    "법원",
    "생활꿀팁",
    "여행꿀팁",
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
                    "maxItems": 12,
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


class TopicDiscoveryError(ValueError):
    """Sonar 주제 추천 응답을 안전하게 사용할 수 없을 때 발생."""


@dataclass(frozen=True)
class DiscoveredTopic:
    topic: str
    topic_hash: str
    category: str
    reason: str
    sources: list[dict[str, str]]


class TopicDiscoverer:
    """Sonar의 최근 검색 결과를 검증 가능한 주제 후보로 정리한다."""

    def __init__(self, client: PerplexityClient):
        self.client = client

    def discover(self) -> list[DiscoveredTopic]:
        completion = self.client.completion_response(
            messages=_build_messages(),
            temperature=0.2,
            max_tokens=2500,
            response_format=TOPIC_DISCOVERY_RESPONSE_FORMAT,
            search_recency_filter="month",
            search_language_filter=["ko"],
        )
        payload = _parse_payload(completion.content)
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise TopicDiscoveryError("Sonar topic response has no candidates")

        discovered: list[DiscoveredTopic] = []
        seen_hashes: set[str] = set()
        for item in candidates[:12]:
            candidate = _validate_candidate(item, completion.citations, completion.search_results)
            if candidate is None or candidate.topic_hash in seen_hashes:
                continue
            seen_hashes.add(candidate.topic_hash)
            discovered.append(candidate)
        if not discovered:
            raise TopicDiscoveryError("Sonar returned no valid topic candidates")
        return discovered


def _build_messages() -> list[dict[str, str]]:
    categories = ", ".join(ALLOWED_CATEGORIES)
    return [
        {
            "role": "system",
            "content": (
                "검색 결과에 근거한 한국어 블로그 주제 추천 도우미다. "
                "반드시 제공된 JSON Schema만 따른다. URL을 직접 작성하지 말고, "
                "검색 근거의 citation 번호만 citation_indices에 넣는다."
            ),
        },
        {
            "role": "user",
            "content": (
                f"오늘은 {date.today().isoformat()}이다. 최근 30일 한국 뉴스와 커뮤니티에서 "
                f"관심을 받은 실용 블로그 주제를 최대 12개 추천해줘. 허용 카테고리는 {categories}뿐이다. "
                "각 카테고리에서 최대 2개만 제안하고, 각 후보에는 왜 지금 유용한지 짧게 설명해줘. "
                "모든 후보는 검색 결과 citation을 하나 이상 연결해야 한다."
            ),
        },
    ]


def _parse_payload(raw_content: str) -> dict[str, Any]:
    cleaned = raw_content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError as error:
        raise TopicDiscoveryError("Sonar topic response is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise TopicDiscoveryError("Sonar topic response must be an object")
    return parsed


def _validate_candidate(
    item: object,
    citations: list[str],
    search_results: list[dict[str, Any]],
) -> DiscoveredTopic | None:
    if not isinstance(item, dict):
        return None
    topic = _normalize(item.get("topic"))
    category = _normalize(item.get("category"))
    reason = _normalize(item.get("reason"))
    source_indices = item.get("citation_indices")
    if not topic or not reason or category not in ALLOWED_CATEGORIES or not isinstance(source_indices, list):
        return None
    sources = _sources_for_indices(source_indices, citations, search_results)
    if not sources:
        return None
    return DiscoveredTopic(
        topic=topic,
        topic_hash=sha256(topic.lower().encode("utf-8")).hexdigest(),
        category=category,
        reason=reason,
        sources=sources,
    )


def _sources_for_indices(
    indices: list[object], citations: list[str], search_results: list[dict[str, Any]]
) -> list[dict[str, str]]:
    title_by_url = {
        result.get("url"): result.get("title")
        for result in search_results
        if isinstance(result.get("url"), str) and isinstance(result.get("title"), str)
    }
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for index in indices:
        if not isinstance(index, int) or isinstance(index, bool) or index < 1 or index > len(citations):
            return []
        url = citations[index - 1].strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = title_by_url.get(url) or urlparse(url).netloc or url
        sources.append({"title": title, "url": url})
    return sources


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()
