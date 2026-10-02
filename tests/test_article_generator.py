from __future__ import annotations

from datetime import date, datetime, timezone
import http.client
import json
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.llm import generator as generator_module
from app.llm.client import PerplexityAPIError
from app.llm.generator import ArticleGenerator
from app.llm.parsing import ArticleParseError, ArticleTruncatedError
from tests.article_helpers import DEFAULT_TITLE, OFFICIAL_URL, SEARCH_RESULTS, FakeClient, make_article, make_body

TODAY = date(2026, 10, 1)
TOPIC = "주민등록등본 발급 방법"
BAD_TITLE = "등본 발급 70% 달라졌다? 정부24에서 신청하는 방법 정리"


def make_generator(rules_path: Path, client: FakeClient, **overrides: object) -> ArticleGenerator:
    settings = Settings(
        _env_file=None,
        perplexity_api_key="pplx-valid-key",
        article_style_rules_path=rules_path,
        **overrides,
    )
    return ArticleGenerator(settings, client=client)  # type: ignore[arg-type]


def bad_article() -> str:
    """제목 숫자 근거 없음 + 본문 짧음 + 제한 표현 3회 + 지어낸 경험담 + 위험 마크업이 한 번에 걸리는 초안."""
    extra = (
        "<p>제가 직접 써봤는데 대박 무조건 대박이에요. "
        "<a href='https://www.gov.kr/portal/made-up/999'>바로가기</a><script>alert(1)</script></p>"
        "<table><tr><th>구분</th><td>내용</td></tr></table>"  # 정제기가 스타일을 입히는 요소: 재작성 요청에서는 걷어내야 한다
    )
    return make_article(title=BAD_TITLE, body=make_body(h2=3, paragraphs_per_h2=1, extra=extra))


def test_clean_draft_needs_one_call_and_gets_a_sources_footer(mini_rules_path: Path) -> None:
    client = FakeClient(make_article())

    article = make_generator(mini_rules_path, client).generate(TOPIC, category="정부지원·민원", today=TODAY)

    assert len(client.calls) == 1 and article.revisions == 0 and article.warnings == []
    assert (article.mode, article.blueprint) == ("lifestyle", "how_to")
    assert article.title == DEFAULT_TITLE
    assert article.tags == ["정부24", "주민등록등본", "등본발급", "민원24", "발급방법"]
    assert article.body_html.count("<h2>") == 5  # 본문 4개 + '참고한 자료'
    assert "<h2>참고한 자료</h2>" in article.body_html
    assert [source["url"] for source in article.sources] == [OFFICIAL_URL, "https://www.yna.co.kr/view/AKR1"]
    assert "blog.naver.com" not in article.body_html
    assert "2026년 10월 1일 기준으로 확인한 내용이에요." in article.body_html


def test_footer_lists_only_the_sources_the_model_says_it_used(mini_rules_path: Path) -> None:
    client = FakeClient(make_article(sources="3"))

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert [source["url"] for source in article.sources] == ["https://www.yna.co.kr/view/AKR1"]
    assert "정부24 주민등록등본 발급 안내" not in article.body_html


@pytest.mark.parametrize("sources", ["", "99", "2"])
def test_footer_falls_back_to_this_searchs_usable_results_when_citations_are_missing_invalid_or_ugc(
    mini_rules_path: Path, sources: str
) -> None:
    article = make_generator(mini_rules_path, FakeClient(make_article(sources=sources))).generate(TOPIC, today=TODAY)

    assert [source["url"] for source in article.sources] == [OFFICIAL_URL, "https://www.yna.co.kr/view/AKR1"]


def test_the_footer_cap_keeps_official_sources_even_when_the_search_lists_them_last(mini_rules_path: Path) -> None:
    results = [{"id": n, "title": f"뉴스 {n}", "url": f"https://news.example.com/{n}"} for n in range(1, 9)]
    results.append({"id": 9, "title": "정부24 안내", "url": OFFICIAL_URL})
    client = FakeClient(make_article(sources=""), search_results=results)  # 근거 번호를 밝히지 않아 검색 결과 전체에서 고른다

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert len(article.sources) == 6 and article.sources[0]["url"] == OFFICIAL_URL
    assert article.body_html.count("<li>") == 6 + 2  # 푸터 6개 + 본문 목록 2개


