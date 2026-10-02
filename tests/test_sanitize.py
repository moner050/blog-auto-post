from __future__ import annotations

import re
import time

import pytest

from app.llm.sanitize import MAX_INPUT_CHARS, MAX_NESTING, sanitize_html, strip_citation_markers
from app.llm.sources import LinkPolicy, Source
from tests.html_oracle import grammar_violations

POLICY = LinkPolicy.from_sources(
    [
        Source("정부24 안내", "https://www.gov.kr/portal/service/serviceInfo/PTR000050"),
        Source("홈택스", "https://www.hometax.go.kr/"),
    ]
)

ATTACK_PAYLOADS = [
    "<script>alert(1)</script><p>본문</p>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT><p>본문</p>",
    '<p onclick="alert(1)">본문</p>',
    "<img src=x onerror=alert(1)><p>본문</p>",
    "<svg onload=alert(1)><p>본문</p></svg>",
    '<iframe src="https://evil.example"></iframe><p>본문</p>',
    '<a href="javascript:alert(1)">클릭</a>',
    '<a href=" jav&#x61;script:alert(1)">클릭</a>',
    '<a href="java\nscript:alert(1)">클릭</a>',
    '<a href="data:text/html,<script>alert(1)</script>">클릭</a>',
    '<a href="//evil.example/x">클릭</a>',
    "<style>body{display:none}</style><p>본문</p>",
    '<p style="background:url(javascript:alert(1))">본문</p>',
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math><p>본문</p>',
    "<scr<script>ipt>alert(1)</scr</script>ipt><p>본문</p>",
    "<!--><script>alert(1)</script>--><p>본문</p>",
    '<form action="https://evil.example"><input name=pw><button>전송</button></form><p>본문</p>',
    '<object data="x"></object><embed src="x"><p>본문</p>',
    '<a href="https://www.gov.kr" onclick="alert(1)">링크</a>',
]
FORBIDDEN = (
    "<script", "<img", "<svg", "<iframe", "<style", "<form", "<input", "<object", "<embed", "<math",
    "javascript:", "onerror", "onload", "onclick", "data:text", "//evil",
)


def clean(html: str, **kwargs):
    """정제하고, 어떤 입력이든 출력이 허용 문법(tests/html_oracle.py)을 벗어나지 않는지 항상 함께 검사한다."""
    result = sanitize_html(html, policy=POLICY, **kwargs)
    assert grammar_violations(result.html) == [], (html, result.html)
    return result


@pytest.mark.parametrize("payload", ATTACK_PAYLOADS)
def test_dangerous_markup_never_survives(payload: str) -> None:
    result = clean(payload)

    lowered = result.html.lower()
    for forbidden in FORBIDDEN:
        assert forbidden not in lowered, (forbidden, result.html)
    assert re.search(r"<[a-z0-9]+[^>]*\son[a-z]+\s*=", lowered) is None


def test_attack_payload_text_content_is_kept_when_harmless() -> None:
    result = clean("<script>alert(1)</script><p>본문</p>")

    assert result.html == "<p>본문</p>"
    assert result.text == "본문"


@pytest.mark.parametrize(
    "dropped",
    [
        "<style>body{display:none}</style>",
        "<script>alert(1)</script>",
        "<textarea>숨은 입력</textarea>",
        "<select><option>하나</option></select>",
        "<button>전송</button>",
        "<svg><text>x</text></svg>",
        "<math><mi>x</mi></math>",
        "<noscript>대체 내용</noscript>",
        "<template><p>숨은 템플릿</p></template>",
    ],
)
def test_elements_that_are_dropped_with_their_content_leave_no_visible_text(dropped: str) -> None:
    result = clean(f"<p>앞</p>{dropped}<p>뒤</p>")

    assert result.html == "<p>앞</p>\n<p>뒤</p>"
    assert result.text == "앞 뒤"


def test_list_items_outside_a_list_are_unwrapped() -> None:
    result = clean("<li>고아 항목</li><p>본문</p>")

    assert "<li" not in result.html and "고아 항목" in result.text


