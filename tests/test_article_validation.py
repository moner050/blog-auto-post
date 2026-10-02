from __future__ import annotations

from pathlib import Path

import pytest

from app.llm.sanitize import sanitize_html
from app.llm.style import PERSONA, StyleProfile, load_style_profile
from app.llm.validation import ERROR, WARN, Issue, score, validate_article
from tests.article_helpers import DEFAULT_SUMMARY, DEFAULT_TITLE, make_body

REAL_RULES = Path(__file__).resolve().parents[1] / "configs" / "tistory_blog_style_rules.yaml"


def validate(
    profile: StyleProfile,
    *,
    title: str = DEFAULT_TITLE,
    summary: str = DEFAULT_SUMMARY,
    tags: list[str] | None = None,
    body_html: str | None = None,
    **body_kwargs: object,
) -> list[Issue]:
    html = make_body(**body_kwargs) if body_html is None else body_html
    return validate_article(
        title=title,
        summary=summary,
        tags=["가", "나", "다", "라", "마"] if tags is None else tags,
        body=sanitize_html(html, title=title),
        profile=profile,
    )


def by_code(issues: list[Issue]) -> dict[str, Issue]:
    return {issue.code: issue for issue in issues}


def test_compliant_article_has_no_issues(mini_profile: StyleProfile) -> None:
    assert validate(mini_profile) == []


def test_title_is_required_and_hard_length_limits_are_errors(mini_profile: StyleProfile) -> None:
    assert by_code(validate(mini_profile, title="  "))["title.empty"].severity == ERROR
    assert by_code(validate(mini_profile, title="가" * 75))["title.length"].severity == ERROR
    assert by_code(validate(mini_profile, title="가" * 8))["title.length"].severity == ERROR


def test_title_slightly_outside_the_range_is_only_a_warning(mini_profile: StyleProfile) -> None:
    short = by_code(validate(mini_profile, title="가" * 15))["title.length"]
    long = by_code(validate(mini_profile, title="가" * 50))["title.length"]

    assert short.severity == long.severity == WARN
    assert "20~40자" in short.message and "15자" in short.message


def test_title_exclamation_limit_depends_on_the_title_style(mini_profile: StyleProfile, mini_rules_path: Path) -> None:
    one = "등본 발급 방법, 정부24에서 끝내는 순서 정리했어요!"
    two = "등본 발급 방법, 정부24에서 끝내는 순서! 정리했어요!"
    persona = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", title_style=PERSONA)

    assert "title.exclaim" not in by_code(validate(mini_profile, title=one))
    assert "title.exclaim" in by_code(validate(mini_profile, title=two))
    assert "title.exclaim" in by_code(validate(persona, title=one))


@pytest.mark.parametrize(
    "title",
    [
        "대출 받기 전에 알아야 할 기준을 차분히 정리한 안내 글입니다",
        "주민등록등본 발급 방법과 방문 신청 차이를 정리한 안내 글",  # 실호출에서 나온 정보 전달형 제목과 같은 꼴
        "주민등록등본 발급 방법, 정부24 온라인 신청 절차 안내",  # '정부24'의 24는 이름의 일부라 숫자 장치가 아니다
    ],
)
def test_a_flat_informational_title_is_a_revision_target_in_clickbait_mode(mini_profile: StyleProfile, title: str) -> None:
    flat = by_code(validate(mini_profile, title=title))["title.flat"]

    assert flat.severity == ERROR and "클릭을 부르는 장치" in flat.message


@pytest.mark.parametrize(
    "title",
    [
        "DSR 규제한다더니 신규 대출이 전부 예외일까?",  # 질문형
        "청년도약계좌 가입 전 꼭 알아야 할 조건 정리 안내글",  # 손해 회피성 단어(꼭 )
        "연말정산 모르면 손해 보는 공제 항목을 정리했어요",  # 모르면/손해
        "등본 발급 방법, 5분 만에 끝내는 순서 정리했어요",  # 글자에 붙지 않은 숫자
        "신용점수 올리는 법, 이것만 알아도 달라져요",  # 호기심·변화를 말하는 표현
    ],
)
def test_titles_with_a_question_number_or_loss_aversion_wording_are_not_flat(mini_profile: StyleProfile, title: str) -> None:
    assert "title.flat" not in by_code(validate(mini_profile, title=title))