def test_at_most_eight_seed_sources_are_kept_and_unusable_ones_are_dropped(mini_rules_path: Path) -> None:
    seed = [{"title": f"출처 {n}", "url": f"https://news.example.com/{n}"} for n in range(12)]
    seed.insert(0, {"title": "블로그", "url": "https://blog.naver.com/a/1"})

    request = make_generator(mini_rules_path, FakeClient()).prepare(TOPIC, sources=seed, reason="이유", today=TODAY).request

    assert [source.title for source in request.sources] == [f"출처 {n}" for n in range(8)]


def test_a_discovery_topic_keeps_its_title_while_a_typed_keyword_gets_a_new_hook(mini_rules_path: Path) -> None:
    generator = make_generator(mini_rules_path, FakeClient())
    headline = "DSR 규제한다더니 신규 대출 70%가 예외?"

    with_reason = generator.prepare(headline, reason="통계가 나왔다", today=TODAY)
    with_sources = generator.prepare(headline, sources=[{"title": "기사", "url": "https://seed.example.com/a"}], today=TODAY)
    unusable_sources = generator.prepare(headline, sources=[{"title": "위험", "url": "javascript:alert(1)"}], today=TODAY)
    typed = generator.prepare(TOPIC, today=TODAY)

    assert with_reason.request.title_seed and with_sources.request.title_seed
    assert not unusable_sources.request.title_seed and not typed.request.title_seed
    assert "3) 제목은 [주제] 문장을 그대로 쓴다" in with_reason.user
    assert "3) 제목은 클릭을 부르는 형태로 새로 만든다" in typed.user


def test_topic_discovery_sources_inform_the_prompt_and_links_but_never_the_footer(mini_rules_path: Path) -> None:
    seed = [{"title": "탐색 때 출처", "url": "https://seed.example.com/news/1"}]
    body = make_body(extra="<p><a href='https://seed.example.com/news/1'>탐색 출처 기사</a></p>")
    # 근거 번호를 밝히지 않아 푸터가 검색 결과 전체에서 고르는 경로다(여기서 시드 출처가 섞이면 안 된다).
    client = FakeClient(make_article(body=body, sources=""))

    article = make_generator(mini_rules_path, client).generate(TOPIC, sources=seed, today=TODAY)

    assert "https://seed.example.com/news/1" in client.calls[0]["messages"][1]["content"]
    assert 'href="https://seed.example.com/news/1"' in article.body_html  # 본문 링크로는 허용
    assert "탐색 때 출처" not in article.body_html and all("seed.example.com" not in s["url"] for s in article.sources)
    assert article.body_html.count("seed.example.com") == 1


def test_request_context_and_settings_reach_the_model(mini_rules_path: Path) -> None:
    client = FakeClient(make_article())
    generator = make_generator(
        mini_rules_path, client, article_model="low", article_temperature=0.6, article_max_tokens=5000
    )

    generator.generate(
        TOPIC,
        category="정부지원·민원",
        reason="온라인 발급 문의가 많음",
        sources=[
            {"title": "정부24 안내", "url": OFFICIAL_URL},
            {"title": "위험", "url": "javascript:alert(1)"},
            {"title": "블로그", "url": "https://blog.naver.com/a/1"},
        ],
        today=TODAY,
    )

    call = client.calls[0]
    system, user = (message["content"] for message in call["messages"])
    assert "미니 페르소나 지시문" in system and "[출력 형식]" in system
    for expected in (
        "[주제] 주민등록등본 발급 방법",
        "[카테고리] 정부지원·민원",
        "2026년 10월 1일",
        "온라인 발급 문의가 많음",
        f"1. 정부24 안내 — {OFFICIAL_URL}",
    ):
        assert expected in user
    assert "javascript" not in user and "blog.naver.com" not in user
    assert (call["model"], call["temperature"], call["max_tokens"]) == ("low", 0.6, 5000)


