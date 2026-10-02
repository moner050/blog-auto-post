from __future__ import annotations

import json
import time

import pytest

from app.llm.parsing import (
    MAX_SUMMARY_INPUT_CHARS,
    MAX_TITLE_CHARS,
    ArticleParseError,
    ArticleTruncatedError,
    count_emoji,
    derive_tags,
    format_article_block,
    normalize_summary,
    normalize_tags,
    normalize_title,
    parse_article,
    strip_emoji,
)

TAGGED = (
    "<article_title>등본 발급 방법</article_title>\n"
    "<article_summary>요약문입니다.</article_summary>\n"
    "<article_tags>정부24, 등본</article_tags>\n"
    "<article_body>\n<p>본문</p>\n</article_body>"
)


def test_parse_tagged_output() -> None:
    parsed = parse_article(TAGGED)

    assert parsed.title == "등본 발급 방법"
    assert parsed.summary == "요약문입니다."
    assert parsed.tags == ["정부24", "등본"]
    assert parsed.body_html == "<p>본문</p>"


def test_parse_is_case_insensitive_and_ignores_chatter_around_the_tags() -> None:
    text = "알겠습니다. 작성했어요!\n\n" + TAGGED.replace("article_", "ARTICLE_") + "\n\n도움이 되었길 바랍니다."

    parsed = parse_article(text)

    assert parsed.title == "등본 발급 방법"
    assert parsed.body_html == "<p>본문</p>"


def test_parse_unwraps_a_fenced_answer() -> None:
    parsed = parse_article(f"```xml\n{TAGGED}\n```")

    assert parsed.title == "등본 발급 방법" and parsed.body_html == "<p>본문</p>"


def test_field_extraction_keeps_the_first_open_to_first_close_semantics() -> None:
    text = "<article_title>첫 <article_title>둘</article_title>끝</article_title><article_body><p>본문</p></article_body>"

    assert parse_article(text).title == "첫 <article_title>둘"
    assert parse_article("<article_title>제목</article_title>\n\n<ARTICLE_BODY>x</ARTICLE_BODY>").title == "제목"
    with pytest.raises(ArticleParseError, match="제목"):
        parse_article("<article_title>닫히지 않은 제목<article_body>x</article_body>")


def test_repeated_open_tags_without_closers_do_not_blow_up() -> None:
    import time

    started = time.perf_counter()
    for text in ("<article_title>" * 20_000, "<article_summary>" * 20_000 + "<article_body>x</article_body>"):
        with pytest.raises(ArticleParseError):
            parse_article(text)
    assert time.perf_counter() - started < 3

    started = time.perf_counter()
    assert normalize_summary("<" * 400_000) == "<" * MAX_SUMMARY_INPUT_CHARS  # '>'가 없으면 태그가 아니므로 그대로 두되 상한까지만
    assert normalize_title("<" * 400_000 + "제목") == "<" * MAX_TITLE_CHARS  # 길이 상한까지만 남긴다
    assert normalize_tags(["<" * 400_000]) == []  # 태그 길이 상한(30자) 초과로 버려진다
    assert len(normalize_tags(",".join(f"t{n}" for n in range(200_000)))) == 10  # 입력 상한 + 태그 개수 상한
    assert time.perf_counter() - started < 3


def test_titles_are_capped_to_the_database_column_width() -> None:
    assert len(normalize_title("가" * 1000)) == MAX_TITLE_CHARS == 255
    assert normalize_title("가" * 250 + " 나다") == "가" * 250 + " 나다"


def test_summary_and_tags_are_optional_but_title_and_body_are_not() -> None:
    parsed = parse_article("<article_title>제목</article_title><article_body><p>본문</p></article_body>")
    assert (parsed.summary, parsed.tags) == ("", [])

    with pytest.raises(ArticleParseError, match="제목"):
        parse_article("<article_body><p>본문</p></article_body>")
    with pytest.raises(ArticleParseError, match="본문"):
        parse_article("<article_title>제목</article_title><article_body>  </article_body>")


def test_missing_closing_body_tag_is_accepted_unless_the_response_was_truncated() -> None:
    text = "<article_title>제목</article_title>\n<article_body>\n<p>본문 끝까지</p>"

    assert parse_article(text).body_html == "<p>본문 끝까지</p>"
    with pytest.raises(ArticleTruncatedError, match="길이 제한"):
        parse_article(text, truncated=True)