def test_flat_titles_are_fine_under_the_persona_title_style(mini_rules_path: Path) -> None:
    persona = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", title_style=PERSONA)

    assert "title.flat" not in by_code(validate(persona, title="대출 받기 전에 알아야 할 기준을 차분히 정리한 안내 글입니다"))


def test_title_numbers_must_be_supported_by_the_body(mini_profile: StyleProfile) -> None:
    title = "등본 발급 70% 달라졌다? 정부24에서 신청하는 방법 정리"

    unsupported = by_code(validate(mini_profile, title=title))["title.number_unsupported"]
    supported = validate(mini_profile, title=title, body_html=make_body(extra="<p>발급 건수의 70%가 온라인입니다.</p>"))

    assert unsupported.severity == ERROR and "'70'" in unsupported.message
    assert "title.number_unsupported" not in by_code(supported)


@pytest.mark.parametrize(
    ("title", "body_extra", "supported"),
    [
        # 숫자 일부가 우연히 겹치는 것(기본 본문의 '2026년 10월 1일')으로는 근거가 되지 않는다.
        ("월 20만 원 환급 받는 방법 정부24 정리", "", False),
        ("신청하면 10만 원 돌려받는다? 정부24 방법", "", False),
        ("이 조건이면 70% 가 달라졌다? 정부24 방법 정리", "<p>170명이 신청했어요.</p>", False),
        ("이 조건이면 70% 가 달라졌다? 정부24 방법 정리", "<p>1,700만 명이 신청했어요.</p>", False),
        ("신청자 15% 늘었다? 정부24 발급 방법 정리", "<p>2015년 자료입니다.</p>", False),
        ("월 300만 원 받는다? 정부24 지원금 방법 정리", "<p>3000원을 냅니다.</p>", False),
        # 근거가 있는 경우
        ("이 조건이면 70% 가 달라졌다? 정부24 방법 정리", "<p>비중이 70.5%로 집계됐어요.</p>", True),  # 반올림한 제목
        ("10일 만에 끝낸다? 정부24 발급 방법 정리", "<p>처리에 10일 이내가 걸려요.</p>", True),
        ("1,000만 명이 쓴다? 정부24 발급 방법 정리", "<p>1000만 명이 가입했어요.</p>", True),
        ("2026 달라진 정부24 발급 방법 정리", "", True),  # 기본 본문에 '2026년'이 있다
    ],
)
def test_title_numbers_are_compared_as_whole_numbers_with_their_units(
    mini_profile: StyleProfile, title: str, body_extra: str, supported: bool
) -> None:
    issues = by_code(validate(mini_profile, title=title, body_html=make_body(extra=body_extra)))

    assert ("title.number_unsupported" not in issues) is supported


def test_a_title_number_that_is_more_precise_than_the_body_is_not_supported(mini_profile: StyleProfile) -> None:
    issues = by_code(validate(mini_profile, title="이 조건이면 70.5% 가 달라졌다? 정부24 방법", body_html=make_body(extra="<p>비중이 70%입니다.</p>")))

    assert "title.number_unsupported" in issues


def test_title_numbers_ignore_thousand_separators_and_single_digits(mini_profile: StyleProfile) -> None:
    comma = validate(mini_profile, title="가입자 1,000만 돌파한 등본 발급 방법 정부24 정리", body_html=make_body(extra="<p>1000만 명이 가입했어요.</p>"))
    single = validate(mini_profile, title="등본 발급 방법 3가지만 알면 정부24에서 끝나요 정리")

    assert "title.number_unsupported" not in by_code(comma)
    assert "title.number_unsupported" not in by_code(single)