def test_blank_model_setting_means_the_client_default(mini_rules_path: Path) -> None:
    client = FakeClient(make_article())

    make_generator(mini_rules_path, client, article_model="  ").generate(TOPIC, today=TODAY)

    assert client.calls[0]["model"] is None


def test_revision_keeps_the_drafts_source_numbers(mini_rules_path: Path) -> None:
    bad = make_article(title=BAD_TITLE, body=make_body(h2=3, paragraphs_per_h2=1), sources="1, 3")
    client = FakeClient(bad, make_article(sources="1, 3"))

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert "<article_sources>1, 3</article_sources>" in client.calls[1]["messages"][1]["content"]
    assert "<article_sources> 줄은 바꾸지 않고 그대로 둔다" in client.calls[1]["messages"][1]["content"]
    assert article.revisions == 1 and len(article.sources) == 2


def test_blocking_issues_trigger_one_revision_that_fixes_them(mini_rules_path: Path) -> None:
    client = FakeClient(bad_article(), make_article())
    progress: list[str] = []

    article = make_generator(mini_rules_path, client).generate(TOPIC, on_progress=progress.append, today=TODAY)

    assert len(client.calls) == 2 and progress == ["write", "revise"] and article.revisions == 1
    assert article.title == DEFAULT_TITLE and article.warnings == []
    revision_user = client.calls[1]["messages"][1]["content"]
    assert "'70'" in revision_user and "제한 표현이 3회" in revision_user and "직접 겪은 경험" in revision_user
    assert f"<article_title>{BAD_TITLE}</article_title>" in revision_user
    assert "<script" not in revision_user and "made-up" not in revision_user
    assert "<table>" in revision_user and 'style="' not in revision_user and "overflow-x" not in revision_user
    assert client.calls[1]["messages"][0] == client.calls[0]["messages"][0]


def test_revision_that_is_not_better_keeps_the_draft_and_reports_what_is_left(mini_rules_path: Path) -> None:
    client = FakeClient(bad_article(), bad_article())

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert article.revisions == 1 and article.title == BAD_TITLE
    assert any("나아지지 않아" in warning for warning in article.warnings)
    assert any("숫자 '70'" in warning for warning in article.warnings)


@pytest.mark.parametrize(
    "second",
    [
        PerplexityAPIError("서버 오류", status_code=502),
        "형식을 무시한 잡담",
        ("<article_title>제목</article_title><article_body><p>잘림", "length"),
        # 예상하지 못한 오류도 이미 비용을 낸 초안을 버리게 해서는 안 된다(수정은 선택 단계다).
        http.client.IncompleteRead(b"par", 100),
        RecursionError("maximum recursion depth exceeded"),
        TypeError("'int' object is not iterable"),
        RuntimeError("뜻밖의 오류"),
    ],
)
def test_revision_failures_keep_the_draft_with_a_warning(mini_rules_path: Path, second: object) -> None:
    client = FakeClient(bad_article(), second)

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert article.revisions == 0 and article.title == BAD_TITLE
    assert any("수정 요청에 실패해 초안을 그대로 사용했습니다" in warning for warning in article.warnings)


def test_a_revision_that_drops_article_sources_inherits_the_drafts_numbers(mini_rules_path: Path) -> None:
    draft = make_article(title=BAD_TITLE, body=make_body(h2=3, paragraphs_per_h2=1), sources="1")
    revised = make_article(sources="")  # 모델이 <article_sources> 줄을 빠뜨렸다
    client = FakeClient(draft, revised)

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert article.revisions == 1 and article.title == DEFAULT_TITLE
    assert [source["url"] for source in article.sources] == [OFFICIAL_URL]  # 검색 결과 전체로 넓어지지 않는다
    assert not any("근거 출처 번호" in warning for warning in article.warnings)


