"""주제 탐색이 모델 응답의 일부 결함 때문에 전체를 잃지 않는지 검증한다 (Perplexity 호출은 모두 대역)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from app.db.models import TopicCandidate
from app.llm.client import PerplexityClient, PerplexityCompletion, parse_agent_response
from app.topics.discovery import MAX_CANDIDATES, MAX_RAW_CANDIDATES, MAX_TOPIC_CHARS, TopicDiscoverer, TopicDiscoveryError

FIXTURE = Path(__file__).parent / "fixtures" / "perplexity_agent_response.json"
URLS = [f"https://news.example.com/article/{n}" for n in range(1, 7)]


def candidate(n: int = 1, **overrides: object) -> dict:
    return {
        "topic": f"주제 {n}번 후보",
        "category": "생활꿀팁",
        "reason": f"이유 {n}",
        "citation_indices": [1],
        **overrides,
    }


def completion(
    content: object,
    *,
    citations: list[str] = URLS,
    search_results: list[dict] | None = None,
    finish_reason: str | None = "stop",
) -> PerplexityCompletion:
    if search_results is None:
        search_results = [{"url": url, "title": f"출처 {n}"} for n, url in enumerate(citations, 1)]
    text = content if isinstance(content, str) else json.dumps({"candidates": content}, ensure_ascii=False)
    return PerplexityCompletion(
        content=text, citations=list(citations), search_results=search_results, finish_reason=finish_reason
    )


def discover(content: object, **kwargs: object):  # noqa: ANN201
    client = create_autospec(PerplexityClient, instance=True)  # 시그니처가 바뀌면 이 테스트가 먼저 알려 준다
    client.completion_response.return_value = completion(content, **kwargs)
    return TopicDiscoverer(client).discover()


@pytest.fixture
def app_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> pytest.LogCaptureFixture:
    # 다른 테스트가 import한 앱이 이 로거의 propagate를 꺼 둘 수 있어, 순서와 무관하게 캡처되도록 켠다.
    monkeypatch.setattr(logging.getLogger("tistory_automation"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="tistory_automation")
    return caplog


# --- 출처: 후보 안에서 쓸 수 있는 것은 살린다 -------------------------------------------------


def test_a_candidate_keeps_its_usable_sources_when_another_number_is_out_of_range() -> None:
    [topic] = discover([candidate(citation_indices=[2, 9, 1])])

    assert topic.sources == [{"title": "출처 2", "url": URLS[1]}, {"title": "출처 1", "url": URLS[0]}]


def test_unusable_urls_drop_only_their_own_source() -> None:
    citations = ["javascript:alert(1)", "https://ok.example.com/a", "http://[bad", "   ", "ftp://files.example.com/x"]

    [topic] = discover([candidate(citation_indices=[1, 2, 3, 4, 5, 2])], citations=citations)

    assert topic.sources == [{"title": "출처 2", "url": "https://ok.example.com/a"}]


def test_a_candidate_with_no_usable_source_is_still_rejected() -> None:
    with pytest.raises(TopicDiscoveryError, match=r"no usable sources x3"):
        discover([candidate(citation_indices=[]), candidate(2, citation_indices=[99]), candidate(3, citation_indices=None)])


def test_a_citation_number_below_one_rejects_the_candidate_because_the_other_numbers_are_probably_shifted() -> None:
    with pytest.raises(TopicDiscoveryError, match=r"citation number below 1 x2"):
        discover([candidate(citation_indices=[0, 2]), candidate(2, citation_indices=[2, -1])])

    kept = discover([candidate(1, citation_indices=[0, 2]), candidate(2, citation_indices=[2])])

    assert [topic.topic for topic in kept] == ["주제 2번 후보"]


def test_numbers_written_as_text_or_as_a_bare_number_are_understood_and_junk_entries_are_ignored() -> None:
    junk = [" 2 ", "1", True, 2.5, None, {"n": 1}, "abc", "9" * 5000]

    [mixed] = discover([candidate(citation_indices=junk)])
    [bare] = discover([candidate(2, citation_indices=3)])

    assert [source["url"] for source in mixed.sources] == [URLS[1], URLS[0]]
    assert [source["url"] for source in bare.sources] == [URLS[2]]
    with pytest.raises(TopicDiscoveryError, match=r"no usable sources x1"):
        discover([candidate(citation_indices=[True])])  # JSON의 true는 1번 출처가 아니다


def test_search_result_urls_are_used_when_the_citation_list_is_empty() -> None:
    results = [{"url": URLS[0], "title": "첫째"}, {"title": "URL 없음"}, {"url": URLS[2], "title": "셋째"}]
    items = [candidate(1, citation_indices=[3]), candidate(2, citation_indices=[2]), candidate(3, citation_indices=[1, 2])]

    found = discover(items, citations=[], search_results=results)

    # URL이 없는 2번 결과가 번호를 당기지 않는다: 3번은 여전히 세 번째 결과이고, 2번만 인용한 후보는 쓸 출처가 없다.
    assert [(topic.topic, topic.sources) for topic in found] == [
        ("주제 1번 후보", [{"title": "셋째", "url": URLS[2]}]),
        ("주제 3번 후보", [{"title": "첫째", "url": URLS[0]}]),
    ]


# --- 카테고리·주제 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "정부지원/민원",
        "정부지원 · 민원",
        "정부지원ㆍ민원",  # U+318D: 정규화하면 한글 모음으로 바뀌는 점
        "정부지원・민원",
        "정부지원･민원",  # 반각 가타카나 가운뎃점
        "정부지원／민원",  # 전각 슬래시: 정규화한 뒤에야 구분 기호가 된다
        "정부지원，민원",
        " 정부지원·민원 ",
        "정부지원 민원",
    ],
)
def test_category_spelling_variants_map_to_the_allowed_name(written: str) -> None:
    [topic] = discover([candidate(category=written)])

    assert topic.category == "정부지원·민원"


@pytest.mark.parametrize("written", ["경제", "", None, 7, "정부지원 및 민원", "생활"])
def test_other_categories_are_rejected_not_guessed(written: object) -> None:
    with pytest.raises(TopicDiscoveryError, match=r"unknown category x1"):
        discover([candidate(category=written)])


def test_invisible_characters_are_removed_from_topics_and_reasons() -> None:
    hidden = chr(0x202E) + chr(0x200B) + chr(0xE0041)

    [topic] = discover([candidate(topic=f"등본{hidden} 발급", reason=f"이유{hidden}")])

    assert topic.topic == "등본 발급" and topic.reason == "이유"


def test_topic_length_limit_fits_the_database_column() -> None:
    assert MAX_TOPIC_CHARS < TopicCandidate.__table__.c.topic.type.length

    assert len(discover([candidate(topic="가" * MAX_TOPIC_CHARS)])) == 1
    with pytest.raises(TopicDiscoveryError, match=r"topic too long x1"):
        discover([candidate(topic="가" * (MAX_TOPIC_CHARS + 1))])


# --- 개수 상한 -------------------------------------------------------------------------------


def test_the_cap_counts_valid_candidates_only() -> None:
    invalid = [candidate(n, category="경제") for n in range(1, 9)]
    valid = [candidate(n) for n in range(9, 24)]

    found = discover(invalid + valid)

    assert MAX_CANDIDATES == 12 and len(found) == 12
    assert [topic.topic for topic in found] == [f"주제 {n}번 후보" for n in range(9, 21)]


def test_only_the_first_raw_candidates_are_examined() -> None:
    invalid = [candidate(n, category="경제") for n in range(1, MAX_RAW_CANDIDATES + 1)]

    with pytest.raises(TopicDiscoveryError, match=rf"unknown category x{MAX_RAW_CANDIDATES}"):
        discover(invalid + [candidate(999)])


# --- JSON 복구 -------------------------------------------------------------------------------


def test_json_cut_off_mid_candidate_keeps_the_complete_ones(app_log: pytest.LogCaptureFixture) -> None:
    full = json.dumps({"candidates": [candidate(1), candidate(2), candidate(3)]}, ensure_ascii=False)
    cut = full[: full.rindex('{"topic"') + 25]

    found = discover(cut, finish_reason="length")

    assert [topic.topic for topic in found] == ["주제 1번 후보", "주제 2번 후보"]
    assert "recovered 2 complete candidates" in app_log.text and "finish_reason=length" in app_log.text


def test_json_cut_off_before_any_complete_candidate_says_so() -> None:
    cut = '{"candidates": [{"topic": "주제 1번 후'

    with pytest.raises(TopicDiscoveryError, match=r"^Sonar topic response is not valid JSON \(cut off by the output token limit\)$"):
        discover(cut, finish_reason="length")
    with pytest.raises(TopicDiscoveryError, match=r"^Sonar topic response is not valid JSON$"):
        discover(cut)


def test_json_wrapped_in_prose_or_fences_is_read() -> None:
    body = json.dumps({"candidates": [candidate(1), candidate(2)]}, ensure_ascii=False)
    wrapped = (
        f"```json\n{body}\n```",
        f"결과입니다.\n```json\n{body}\n```\n도움이 되길 바랍니다.",
        f"{body}\n\n참고: 위 후보는 최근 7일 기사를 바탕으로 했습니다.",
    )

    for content in wrapped:
        assert [topic.topic for topic in discover(content)] == ["주제 1번 후보", "주제 2번 후보"], content


def test_a_bare_candidate_array_is_accepted() -> None:
    assert len(discover(json.dumps([candidate(1)], ensure_ascii=False))) == 1


def test_empty_or_missing_content_is_a_discovery_error_not_a_crash() -> None:
    for content in ("", "   \n"):
        with pytest.raises(TopicDiscoveryError, match="is empty"):
            discover(content)

    client = create_autospec(PerplexityClient, instance=True)
    client.completion_response.return_value = PerplexityCompletion(content=None, citations=[], search_results=[])  # type: ignore[arg-type]
    with pytest.raises(TopicDiscoveryError, match="is empty"):
        TopicDiscoverer(client).discover()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("123", "must be an object"),
        ('{"topics": []}', "has no candidates"),
        ('{"candidates": "x"}', "has no candidates"),
        ("not json at all", "not valid JSON"),
        pytest.param("[" * 100_000, "not valid JSON", id="nested-past-the-recursion-limit"),  # RecursionError로 새지 않는다
        ('{"candidates": []}', "no valid topic candidates"),
    ],
)
def test_unusable_responses_raise_a_discovery_error(content: str, message: str) -> None:
    with pytest.raises(TopicDiscoveryError, match=message):
        discover(content)


# --- 폐기 사유 -------------------------------------------------------------------------------


def test_the_error_lists_why_candidates_were_dropped() -> None:
    items = [
        candidate(1, category="경제"),
        candidate(2, category="경제"),
        candidate(3, citation_indices=[]),
        "문자열",
        None,
        candidate(5, topic=""),
        candidate(6, reason=" "),
        candidate(7, topic="가" * (MAX_TOPIC_CHARS + 1)),
    ]

    with pytest.raises(TopicDiscoveryError) as error:
        discover(items)

    assert str(error.value) == (
        "Sonar returned no valid topic candidates (dropped: unknown category x2, no usable sources x1, "
        "not an object x2, missing topic x1, missing reason x1, topic too long x1)"
    )


def test_dropped_candidates_are_logged_when_others_survive(app_log: pytest.LogCaptureFixture) -> None:
    found = discover([candidate(1), candidate(2, category="경제"), candidate(3, citation_indices=[99]), candidate(1)])

    assert len(found) == 1
    assert "kept 1 candidates; dropped: unknown category x1, no usable sources x1, duplicate topic x1" in app_log.text


def test_nothing_is_logged_when_every_candidate_is_kept(app_log: pytest.LogCaptureFixture) -> None:
    discover([candidate(1), candidate(2)])

    assert app_log.text == ""


# --- 실제 응답 구조 --------------------------------------------------------------------------


def test_a_recorded_agent_api_response_flows_through_discovery() -> None:
    recorded = parse_agent_response(json.loads(FIXTURE.read_text(encoding="utf-8")))
    client = create_autospec(PerplexityClient, instance=True)
    client.completion_response.return_value = recorded

    found = TopicDiscoverer(client).discover()

    assert [(topic.category, [source["url"] for source in topic.sources]) for topic in found] == [
        ("대출·금융", ["https://news.example.com/article/1", "https://news.example.com/article/3"]),
        ("세금·환급", ["https://news.example.com/article/2"]),
    ]
