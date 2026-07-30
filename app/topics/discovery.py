from __future__ import annotations

from dataclasses import dataclass
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
                "Recommend Korean blog topics only from the provided search results. "
                "Follow the JSON Schema exactly. Do not write URLs; put only Sonar's "
                "1-based source numbers in citation_indices."
            ),
        },
        {
            "role": "user",
            "content": (
                "Find recent Korean news and community discussions from the last 30 days about "
                "these six areas: Hometax tax filing, refunds, and deductions; Korean government "
                "policy benefits; Government24 public services; court or legal-life issues; practical "
                "household tips; and Korea travel tips. Identify current issues or timely how-to topics "
                "that Korean readers would search for. Return up to 12 Korean candidate topics, at most "
                f"2 per requested category ({categories}), as the JSON schema. Each candidate must include "
                "one or more citation_indices corresponding to the source list returned by Sonar."
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
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return []
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = title_by_url.get(url) or parsed_url.netloc
        sources.append({"title": title, "url": url})
    return sources


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()