def test_a_stray_end_tag_does_not_close_unrelated_open_elements() -> None:
    assert clean("<p>가<strong>나</em>다</strong>라</p>").html == "<p>가<strong>나다</strong>라</p>"


def test_only_one_to_three_digit_numbers_in_brackets_are_citation_markers() -> None:
    assert strip_citation_markers("연도 [2026]와 항목[12]") == "연도 [2026]와 항목"


def test_allowed_link_is_rebuilt_with_safe_attributes_only() -> None:
    result = clean('<p><a href="https://www.hometax.go.kr/" onclick="x()" style="color:red" class="c">홈택스</a></p>')

    assert result.html == (
        '<p><a href="https://www.hometax.go.kr/" target="_blank" rel="noopener noreferrer">홈택스</a></p>'
    )


def test_unverified_deep_link_is_unwrapped_but_its_text_is_kept() -> None:
    url = "https://www.gov.kr/portal/service/serviceInfo/PTR999999"

    result = clean(f'<p>자세한 내용은 <a href="{url}">여기</a>를 보세요.</p>')

    assert "<a" not in result.html
    assert "자세한 내용은 여기를 보세요." in result.text
    assert result.removed_links == [url]


def test_verified_urls_and_official_roots_are_allowed() -> None:
    result = clean(
        '<p><a href="https://www.gov.kr">정부24</a> '
        '<a href="https://www.gov.kr/portal/service/serviceInfo/PTR000050/">상세</a> '
        '<a href="https://www.nhis.or.kr/">건강보험</a> '
        '<a href="https://shop.example.com/">상점</a></p>'
    )

    assert result.html.count("<a ") == 3
    assert result.removed_links == ["https://shop.example.com/"]


def test_h1_equal_to_title_is_removed_and_other_h1_becomes_h2() -> None:
    result = clean("<h1>등본 발급 방법!</h1><p>본문</p><h1>다른 제목</h1><p>끝</p>", title="등본 발급 방법")

    assert "등본 발급 방법" not in result.html
    assert "<h2>다른 제목</h2>" in result.html
    assert result.h2_count == 1
    assert any("제목(h1)" in note for note in result.notes)


def test_heading_levels_and_inline_tag_aliases() -> None:
    result = clean("<h4>소제목</h4><p><b>굵게</b> <i>기울임</i></p>")

    assert "<h3>소제목</h3>" in result.html
    assert "<strong>굵게</strong>" in result.html and "<em>기울임</em>" in result.html


def test_tables_get_scroll_wrapper_and_readable_inline_styles() -> None:
    result = clean(
        "<table><thead><tr><th scope=col colspan=2>구분</th></tr></thead>"
        "<tbody><tr><td rowspan=2 onclick=x>A</td><td>B</td></tr></tbody></table>"
    )

    assert result.html.startswith('<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;')
    assert result.html.endswith("</table></div>")
    assert 'colspan="2"' in result.html and 'rowspan="2"' in result.html and 'scope="col"' in result.html
    assert "onclick" not in result.html
    assert "rgba(127,127,127" in result.html
    assert result.table_count == 1


def test_table_cells_outside_a_table_are_unwrapped() -> None:
    result = clean("<td>고아 셀</td><p>본문</p>")

    assert "<td" not in result.html
    assert "고아 셀" in result.text


def test_citation_markers_are_removed_from_text_but_kept_in_code_blocks() -> None:
    result = clean("<p>정부24[1]에서 신청해요.[2][3] 기간은 7일[1, 2]입니다 [4-6].</p><pre><code>arr[1] = 2</code></pre>")

    assert "정부24에서 신청해요. 기간은 7일입니다." in result.text
    assert "<pre><code>arr[1] = 2</code></pre>" in result.html


def test_unclosed_and_mismatched_tags_are_repaired() -> None:
    result = clean("<ul><li>하나<li>둘</ul></p><p>끝")

    assert result.html == "<ul><li>하나</li>\n<li>둘</li>\n</ul>\n<p>끝</p>"