def test_complete_response_is_accepted_even_when_finish_reason_says_length() -> None:
    assert parse_article(TAGGED, truncated=True).body_html == "<p>본문</p>"


def test_json_fallback_for_models_that_ignore_the_tag_format() -> None:
    payload = {"title": "JSON 제목[1]", "body_html": "<p>본문[2]</p>", "tags": ["a", "b"], "summary": "요약"}

    parsed = parse_article("물론이죠!\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")

    assert (parsed.title, parsed.body_html, parsed.tags, parsed.summary) == ("JSON 제목[1]", "<p>본문[2]</p>", ["a", "b"], "요약")


def test_json_fallback_accepts_alternative_keys_and_comma_separated_tags() -> None:
    parsed = parse_article(json.dumps({"title": "제목", "content": "<p>본문</p>", "tags": "가, 나"}, ensure_ascii=False))

    assert parsed.body_html == "<p>본문</p>"
    assert parsed.tags == ["가", "나"]


def test_unparseable_or_truncated_json_raises_the_right_error() -> None:
    with pytest.raises(ArticleParseError, match="해석하지 못했습니다"):
        parse_article("그냥 잡담입니다")
    with pytest.raises(ArticleParseError):
        parse_article('{"title": "제목", "body_html": "<p>잘린')
    with pytest.raises(ArticleTruncatedError):
        parse_article('{"title": "제목", "body_html": "<p>잘린', truncated=True)


def test_format_article_block_round_trips() -> None:
    block = format_article_block("제목", "요약", ["가", "나"], "<p>본문</p>", (1, 3))

    parsed = parse_article(block)

    assert (parsed.title, parsed.summary, parsed.body_html) == ("제목", "요약", "<p>본문</p>")
    assert normalize_tags(parsed.tags) == ["가", "나"]
    assert parsed.source_ids == (1, 3)
    assert "<article_sources>" not in format_article_block("제목", "요약", ["가"], "<p>본문</p>")


def test_source_ids_are_parsed_in_order_without_duplicates_or_zero() -> None:
    text = "<article_title>제목</article_title><article_sources>3, 1; 3 / 0, 12 abc 7</article_sources><article_body><p>본문</p></article_body>"

    assert parse_article(text).source_ids == (3, 1, 12, 7)
    assert parse_article(TAGGED).source_ids == ()
    many = ", ".join(str(number) for number in range(1, 50))
    assert len(parse_article(f"<article_title>제목</article_title><article_sources>{many}</article_sources><article_body>x</article_body>").source_ids) == 20


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<b>등본 발급</b> 방법[1]", "등본 발급 방법"),
        ("&quot;모르면 손해&quot; 대출 규제", '"모르면 손해" 대출 규제'),
        ("## 등본 발급 방법", "등본 발급 방법"),
        ("💡 등본 발급 방법 ✅", "등본 발급 방법"),
        ("지금 확인하세요!!! 정말이에요??", "지금 확인하세요! 정말이에요?"),
        ('"등본 발급 방법"', "등본 발급 방법"),
        ("“등본 발급 방법”", "등본 발급 방법"),
        ('"모르면 손해" 등본 발급', '"모르면 손해" 등본 발급'),
        ("  등본   발급\n방법  ", "등본 발급 방법"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-5도 한파에도 난방비 아끼는 법 7가지", "-5도 한파에도 난방비 아끼는 법 7가지"),  # 부호는 제목의 일부다
        ("#1 추천 방법", "#1 추천 방법"),
        ("*필독* 연말정산 놓치면 손해", "필독 연말정산 놓치면 손해"),
        ("**굵은** 제목", "굵은 제목"),
        ("- 항목 제목", "항목 제목"),
        ("> 인용 제목", "인용 제목"),
        ("'따옴표' 제목 '끝'", "'따옴표' 제목 '끝'"),  # 서로 다른 쌍의 따옴표는 벗기지 않는다
        ('"연말정산" 환급 "꿀팁"', '"연말정산" 환급 "꿀팁"'),
        ("별점 ★★★★★ 후기", "별점 ★★★★★ 후기"),  # 일반 기호는 이모지가 아니다
        ("추천 ♥ 방법 ✓ 확인", "추천 ♥ 방법 ✓ 확인"),
    ],
)
def test_normalize_title_keeps_symbols_that_belong_to_the_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_model_text_fields_lose_invisible_characters() -> None:
    rlo, zwsp, tag = chr(0x202E), chr(0x200B), chr(0xE0041)

    assert normalize_title(f"제{rlo}목{zwsp}입니다{tag}") == "제목입니다"
    assert normalize_title("&#8238;숨은 방향 제어") == "숨은 방향 제어"
    assert normalize_summary(f"요{rlo}약{tag}") == "요약"
    assert normalize_tags([f"태{zwsp}그"]) == ["태그"]