def test_body_length_thresholds(mini_profile: StyleProfile) -> None:
    too_short = by_code(validate(mini_profile, h2=3, paragraphs_per_h2=1))["body.too_short"]
    short = by_code(validate(mini_profile, h2=3, paragraphs_per_h2=2))["body.short"]
    long = by_code(validate(mini_profile, h2=5, paragraphs_per_h2=6))["body.long"]

    assert too_short.severity == ERROR and "1,000~2,000자" in too_short.message
    assert short.severity == long.severity == WARN


def test_body_below_three_quarters_of_the_minimum_is_an_error_not_a_warning(mini_profile: StyleProfile) -> None:
    """권장 최소 1,000자의 75% = 750자. 이 경계 바로 아래/위를 본문 길이로 직접 맞춰 확인한다."""
    def issues_for(chars: int) -> dict[str, Issue]:
        body = f"<h2>💡 가</h2><p>{'가' * chars}</p><h2>✅ 나</h2><h2>⚠️ 다</h2><ol><li>하나</li></ol>"
        return by_code(validate(mini_profile, body_html=body))

    assert issues_for(700)["body.too_short"].severity == ERROR
    assert "body.too_short" not in issues_for(790) and issues_for(790)["body.short"].severity == WARN


def test_heading_counts(mini_profile: StyleProfile) -> None:
    assert by_code(validate(mini_profile, h2=0))["h2.missing"].severity == ERROR
    few = by_code(validate(mini_profile, h2=2, emoji=2))["h2.count"]
    many = by_code(validate(mini_profile, h2=6, emoji=3))["h2.count"]

    assert few.severity == many.severity == WARN and "3~5개" in few.message


def test_emoji_count_must_stay_in_range(mini_profile: StyleProfile) -> None:
    assert "emoji.count" in by_code(validate(mini_profile, emoji=0))
    assert "emoji.count" in by_code(validate(mini_profile, h2=5, emoji=5))
    assert "emoji.count" not in by_code(validate(mini_profile, emoji=2))


def test_technical_posts_must_not_use_emoji() -> None:
    technical = load_style_profile(REAL_RULES, "Spring Bean이란 무엇인가")
    body = "<h2>💡 개념</h2><p>" + "빈은 스프링 컨테이너가 관리하는 객체다. " * 40 + "</p>"

    issues = by_code(validate(technical, title="Spring Bean이란 무엇인가, 개념과 사용법 정리", body_html=body))

    assert technical.mode == "technical" and issues["emoji.forbidden"].severity == WARN


def test_restricted_phrases_are_counted_over_the_whole_body(mini_profile: StyleProfile) -> None:
    within = validate(mini_profile, body_html=make_body(extra="<p>대박 그리고 무조건</p>"))
    over = by_code(validate(mini_profile, body_html=make_body(extra="<p>대박 무조건 대박</p>")))["restricted.phrases"]

    assert "restricted.phrases" not in by_code(within)
    assert over.severity == ERROR and "'대박' 2회, '무조건' 1회" in over.message and "2회 이하" in over.message


def test_clickbait_titles_are_exempt_from_restricted_phrases_but_persona_titles_are_not(
    mini_profile: StyleProfile, mini_rules_path: Path
) -> None:
    title = "무조건 대박 무조건 등본 발급 방법, 정부24에서 끝내는 순서 정리"  # 허용 2회를 넘는 3회
    persona = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", title_style=PERSONA)

    assert "restricted.phrases" not in by_code(validate(mini_profile, title=title))
    assert "restricted.phrases" in by_code(validate(persona, title=title))


@pytest.mark.parametrize(
    "sentence",
    [
        "제가 직접 써봤는데 편했어요.", "직접 신청해 보니 빨랐어요.", "제 경험상 그렇더라고요.", "저도 발급해 봤는데 쉬웠어요.",
        # 띄어쓰기를 달리 쓴 같은 뜻의 문장
        "제가 써 보니 편했어요.", "제가 해 보니까 금방 끝났어요.", "제가 이용해 보니 빨랐어요.", "제가 신청해 보니 어렵지 않았어요.",
        "제가 겪어 보니 그렇더라고요.", "저도 직접 발급받아 봤는데 쉬웠어요.", "직접 가 봤는데 사람이 많았어요.",
        "저는 지난달 환급받았어요.", "내가 직접 확인해 봤어요.", "실제로 사용해 본 결과 만족스러웠어요.",
    ],
)
def test_invented_first_person_experience_is_an_error(mini_profile: StyleProfile, sentence: str) -> None:
    issues = by_code(validate(mini_profile, body_html=make_body(extra=f"<p>{sentence}</p>")))

    assert issues["experience.fabricated"].severity == ERROR


