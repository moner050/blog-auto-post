from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.settings import Settings
from app.llm.client import PerplexityClient, PerplexityCompletion
from app.topics.discovery import TopicDiscoverer, TopicDiscoveryError


def sonar_response(candidates: list[dict]) -> PerplexityCompletion:
    return PerplexityCompletion(
        content=json.dumps({"candidates": candidates}),
        citations=["https://news.example.com/topic", "https://community.example.com/topic"],
        search_results=[
            {"url": "https://news.example.com/topic", "title": "뉴스 근거"},
            {"url": "https://community.example.com/topic", "title": "커뮤니티 근거"},
        ],
    )


def test_discoverer_keeps_valid_unique_candidates_and_maps_citations() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "정부지원 청년도약계좌 신청 방법",
                "category": "정부지원·민원",
                "reason": "최근 이용 문의가 늘고 있습니다.",
                "citation_indices": [1, 2],
            },
            {
                "topic": "  정부지원 청년도약계좌   신청 방법  ",
                "category": "정부지원·민원",
                "reason": "중복 후보입니다.",
                "citation_indices": [1],
            },
            {
                "topic": "허용되지 않은 후보",
                "category": "경제",
                "reason": "허용되지 않은 카테고리입니다.",
                "citation_indices": [1],
            },
            {
                "topic": "잘못된 출처 번호",
                "category": "생활꿀팁",
                "reason": "출처 번호가 존재하지 않습니다.",
                "citation_indices": [3],
            },
        ]
    )

    discovered = TopicDiscoverer(client).discover()

    assert len(discovered) == 1
    assert discovered[0].topic == "정부지원 청년도약계좌 신청 방법"
    assert discovered[0].sources == [
        {"title": "뉴스 근거", "url": "https://news.example.com/topic"},
        {"title": "커뮤니티 근거", "url": "https://community.example.com/topic"},
    ]
    assert client.completion_response.call_args.kwargs["search_recency_filter"] == "month"
    assert client.completion_response.call_args.kwargs["search_language_filter"] == ["ko"]
    assert "practical household tips" in client.completion_response.call_args.kwargs["messages"][1]["content"]
    assert "정부지원·민원" in client.completion_response.call_args.kwargs["messages"][1]["content"]


def test_discoverer_supports_focus_sns_option() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "클리앙/뽐뿌 핫딜 알뜰폰 요금제 비교",
                "category": "생활꿀팁",
                "reason": "커뮤니티에서 실시간 관심도가 매우 높습니다.",
                "citation_indices": [2],
            }
        ]
    )

    discovered = TopicDiscoverer(client).discover(focus_sns=True)

    assert len(discovered) == 1
    system_prompt = client.completion_response.call_args.kwargs["messages"][0]["content"]
    user_prompt = client.completion_response.call_args.kwargs["messages"][1]["content"]
    assert "EXCLUSIVELY" in system_prompt
    assert "Search EXCLUSIVELY" in user_prompt
    assert "DCInside" in user_prompt or "Clien" in user_prompt


def test_discoverer_rejects_empty_valid_result() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "출처 없는 후보",
                "category": "여행·할인",
                "reason": "근거가 없습니다.",
                "citation_indices": [],
            }
        ]
    )

    with pytest.raises(TopicDiscoveryError, match="no valid"):
        TopicDiscoverer(client).discover()


def test_discoverer_rejects_non_http_citation_url() -> None:
    client = MagicMock()
    client.completion_response.return_value = PerplexityCompletion(
        content=json.dumps(
            {
                "candidates": [
                    {
                        "topic": "안전하지 않은 링크 후보",
                        "category": "생활꿀팁",
                        "reason": "잘못된 URL입니다.",
                        "citation_indices": [1],
                    }
                ]
            }
        ),
        citations=["javascript:alert(1)"],
        search_results=[],
    )

    with pytest.raises(TopicDiscoveryError, match="no valid"):
        TopicDiscoverer(client).discover()


@patch("urllib.request.urlopen")
def test_completion_response_keeps_sonar_citations_and_search_results(mock_urlopen) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps(
        {
            "choices": [{"message": {"content": "{}"}}],
            "citations": ["https://example.com/source"],
            "search_results": [{"url": "https://example.com/source", "title": "출처"}],
        }
    ).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = response

    client = PerplexityClient(Settings(perplexity_api_key="pplx-valid-key"))
    completion = client.completion_response(
        messages=[{"role": "user", "content": "최근 한국 이슈"}],
        response_format={"type": "json_schema", "json_schema": {"schema": {"type": "object"}}},
        search_recency_filter="month",
        search_language_filter=["ko"],
    )

    request_payload = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
    assert mock_urlopen.call_args.args[0].full_url.endswith("/v1/sonar")
    assert completion.citations == ["https://example.com/source"]
    assert completion.search_results == [{"url": "https://example.com/source", "title": "출처"}]
    assert request_payload["response_format"]["type"] == "json_schema"
    assert request_payload["search_recency_filter"] == "month"