def test_block_elements_close_an_open_paragraph_even_through_inline_tags() -> None:
    assert clean("<p>가<strong>나<p>다").html == "<p>가<strong>나</strong></p>\n<p>다</p>"
    assert clean("<p>가<em><a href='https://www.hometax.go.kr/'>링크<ul><li>항목</li></ul>").html == (
        '<p>가<em><a href="https://www.hometax.go.kr/" target="_blank" rel="noopener noreferrer">링크</a></em></p>\n'
        "<ul><li>항목</li>\n</ul>"
    )
    # 표 안의 문단처럼 인라인이 아닌 태그가 사이에 끼면 바깥 <p>는 건드리지 않는다.
    assert clean("<p>가<table><tr><td><p>나</td></tr></table>").html.count("<p>") == 2


def test_unclosed_iframe_drops_the_rest_instead_of_leaking_it() -> None:
    result = clean('<p>앞</p><iframe src="x"><p>뒤</p>')

    assert "앞" in result.text and "뒤" not in result.text


def test_escaped_markup_in_text_stays_inert() -> None:
    result = clean("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")

    assert "<script" not in result.html
    assert "&lt;script&gt;" in result.html


def test_code_class_is_limited_to_language_names() -> None:
    good = clean('<pre><code class="language-java">int a;</code></pre>')
    bad = clean('<pre><code class="x" onmouseover="alert(1)">int a;</code></pre>')

    assert 'class="language-java"' in good.html
    assert "onmouseover" not in bad.html and "class=" not in bad.html


def test_counts_headings_tables_and_ordered_lists_and_collapses_text() -> None:
    result = clean(
        "<h2>가</h2><p>한 줄</p><h2>나</h2><ol><li>1단계</li><li>2단계</li></ol><table><tr><td>x</td></tr></table>"
    )

    assert (result.h2_count, result.ol_count, result.table_count) == (2, 1, 1)
    assert result.text == "가 한 줄 나 1단계 2단계 x"


def test_parser_failure_falls_back_to_escaped_paragraphs(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, data):  # noqa: ANN001
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("app.llm.sanitize._Sanitizer.feed", boom)

    result = clean("<b>첫 줄</b>\n<script>alert(1)</script>\n둘째 줄")

    assert "<script" not in result.html and "<b>" not in result.html
    assert result.html.startswith("<p>")
    assert result.notes == ["본문 HTML 구조가 깨져 텍스트만 남겼습니다."]


def test_pathological_nesting_is_capped_and_text_survives() -> None:
    start = time.perf_counter()

    result = clean("<b>" * 5000 + "깊다" + "</b>" * 5000)

    assert result.text == "깊다"
    assert result.html.count("<strong>") == MAX_NESTING == result.html.count("</strong>")
    assert time.perf_counter() - start < 5


def test_many_unmatched_end_tags_stay_fast() -> None:
    start = time.perf_counter()

    result = clean("<b>" * 3000 + "</i>" * 50_000 + "끝")

    assert result.text == "끝"
    assert time.perf_counter() - start < 5


def test_oversized_input_is_truncated_with_a_note() -> None:
    start = time.perf_counter()

    result = clean("<p>" + "가" * 500_000 + "</p>")

    assert len(result.text) == MAX_INPUT_CHARS - len("<p>")
    assert "본문이 너무 길어 일부를 잘랐습니다." in result.notes
    assert time.perf_counter() - start < 5


def test_strip_presentation_removes_table_styles_and_wrapper_only() -> None:
    from app.llm.sanitize import strip_presentation

    styled = clean("<table><tr><th>구분</th></tr></table>").html

    assert 'style="' in styled
    assert strip_presentation(styled) == "<table><tr><th>구분</th></tr>\n</table>"


def test_strip_citation_markers() -> None:
    assert strip_citation_markers("정부24[1] 안내[2][3]입니다 [4, 5]") == "정부24 안내입니다"
    assert strip_citation_markers("2026[년] 배열[a]") == "2026[년] 배열[a]"


# --- 코드 샘플과 꺾쇠 표기: 태그로 읽혀 사라지지 않고 글자로 남는다 ---------------------------------------