@pytest.mark.parametrize(
    "sentence",
    [
        "직접 신청해 보세요.", "저는 이 방법을 추천해요.", "제가 안내해 드릴게요.", "공식 안내를 확인해 보니 정부24에서 가능해요.",
        "많은 분이 직접 가 보시는 곳이에요.", "저는 이 글에서 신청 방법을 정리했어요.", "신청하면 바로 발급됩니다.",
    ],
)
def test_second_person_advice_and_research_voice_are_not_flagged_as_experience(mini_profile: StyleProfile, sentence: str) -> None:
    assert "experience.fabricated" not in by_code(validate(mini_profile, body_html=make_body(extra=f"<p>{sentence}</p>")))


def test_leftover_markdown_is_an_error(mini_profile: StyleProfile) -> None:
    assert "markdown.leftover" in by_code(validate(mini_profile, body_html=make_body(extra="<p>**굵게** 표시</p>")))
    assert "markdown.leftover" in by_code(validate(mini_profile, body_html=make_body(extra="<p>```코드```</p>")))
    assert "markdown.leftover" in by_code(validate(mini_profile, body_html=make_body(extra="<p>## 소제목</p>")))
    assert "markdown.leftover" not in by_code(validate(mini_profile))


@pytest.mark.parametrize(
    "snippet",
    [
        '<pre><code class="language-bash"># 설치 방법\nnpm install</code></pre>',
        '<pre><code class="language-python">def f(**kwargs):\n    return g(**kwargs, **extra)</code></pre>',
        "<p><code>**kwargs</code>를 씁니다.</p>",
        "<p># 1위 서비스 소개</p>",
        "<h2># 해시태그가 아니라 제목입니다</h2>",
    ],
)
def test_code_blocks_and_a_lone_hash_are_not_markdown_leftovers(mini_profile: StyleProfile, snippet: str) -> None:
    assert "markdown.leftover" not in by_code(validate(mini_profile, body_html=make_body(extra=snippet)))


def test_structure_hints_follow_the_blueprint(mini_rules_path: Path, mini_profile: StyleProfile) -> None:
    comparison = load_style_profile(mini_rules_path, "청년도약계좌와 청년희망적금 차이")

    assert "structure.table_missing" in by_code(validate(comparison))
    assert "structure.table_missing" not in by_code(validate(comparison, body_html=make_body(extra="<table><tr><td>x</td></tr></table>")))
    assert "structure.steps_missing" in by_code(validate(mini_profile, steps=False))
    assert "structure.steps_missing" not in by_code(validate(mini_profile))


def test_meta_fields_and_date_hint(mini_profile: StyleProfile) -> None:
    no_hint = "<h2>💡 가</h2><p>" + "내용을 자세히 설명해요. " * 60 + "</p><h2>✅ 나</h2><h2>⚠️ 다</h2><ol><li>하나</li></ol>"

    issues = by_code(validate(mini_profile, summary="", tags=["가"], body_html=no_hint))

    assert {"summary.missing", "tags.few", "date.missing"} <= set(issues)
    assert "summary.length" in by_code(validate(mini_profile, summary="너무 짧다"))
    assert all(issue.severity == WARN for issue in (issues["summary.missing"], issues["tags.few"], issues["date.missing"]))


def test_score_weights_errors_above_warnings() -> None:
    issues = [Issue("a", ERROR, ""), Issue("b", WARN, ""), Issue("c", WARN, "")]

    assert score(issues) == 12
    assert score([]) == 0
    assert score([Issue("a", WARN, "")] * 9) < score([Issue("a", ERROR, "")])
