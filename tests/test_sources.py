from __future__ import annotations

from datetime import date

import pytest

from app.llm.sources import (
    LinkPolicy,
    Source,
    clean_url,
    is_official_host,
    is_ugc_host,
    merge_sources,
    normalize_url,
    rank_sources,
    render_footer,
    strip_footer,
    usable_sources,
)


def test_clean_url_accepts_plain_http_urls() -> None:
    assert clean_url("https://www.gov.kr/portal") == "https://www.gov.kr/portal"
    assert clean_url("http://example.com/a?b=1") == "http://example.com/a?b=1"
    assert clean_url("ht\ntps://www.gov.kr") == "https://www.gov.kr"


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "//evil.example/x",
        "ftp://x.example/a",
        "https://user:pw@x.example/",
        "https://x.example\\@evil.example/",
        "data:text/html,x",
        "mailto:a@b.example",
        "https://",
        "",
        " jav\nascript:alert(1)",
    ],
)
def test_clean_url_rejects_everything_else(bad: str) -> None:
    assert clean_url(bad) is None


@pytest.mark.parametrize("bad", ['https://x.example/a"b', "https://x.example/<b>", "https://x.example/a`b", "https://x.example:99999/", "https://x.example:abc/"])
def test_clean_url_rejects_attribute_breakers_and_invalid_ports(bad: str) -> None:
    assert clean_url(bad) is None


def test_clean_url_keeps_apostrophes_and_default_ports() -> None:
    assert clean_url("https://ko.wikipedia.org/wiki/O'Brien") == "https://ko.wikipedia.org/wiki/O'Brien"
    assert clean_url("https://x.example:443/a") == "https://x.example:443/a"


def test_clean_url_rejects_a_backslash_that_browsers_read_as_a_slash() -> None:
    assert clean_url("https://evil.example" + chr(92) + ".gov.kr/") is None  # '@'가 없어도 거부한다


def test_clean_url_strips_invisible_characters_instead_of_letting_them_hide_a_scheme() -> None:
    assert clean_url("ht" + chr(0x200B) + "tps://www.gov.kr/a" + chr(0x202E)) == "https://www.gov.kr/a"


def test_normalize_url_ignores_scheme_www_fragment_and_trailing_slash() -> None:
    assert normalize_url("http://www.Gov.kr/a/b/#top") == normalize_url("https://gov.kr/a/b") == "gov.kr/a/b"
    assert normalize_url("https://gov.kr/a?x=1") != normalize_url("https://gov.kr/a")


def test_normalize_url_tells_non_default_ports_apart() -> None:
    assert normalize_url("https://news.example.com:8443/a/1") != normalize_url("https://news.example.com/a/1")
    assert normalize_url("https://news.example.com:443/a/1") == normalize_url("http://news.example.com:80/a/1") == "news.example.com/a/1"


@pytest.mark.parametrize(
    ("host", "official"),
    [
        ("www.gov.kr", True), ("hometax.go.kr", True), ("go.kr", True), ("www.nhis.or.kr", True),
        ("evilgov.kr", False), ("notgo.kr", False), ("evil-or.kr", False), ("example.or.kr", False), ("evilnhis.or.kr", False),
    ],
)
def test_official_hosts_are_matched_on_whole_labels(host: str, official: bool) -> None:
    assert is_official_host(host) is official


@pytest.mark.parametrize(
    ("host", "ugc"),
    [("x.com", True), ("mobile.x.com", True), ("blog.naver.com", True), ("dropbox.com", False), ("box.com", False), ("notyoutube.com", False)],
)
def test_ugc_hosts_are_matched_on_whole_labels(host: str, ugc: bool) -> None:
    assert is_ugc_host(host) is ugc


def test_merge_sources_dedupes_drops_invalid_and_fills_titles() -> None:
    merged = merge_sources(
        [
            {"title": "A", "url": "https://x.example/a"},
            {"title": "중복", "url": "http://www.x.example/a/"},
            {"title": "위험", "url": "javascript:alert(1)"},
        ],
        [{"url": "https://y.example/b"}, "not-a-dict", Source("C", "https://z.example/c")],
    )

    assert merged == [
        Source("A", "https://x.example/a"),
        Source("y.example", "https://y.example/b"),
        Source("C", "https://z.example/c"),
    ]


def test_usable_sources_exclude_community_and_sns_hosts() -> None:
    sources = [
        Source("블로그", "https://blog.naver.com/a/1"),
        Source("티스토리", "https://foo.tistory.com/1"),
        Source("공식", "https://www.gov.kr/x"),
        Source("뉴스", "https://www.yna.co.kr/view/1"),
        Source("유튜브", "https://www.youtube.com/watch?v=1"),
    ]

    assert [source.title for source in usable_sources(sources)] == ["공식", "뉴스"]


def test_rank_sources_puts_official_sources_first_and_otherwise_keeps_the_order() -> None:
    sources = [
        Source("뉴스 A", "https://www.yna.co.kr/a"),
        Source("블로그", "https://blog.naver.com/x/1"),
        Source("정부24", "https://www.gov.kr/a"),
        Source("뉴스 B", "https://news.example.com/b"),
        Source("건강보험", "https://www.nhis.or.kr/x"),
        Source("아무 or.kr 단체", "https://www.example.or.kr/y"),
    ]

    # or.kr은 누구나 등록할 수 있어 통째로 공식으로 보지 않는다. 자주 인용되는 공공기관(nhis.or.kr 등)만 공식이다.
    assert [source.title for source in rank_sources(sources)] == ["정부24", "건강보험", "뉴스 A", "뉴스 B", "아무 or.kr 단체"]