def test_angle_brackets_in_code_samples_are_shown_as_text() -> None:
    result = clean(
        '<pre><code class="language-java">List<String> names = new ArrayList<>();\n'
        "Map<String, List<Integer>> m = new HashMap<>();</code></pre>"
    )

    assert result.html == (
        '<pre><code class="language-java">List&lt;String&gt; names = new ArrayList&lt;&gt;();\n'
        "Map&lt;String, List&lt;Integer&gt;&gt; m = new HashMap&lt;&gt;();</code></pre>"
    )
    assert "List<String> names = new ArrayList<>();" in result.text


def test_markup_inside_code_samples_is_kept_as_text_and_never_runs() -> None:
    result = clean(
        '<pre><code class="language-html"><div class="box"><button>OK</button></div>'
        "<script>alert(1)</script><style>a{}</style></code></pre>"
    )

    assert '&lt;div class="box"&gt;&lt;button&gt;OK&lt;/button&gt;&lt;/div&gt;' in result.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;style&gt;a{}&lt;/style&gt;" in result.html
    assert "<script" not in result.html and "<div" not in result.html and "<button" not in result.html


def test_generic_type_names_survive_in_inline_code_and_in_prose() -> None:
    assert clean("<p><code>Optional<String></code> 타입을 씁니다.</p>").html == "<p><code>Optional&lt;String&gt;</code> 타입을 씁니다.</p>"
    assert clean("<p>List<String> 은 제네릭입니다 <b>굵게</b></p>").html == "<p>List&lt;String&gt; 은 제네릭입니다 <strong>굵게</strong></p>"
    assert clean("#include <stdio.h>").html == "#include &lt;stdio.h&gt;"
    # HTML 요소 이름이 아닌 것만 글자로 남긴다. 실제 요소(font, span)는 예전처럼 벗기고 안의 글만 남긴다.
    assert clean("<p><T>와 <K, V> 같은 표기, <font>폰트</font> <span>벗김</span></p>").html == (
        "<p>&lt;T&gt;와 &lt;K, V&gt; 같은 표기, 폰트 벗김</p>"
    )


def test_angle_bracket_notation_outside_code_is_reported_for_a_human_to_check() -> None:
    prose = clean("<p>사용법 <note>주의</note> 그리고 List<String></p>")
    code = clean("<pre><code>List<String> a;</code></pre><p><code>Map<K, V></code></p>")

    assert prose.html == "<p>사용법 &lt;note&gt;주의&lt;/note&gt; 그리고 List&lt;String&gt;</p>"
    assert any("꺾쇠 표기 2개(<note>, <String>)를 글자로 남겼습니다" in note for note in prose.notes)
    assert code.notes == []  # 코드 안의 꺾쇠는 당연한 것이라 알리지 않는다


def test_comments_declarations_and_line_breaks_in_code_samples_survive_as_text() -> None:
    result = clean("<pre><code><!-- 주석 --><!DOCTYPE html><br/>줄<br>바꿈</code></pre>")

    assert result.html == "<pre><code>&lt;!-- 주석 --&gt;&lt;!DOCTYPE html&gt;\n줄\n바꿈</code></pre>"
    assert clean("<p>본문 <!-- 숨은 주석 --> 입니다</p>").html == "<p>본문  입니다</p>"  # 코드 밖의 주석은 버린다


def test_an_unclosed_inline_code_does_not_turn_the_rest_of_the_article_into_text() -> None:
    result = clean("<p>앞 <code>닫지 않음 <p>다음 문단</p><h2>소제목</h2><p>본문</p>")

    assert result.html == "<p>앞 <code>닫지 않음 </code></p>\n<p>다음 문단</p>\n<h2>소제목</h2>\n<p>본문</p>"
    assert result.h2_count == 1 and result.notes == []


def test_an_unclosed_code_block_is_reported_instead_of_silently_swallowing_the_rest() -> None:
    result = clean("<pre><code>닫히지 않은 코드 <h2>소제목</h2>")

    assert "&lt;h2&gt;" in result.html and result.h2_count == 0
    assert any("닫히지 않아" in note for note in result.notes)