def test_missing_article_sources_are_reported_when_the_footer_falls_back_to_all_results(mini_rules_path: Path) -> None:
    article = make_generator(mini_rules_path, FakeClient(make_article(sources=""))).generate(TOPIC, today=TODAY)

    assert any("근거 출처 번호(<article_sources>)를 확인하지 못해" in warning for warning in article.warnings)
    assert len(article.sources) == 2


def test_the_report_says_so_when_there_is_nothing_to_list_at_all(mini_rules_path: Path) -> None:
    client = FakeClient(make_article(sources=""), search_results=[])

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert article.sources == [] and "<h2>참고한 자료</h2>" not in article.body_html
    assert any("검색 결과도 없습니다" in warning for warning in article.warnings)


def test_revisions_can_be_disabled(mini_rules_path: Path) -> None:
    client = FakeClient(bad_article())

    article = make_generator(mini_rules_path, client, article_max_revisions=0).generate(TOPIC, today=TODAY)

    assert len(client.calls) == 1 and article.revisions == 0
    assert any("제한 표현" in warning for warning in article.warnings)


def test_write_failures_propagate(mini_rules_path: Path) -> None:
    def generate(*responses: object, topic: str = TOPIC) -> None:
        make_generator(mini_rules_path, FakeClient(*responses)).generate(topic, today=TODAY)

    with pytest.raises(PerplexityAPIError, match="다운"):
        generate(PerplexityAPIError("다운"))
    with pytest.raises(ArticleTruncatedError):
        generate(("<article_title>제목</article_title><article_body><p>잘림", "length"))
    with pytest.raises(ArticleParseError, match="해석하지 못했습니다"):
        generate("그냥 잡담")
    with pytest.raises(ArticleParseError, match="본문이 비어"):
        generate(make_article(body="<script>alert(1)</script>"))
    with pytest.raises(ValueError, match="주제가 비어"):
        generate(make_article(), topic="   ")


def test_html_is_sanitized_and_unverified_links_are_reported(mini_rules_path: Path) -> None:
    body = f"<h1>{DEFAULT_TITLE}</h1>" + make_body(
        extra=f"<p><a href='{OFFICIAL_URL}'>정부24</a> <a href='https://www.gov.kr/portal/made-up/999'>만든 링크</a><script>alert(1)</script></p>"
    )

    article = make_generator(mini_rules_path, FakeClient(make_article(body=body, tags=""))).generate(
        TOPIC, category="정부지원·민원", today=TODAY
    )

    assert "<script" not in article.body_html and "made-up" not in article.body_html
    assert f'href="{OFFICIAL_URL}"' in article.body_html and "만든 링크" in article.body_html
    assert DEFAULT_TITLE not in article.body_html
    assert any("검증되지 않은 링크 1개" in warning for warning in article.warnings)
    assert any("제목(h1)" in warning for warning in article.warnings)
    assert article.tags == ["주민등록등본", "발급", "정부지원·민원"]


def test_links_to_sites_that_are_not_official_nor_cited_are_flagged_for_a_human_to_check(mini_rules_path: Path) -> None:
    body = make_body(extra="<p><a href='https://www.yna.co.kr/view/AKR1'>기사</a> <a href='https://www.gov.kr/'>정부24</a></p>")

    uncited = make_generator(mini_rules_path, FakeClient(make_article(body=body, sources="1"))).generate(TOPIC, today=TODAY)
    cited = make_generator(mini_rules_path, FakeClient(make_article(body=body, sources="1, 3"))).generate(TOPIC, today=TODAY)

    assert any("공식 기관이 아닌 외부 사이트 링크가 있습니다(yna.co.kr)" in warning for warning in uncited.warnings)
    assert not any("외부 사이트 링크" in warning for warning in cited.warnings)  # 글의 근거로 밝힌 출처의 사이트, 그리고 gov.kr