def test_link_policy_allows_verified_urls_official_roots_and_source_site_roots() -> None:
    policy = LinkPolicy.from_sources([Source("기사", "https://news.example.com/a/1")])

    assert policy.allows("https://news.example.com/a/1/")
    assert policy.allows("https://news.example.com/")
    assert not policy.allows("https://news.example.com/a/2")
    assert policy.allows("https://www.hometax.go.kr/")
    assert not policy.allows("https://www.hometax.go.kr/ui/pp/index.do")
    assert not policy.allows("https://evil.example/")
    assert not policy.allows("javascript:alert(1)")


def test_link_policy_does_not_trust_ports_queries_or_arbitrary_or_kr_hosts() -> None:
    policy = LinkPolicy.from_sources([Source("기사", "https://news.example.com/a/1")])

    assert not policy.allows("https://news.example.com:8443/a/1")  # 같은 호스트의 다른 포트는 다른 서비스다
    assert not policy.allows("https://news.example.com:8443/")
    assert not policy.allows("https://www.hometax.go.kr/?redirect=https://evil.example")
    assert not policy.allows("https://evil.or.kr/")  # or.kr은 누구나 등록할 수 있다
    assert policy.allows("https://www.nhis.or.kr/")
    assert not policy.allows("https://evil.example" + chr(92) + ".gov.kr/")


def test_render_footer_lists_official_sources_first_and_escapes_titles() -> None:
    sources = [
        Source("뉴스 <b>기사</b>", "https://www.yna.co.kr/a?x=1&y=2"),
        Source("정부24", "https://www.gov.kr/a"),
        Source("블로그", "https://blog.naver.com/x/1"),
    ]

    html = render_footer(sources, date(2026, 10, 1), "lifestyle")

    assert html.startswith("<h2>참고한 자료</h2>")
    assert html.index("정부24") < html.index("뉴스")
    assert "&lt;b&gt;기사&lt;/b&gt;" in html and "x=1&amp;y=2" in html
    assert "블로그" not in html
    assert '<a href="https://www.gov.kr/a" target="_blank" rel="noopener noreferrer">정부24</a>' in html
    assert "2026년 10월 1일 기준으로 확인한 내용이에요." in html


def test_render_footer_shows_the_real_host_and_marks_unofficial_links_nofollow() -> None:
    sources = [Source("정부24 공식 안내 - 지금 바로 신청하기", "https://gov24-help.example.net/apply"), Source("정부24", "https://www.gov.kr/a")]

    html = render_footer(sources, date(2026, 10, 1), "lifestyle")

    # 제목은 검색 결과를 쓴 쪽이 정하므로, 실제 호스트를 함께 보여 주어 공식 안내로 꾸민 가짜를 알아볼 수 있게 한다.
    assert ('rel="noopener noreferrer">정부24</a> (gov.kr)</li>') in html
    assert ('rel="noopener noreferrer nofollow">정부24 공식 안내 - 지금 바로 신청하기</a> (gov24-help.example.net)</li>') in html


def test_render_footer_escapes_urls_and_removes_invisible_characters_from_titles() -> None:
    # merge_sources를 거치지 않고 직접 만든 출처라도 푸터가 속성을 깨고 나오지 못해야 한다.
    html = render_footer([Source("제목" + chr(0x202E) + "끝", 'https://e.example/a"onmouseover="alert(1)')], date(2026, 10, 1), "lifestyle")

    assert 'href="https://e.example/a&quot;onmouseover=&quot;alert(1)"' in html
    assert ">제목끝</a>" in html


def test_strip_footer_removes_exactly_what_render_footer_added() -> None:
    body = "<h2>소제목</h2>\n<p>본문 2026년 기준.</p>"
    sources = [Source("정부24", "https://www.gov.kr/a")]
    with_sources = f"{body}\n\n{render_footer(sources, date(2026, 10, 1), 'lifestyle')}"
    date_only = f"{body}\n\n{render_footer([], date(2026, 10, 1), 'technical')}"

    assert strip_footer(with_sources) == body
    assert strip_footer(date_only) == body
    assert strip_footer(body) == body


def test_merge_sources_caps_the_title_length() -> None:
    [source] = merge_sources([{"title": "가" * 200, "url": "https://x.example/a"}])

    assert source.title == "가" * 80


def test_merge_sources_removes_invisible_characters_from_titles() -> None:
    merged = merge_sources([{"title": "기사" + chr(0xE0041) + chr(0x200B) + " 제목", "url": "https://x.example/a"}])

    assert merged == [Source("기사 제목", "https://x.example/a")]


def test_render_footer_without_sources_keeps_only_the_date_notice() -> None:
    html = render_footer([], date(2026, 1, 5), "lifestyle")

    assert "<h2>" not in html and "<ul>" not in html
    assert "2026년 1월 5일 기준" in html


def test_render_footer_for_technical_posts_uses_plain_style_notice() -> None:
    html = render_footer([], date(2026, 10, 1), "technical")

    assert "공식 문서를 함께 확인한다." in html


def test_render_footer_limits_number_of_items() -> None:
    sources = [Source(f"출처 {n}", f"https://news.example.com/{n}") for n in range(10)]

    assert render_footer(sources, date(2026, 10, 1), "lifestyle").count("<li>") == 6