def test_citation_markers_survive_in_inline_code_too() -> None:
    assert clean("<p><code>arr[1]</code>와 정부24[1]</p>").html == "<p><code>arr[1]</code>와 정부24</p>"


# --- 속성 값을 깨고 나오려는 시도(변이 점검에서 테스트가 놓친 가드들) -------------------------------------


def test_link_urls_are_attribute_escaped() -> None:
    url = "https://news.example.com/a?x=1&y=2&z='"
    policy = LinkPolicy.from_sources([Source("기사", url)])

    result = sanitize_html(f'<a href="{url}">기사</a>', policy=policy)

    assert 'href="https://news.example.com/a?x=1&amp;y=2&amp;z=&#x27;"' in result.html
    assert grammar_violations(result.html) == []


def test_a_url_with_a_quote_never_becomes_a_link() -> None:
    result = clean("""<a href='https://www.gov.kr:"onmouseover="alert(1)/'>x</a>""")

    assert "<a" not in result.html and "onmouseover" not in result.html and result.removed_links


@pytest.mark.parametrize(
    "language_class",
    ['language-x"onclick=alert(1)', "language-", "language-" + "a" * 21, "x language-java", "language-java onclick"],
)
def test_code_class_must_be_a_plain_language_name(language_class: str) -> None:
    result = clean(f"<pre><code class='{language_class}'>x</code></pre>")

    assert "class=" not in result.html and "onclick" not in result.html


def test_th_scope_is_limited_to_col_and_row() -> None:
    result = clean("""<table><tr><th scope='row" onclick="alert(1)'>x</th></tr></table>""")

    assert "onclick" not in result.html and "scope=" not in result.html


def test_table_spans_and_list_start_are_bounded() -> None:
    result = clean(
        '<table><tr><td colspan="99999" rowspan="11">큼</td><td colspan="10" rowspan="1">작음</td></tr></table>'
        '<ol start="5000"><li>a</li></ol><ol start="12"><li>b</li></ol>'
    )

    assert 'colspan="99999"' not in result.html and 'rowspan="11"' not in result.html
    assert 'colspan="10"' in result.html and 'rowspan="1"' in result.html
    assert 'start="5000"' not in result.html and 'start="12"' in result.html


def test_root_links_need_the_default_port_and_no_query() -> None:
    result = clean(
        '<a href="https://www.hometax.go.kr:8443/">포트</a> '
        '<a href="https://www.hometax.go.kr/?redirect=https://evil.example">쿼리</a> '
        '<a href="https://www.hometax.go.kr/">루트</a>'
    )

    assert result.html.count("<a ") == 1 and len(result.removed_links) == 2


def test_a_backslash_cannot_smuggle_another_host_into_an_official_root() -> None:
    backslash = chr(92)  # 브라우저는 \를 /로 읽어 이 주소의 호스트를 evil.example로 만든다

    result = clean(f'<a href="https://evil.example{backslash}.gov.kr/">x</a>')

    assert "<a" not in result.html and result.removed_links


def test_links_to_unofficial_sites_get_nofollow_but_official_ones_do_not() -> None:
    policy = LinkPolicy.from_sources([Source("기사", "https://news.example.com/a")])

    result = sanitize_html('<a href="https://news.example.com/a">기사</a> <a href="https://www.gov.kr/">정부24</a>', policy=policy)

    assert 'href="https://news.example.com/a" target="_blank" rel="noopener noreferrer nofollow"' in result.html
    assert 'href="https://www.gov.kr/" target="_blank" rel="noopener noreferrer"' in result.html


def test_invisible_characters_are_removed_from_text_but_emoji_joiners_stay() -> None:
    rlo, zwsp, bell, tag_char, bom, zwj = chr(0x202E), chr(0x200B), chr(7), chr(0xE0041), chr(0xFEFF), chr(0x200D)

    result = clean(f"<p>가{rlo}나{zwsp}다{bell}라{tag_char}마{bom}바</p>")

    assert result.html == "<p>가나다라마바</p>" and result.text == "가나다라마바"
    assert zwj in clean(f"<p>👨{zwj}👩</p>").html
