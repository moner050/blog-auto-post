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
                "topic": "교통사고 형사합의 절차 및 판례 가이드",
                "category": "법률·합의·분쟁",
                "reason": "최근 이용 문의가 늘고 있습니다.",
                "citation_indices": [1, 2],
            },
            {
                "topic": "  교통사고 형사합의 절차 및 판례 가이드  ",
                "category": "법률·합의·분쟁",
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
                "category": "주식·코인·투자",
                "reason": "출처 번호가 존재하지 않습니다.",
                "citation_indices": [3],
            },
        ]
    )

    discovered = TopicDiscoverer(client).discover()

    assert len(discovered) == 1
    assert discovered[0].topic == "교통사고 형사합의 절차 및 판례 가이드"
    assert discovered[0].sources == [
        {"title": "뉴스 근거", "url": "https://news.example.com/topic"},
        {"title": "커뮤니티 근거", "url": "https://community.example.com/topic"},
    ]
    assert client.completion_response.call_args.kwargs["search_recency_filter"] == "week"
    assert client.completion_response.call_args.kwargs["search_language_filter"] == ["ko"]
    assert "legal disputes" in client.completion_response.call_args.kwargs["messages"][1]["content"]
    assert "법률·합의·분쟁" in client.completion_response.call_args.kwargs["messages"][1]["content"]


def test_discoverer_supports_focus_sns_option() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "미국 배당 ETF 월배당 포트폴리오 비교",
                "category": "주식·코인·투자",
                "reason": "커뮤니티에서 실시간 관심도가 매우 높습니다.",
                "citation_indices": [2],
            }
        ]
    )

    discovered = TopicDiscoverer(client).discover(focus_sns=True)

    assert len(discovered) == 1
    system_prompt = client.completion_response.call_args.kwargs["messages"][0]["content"]
    user_prompt = client.completion_response.call_args.kwargs["messages"][1]["content"]
    assert "online community discussions" in system_prompt or "online community" in user_prompt
    assert "DCInside" in user_prompt or "Clien" in user_prompt


def test_discoverer_novelty_mode_uses_high_temperature_and_unique_prompt() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "실손보험 도수치료 청구 거절 시 금감원 민원 대처법",
                "category": "보험·보상·청구",
                "reason": "남들이 잘 모르는 틈새 보상 팁입니다.",
                "citation_indices": [1],
            }
        ]
    )

    discovered = TopicDiscoverer(client).discover(novelty=True)

    assert len(discovered) == 1
    assert client.completion_response.call_args.kwargs["temperature"] == 0.9
    user_prompt = client.completion_response.call_args.kwargs["messages"][1]["content"]
    assert "Make the topic titles extra intriguing" in user_prompt


def test_discoverer_passes_existing_topics_exclusion_to_prompt() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "새로운 독창적 주제",
                "category": "주식·코인·투자",
                "reason": "기존 주제와 전혀 다릅니다.",
                "citation_indices": [2],
            }
        ]
    )

    existing = ["교통사고 형사합의 절차 및 판례 가이드", "미국 배당 ETF 월배당 포트폴리오 비교"]
    discovered = TopicDiscoverer(client).discover(existing_topics=existing)

    assert len(discovered) == 1
    user_prompt = client.completion_response.call_args.kwargs["messages"][1]["content"]
    assert "avoid exact duplicate topics" in user_prompt
    assert "교통사고 형사합의 절차 및 판례 가이드" in user_prompt
    assert "미국 배당 ETF 월배당 포트폴리오 비교" in user_prompt


def test_discoverer_rejects_empty_valid_result() -> None:
    client = MagicMock()
    client.completion_response.return_value = sonar_response(
        [
            {
                "topic": "출처 없는 후보",
                "category": "여행·특가·예약",
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
                        "category": "차량·리스·렌트",
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

    client = PerplexityClient(Settings(perplexity_api_key="pplx-valid-key", perplexity_api_mode="sonar"))
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