def test_normalize_summary_strips_markup_markers_and_whitespace() -> None:
    assert normalize_summary("<p>정부24&amp;홈택스[1]  안내\n입니다.</p>") == "정부24&홈택스 안내 입니다."


def test_normalize_tags_splits_dedupes_and_limits() -> None:
    assert normalize_tags("정부24, #등본 ,정부24|  발급 방법\n<b>민원</b>;") == ["정부24", "등본", "발급 방법", "민원"]
    assert normalize_tags(["A", "a", "B"]) == ["A", "B"]
    assert normalize_tags(["x" * 31, "ok"]) == ["ok"]
    assert len(normalize_tags([f"태그{n}" for n in range(30)])) == 10
    assert normalize_tags(None) == []


def test_derive_tags_uses_topic_keywords_and_category() -> None:
    tags = derive_tags("모르면 손해! 주민등록등본 발급 방법 총정리", "정부지원·민원")

    assert tags == ["주민등록등본", "발급", "정부지원·민원"]
    assert derive_tags("a b") == []


def test_emoji_helpers_ignore_variation_selectors_and_korean_punctuation() -> None:
    text = "💡 팁 ⚠️ 주의 ✅ 핵심 ※ 참고 → 이동 · 점"

    assert count_emoji(text) == 3
    assert strip_emoji("⚠️ 주의") == " 주의"


def test_emoji_helpers_count_real_emoji_and_not_ordinary_symbols() -> None:
    assert count_emoji("💡 ✅ ⚠️ 📌") == 4
    assert count_emoji("✔️ 확인 ❤️ ➡️ ⭐ 1️⃣ 🇰🇷") == 7  # 이모지 표시 선택자가 붙은 글자, 기본이 이모지인 기호, 키캡, 국기(2글자)
    assert count_emoji("★★★★★ ♥ ✓ ✔ ⌘ ☎ ※ → ← ↔") == 0


def test_derive_tags_only_reads_the_start_of_a_huge_topic() -> None:
    many_distinct = " ".join(f"단어{n}" for n in range(60_000))  # 서로 다른 단어 6만 개(약 40만 자)
    huge = "단어 " + "다른 " * 4_000_000  # 약 14MB
    started = time.perf_counter()

    assert derive_tags(many_distinct) == ["단어0", "단어1", "단어2", "단어3", "단어4"]
    assert derive_tags(huge) == ["단어", "다른"]
    assert time.perf_counter() - started < 0.5


def test_deeply_nested_json_is_a_parse_error_not_a_crash() -> None:
    with pytest.raises(ArticleParseError):
        parse_article('{"a":' * 100_000)


def test_the_json_probe_gives_up_after_a_bounded_number_of_opening_braces() -> None:
    payload = json.dumps({"title": "제목", "body_html": "<p>본문</p>"}, ensure_ascii=False)

    assert parse_article("{ " * 5 + payload).title == "제목"
    with pytest.raises(ArticleParseError):
        parse_article("{ " * 80 + payload)  # 앞의 80개 중괄호가 모두 JSON이 아니다: 뒤의 진짜 JSON까지 뒤지지 않는다


def test_an_outer_code_fence_is_not_left_in_the_body_when_the_closing_body_tag_is_missing() -> None:
    text = "```html\n<article_title>제목</article_title>\n<article_body>\n<p>본문</p>\n```"

    assert parse_article(text).body_html == "<p>본문</p>"