def test_user_generated_sites_cannot_be_linked_even_when_they_appear_in_the_search_results(mini_rules_path: Path) -> None:
    body = make_body(extra="<p><a href='https://blog.naver.com/someone/1'>후기 블로그</a></p>")  # SEARCH_RESULTS의 2번

    article = make_generator(mini_rules_path, FakeClient(make_article(body=body))).generate(TOPIC, today=TODAY)

    assert "blog.naver.com" not in article.body_html and "후기 블로그" in article.body_html
    assert any("검증되지 않은 링크 1개" in warning for warning in article.warnings)


def test_a_link_verified_only_by_the_revision_search_is_allowed(mini_rules_path: Path) -> None:
    extra_result = {"id": 9, "title": "수정 때 찾은 기사", "url": "https://revision-only.example.com/a"}
    revised_body = make_body(extra=f"<p><a href='{extra_result['url']}'>수정 중 찾은 기사</a></p>")
    client = FakeClient(
        bad_article(),
        make_article(body=revised_body),
        results_by_call={1: [*SEARCH_RESULTS, extra_result]},
    )

    article = make_generator(mini_rules_path, client).generate(TOPIC, today=TODAY)

    assert article.revisions == 1 and f'href="{extra_result["url"]}"' in article.body_html


def test_legacy_json_output_is_still_understood_and_citation_markers_are_removed(mini_rules_path: Path) -> None:
    payload = {
        "title": "주민등록등본 발급방법[1] 정부24에서 5분 만에 끝내는 순서",
        "body_html": make_body() + "<p>정부24[9]에서 신청하면 됩니다.[10]</p>",
        "tags": ["정부24", "등본"],
    }

    article = make_generator(mini_rules_path, FakeClient(json.dumps(payload, ensure_ascii=False))).generate(TOPIC, today=TODAY)

    assert article.title == "주민등록등본 발급방법 정부24에서 5분 만에 끝내는 순서"
    assert "정부24에서 신청하면 됩니다." in article.body_html
    assert not any(marker in article.body_html for marker in ("[1]", "[9]", "[10]"))
    assert article.tags == ["정부24", "등본"]


def test_progress_callback_failures_never_break_generation(mini_rules_path: Path) -> None:
    def boom(stage: str) -> None:
        raise RuntimeError("heartbeat failed")

    article = make_generator(mini_rules_path, FakeClient(make_article())).generate(TOPIC, on_progress=boom, today=TODAY)

    assert article.title == DEFAULT_TITLE


def test_today_defaults_to_korea_time(mini_rules_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return datetime(2026, 9, 30, 16, 30, tzinfo=timezone.utc).astimezone(tz)  # UTC로는 아직 9월 30일

    monkeypatch.setattr(generator_module, "datetime", FixedDatetime)
    client = FakeClient(make_article())

    article = make_generator(mini_rules_path, client).generate(TOPIC)

    assert "[오늘 날짜] 2026년 10월 1일" in client.calls[0]["messages"][1]["content"]
    assert "2026년 10월 1일 기준" in article.body_html


def test_unreadable_style_rules_fall_back_with_a_warning(tmp_path: Path) -> None:
    article = make_generator(tmp_path / "missing.yaml", FakeClient(make_article())).generate(TOPIC, today=TODAY)

    assert any("찾을 수 없습니다" in warning and "내장 기본 규칙으로 작성했습니다" in warning for warning in article.warnings)


def test_title_style_setting_reaches_the_prompt(mini_rules_path: Path) -> None:
    clickbait, persona = FakeClient(make_article()), FakeClient(make_article())

    make_generator(mini_rules_path, clickbait).generate(TOPIC, today=TODAY)
    make_generator(mini_rules_path, persona, article_title_style="persona").generate(TOPIC, today=TODAY)

    assert "클릭을 부르는 제목" in clickbait.calls[0]["messages"][0]["content"]
    assert "클릭을 부르는 제목" not in persona.calls[0]["messages"][0]["content"]
    assert "느낌표와 이모지를 쓰지 않는다" in persona.calls[0]["messages"][0]["content"]
