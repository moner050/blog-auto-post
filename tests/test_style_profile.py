from __future__ import annotations

import os
from pathlib import Path
import re

import pytest
import yaml

from app.llm import style
from app.llm.style import CLICKBAIT, LIFESTYLE, PERSONA, TECHNICAL, load_style_profile, route_blueprint, route_mode
from tests.article_helpers import MINI_RULES

REAL_RULES = Path(__file__).resolve().parents[1] / "configs" / "tistory_blog_style_rules.yaml"


def test_route_mode_matches_the_boundary_examples_documented_in_the_yaml() -> None:
    examples = yaml.safe_load(REAL_RULES.read_text(encoding="utf-8"))["content_routing"]["boundary_examples"]

    assert examples
    for topic, expected in examples.items():
        assert route_mode(topic) == expected, topic


@pytest.mark.parametrize("topic", ["쿠폰 코드 적용 방법", "할인 코드 입력하는 곳", "서버 점검 안내 확인하는 법", "고지서 납부 방법"])
def test_everyday_words_that_look_technical_stay_lifestyle(topic: str) -> None:
    assert route_mode(topic, "생활꿀팁") == LIFESTYLE


@pytest.mark.parametrize(
    "topic",
    [
        "프리랜서 개발자 종합소득세 신고 방법",
        "국비지원 코딩 부트캠프 신청 방법",
        "유튜브 알고리즘 바뀐 점, 수익 창출 조건",
        "Git 몰라도 되는 연말정산",
        "HTML 모르는 부모님도 하는 정부24 신청",
        "SQL 몰라도 엑셀로 가계부 쓰는 법",
        "데이터베이스 보험 가입 시 확인할 점",
    ],
)
def test_one_incidental_technical_word_does_not_make_a_lifestyle_topic_technical(topic: str) -> None:
    assert route_mode(topic, "세금·환급") == LIFESTYLE
    assert route_mode(topic) == LIFESTYLE


@pytest.mark.parametrize(
    "topic",
    [
        "정부24 API 연동 방법",  # 기술 단어 + 구현 단어
        "에어컨 소비전력 계산 Python 코드",
        "Spring Bean이란 무엇인가",
        "Spring Boot 설정 파일 yml 작성 방법",
        "Docker 설치 방법",
        "Java 예외 처리 방법",  # 기술 단어 둘
        "개발자 알고리즘 면접 준비",
        "SQL 쿼리 최적화",
    ],
)
def test_technical_words_with_implementation_words_or_two_technical_words_are_technical(topic: str) -> None:
    assert route_mode(topic, "정부지원·민원") == TECHNICAL


@pytest.mark.parametrize(
    ("topic", "mode", "expected"),
    [
        ("홈택스 로그인 오류 해결", LIFESTYLE, "troubleshooting"),
        ("청년도약계좌와 청년희망적금 차이", LIFESTYLE, "comparison"),
        ("에어컨 전기요금 줄이는 방법", LIFESTYLE, "saving"),
        ("주민등록등본 발급 방법", LIFESTYLE, "how_to"),
        ("Spring Bean과 Component 차이", TECHNICAL, "concept_comparison"),
        ("Spring 예외 처리 해결", TECHNICAL, "debugging"),
        ("Spring Security 설정 방법", TECHNICAL, "implementation_howto"),
        ("POJO란", TECHNICAL, "concept_definition"),
    ],
)
def test_route_blueprint(topic: str, mode: str, expected: str) -> None:
    assert route_blueprint(topic, mode) == expected


def test_profile_values_follow_the_yaml(mini_rules_path: Path) -> None:
    profile = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", "정부지원·민원")

    assert (profile.mode, profile.blueprint, profile.source) == (LIFESTYLE, "how_to", str(mini_rules_path))
    assert profile.title_chars == (20, 40)
    assert profile.body_chars == (1000, 2000)
    assert profile.h2_count == (3, 5)
    assert profile.emoji_count == (2, 4)
    assert profile.summary_chars == (50, 100)
    assert profile.restricted_phrases == ("대박", "무조건")
    assert profile.restricted_max_total == 2
    assert profile.checklist == ("체크 A", "체크 B")
    assert profile.fallback_reason == ""


def test_rendered_rules_contain_yaml_content_in_korean(mini_rules_path: Path) -> None:
    rules = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", "생활꿀팁").system_rules

    for expected in (
        "미니 페르소나 지시문",
        "해요체",
        "일반 독자",
        "부모님의 생활 문제를 대신 알아보는 자녀",
        "한 문장은 대체로 10~30자",
        "앞쪽 15% 안에 핵심 답",
        "감탄문은 글 전체에서 0~3문장 이내로 쓴다",
        "미니 도입 문장",
        "소제목은 <h2> 3~5개",
        "1,000~2,000자",
        "표는 둘 이상의 선택지를 비교할 때에 쓰고, 최대 3열",
        "글 전체 2~4개",
        "'대박', '무조건'",
        "체크 A",
        "체크 B",
        "'문제 + 해결 범위' 구조",
        "1) 독자가 겪는 문제 공감 → 2) 번호별 진행 절차 → 3) 핵심 요약",
    ):
        assert expected in rules, expected
    assert "문장로" not in rules
    assert "compare_two_or_more_options" not in rules


def test_clickbait_title_style_is_default_and_exempts_the_title_from_restricted_phrases(mini_rules_path: Path) -> None:
    default = load_style_profile(mini_rules_path, "주민등록등본 발급 방법")
    clickbait = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", title_style=CLICKBAIT)
    persona = load_style_profile(mini_rules_path, "주민등록등본 발급 방법", title_style=PERSONA)

    assert default.title_style == clickbait.title_style == CLICKBAIT
    assert "클릭을 부르는 제목" in clickbait.system_rules
    assert "제목의 훅 표현은 예외" in clickbait.system_rules
    assert "느낌표는 최대 1개" in clickbait.system_rules
    # 페르소나의 '공포 자극·단정 지양'이 제목까지 순화하지 않도록 우선순위를 명시하고, 장치를 선택이 아닌 의무로 둔다.
    assert "제목은 이 섹션을 우선한다" in clickbait.system_rules
    assert "하나 이상을 반드시 쓴다" in clickbait.system_rules
    assert "{현상}?" in clickbait.system_rules
    assert persona.title_style == PERSONA
    assert "클릭을 부르는 제목" not in persona.system_rules
    assert "느낌표와 이모지를 쓰지 않는다" in persona.system_rules
    assert "제목과 본문을 합쳐 전체에서" in persona.system_rules


def test_unknown_title_style_falls_back_to_clickbait(mini_rules_path: Path) -> None:
    assert load_style_profile(mini_rules_path, "주제", title_style="weird").title_style == CLICKBAIT


@pytest.mark.parametrize("topic", ["주민등록등본 발급 방법", "정부24 API 연동 방법"])
def test_real_rules_file_renders_every_section_without_leaking_identifiers(topic: str) -> None:
    profile = load_style_profile(REAL_RULES, topic, "정부지원·민원")

    assert profile.source.endswith("tistory_blog_style_rules.yaml") and profile.fallback_reason == ""
    for section in ("[페르소나]", "[독자]", "[문체]", "[글 구조", "[소제목과 분량]", "[서식]", "[이모지]", "[제목(<article_title>)", "[금지·제한 표현]", "[사실성 규칙", "[검색 노출(SEO)]"):
        assert section in profile.system_rules, section
    visible = re.sub(r"\{[^}]*\}|<[^>]*>", "", profile.system_rules)
    assert re.findall(r"\b[a-z]+_[a-z_]+\b", visible) == []
    assert "문장로" not in profile.system_rules


def test_the_yaml_checklist_item_about_the_confirmation_date_does_not_clash_with_the_automatic_footer() -> None:
    rules = load_style_profile(REAL_RULES, "주민등록등본 발급 방법", "정부지원·민원").system_rules

    assert "공식 안내의 확인일을 적었는가 (글 끝의 확인일 안내 문구는 시스템이 붙인다." in rules
    assert "본문에는 'YYYY년 M월 기준'처럼 기준 시점만 밝힌다)" in rules


def test_real_technical_profile_uses_plain_style_rules() -> None:
    profile = load_style_profile(REAL_RULES, "Spring Bean이란 무엇인가", "생활꿀팁")

    assert profile.mode == TECHNICAL
    assert profile.emoji_count == (0, 0)
    assert "한다체" in profile.system_rules and "이모지를 쓰지 않는다" in profile.system_rules
    assert "language-java" in profile.system_rules
    # 어그로 제목은 생활 글에만 적용한다. 기술 글은 설정과 상관없이 YAML의 차분한 제목 규칙을 따른다.
    assert profile.title_style == PERSONA
    assert "클릭을 부르는 제목" not in profile.system_rules


def test_missing_file_falls_back_to_builtin_rules(tmp_path: Path) -> None:
    clickbait = load_style_profile(tmp_path / "nope.yaml", "주민등록등본 발급 방법")
    persona = load_style_profile(tmp_path / "nope.yaml", "주민등록등본 발급 방법", title_style=PERSONA)

    assert clickbait.source == "builtin" and "찾을 수 없습니다" in clickbait.fallback_reason
    assert "[페르소나]" in clickbait.system_rules and "클릭을 부르는 제목" in clickbait.system_rules
    assert "클릭을 부르는 제목" not in persona.system_rules and "느낌표와 이모지를 쓰지 않는다" in persona.system_rules
    assert clickbait.body_chars == style.DEFAULT_RANGES["body_chars"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [("key: [unclosed", "읽지 못했습니다"), ("- just\n- a list\n", "형식이 올바르지 않습니다"), ("modes: []\n", "형식이 올바르지 않습니다")],
)
def test_broken_rules_file_falls_back_with_a_reason(tmp_path: Path, content: str, reason: str) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(content, encoding="utf-8")

    profile = load_style_profile(path, "주민등록등본 발급 방법")

    assert profile.source == "builtin" and reason in profile.fallback_reason


@pytest.mark.parametrize(
    ("broken", "falls_back"),
    [
        ("restricted_expressions:\n  lifestyle: [대박, 무조건]\n", True),  # 매핑이어야 할 자리에 리스트: 해석 불가
        ("seo: 문자열\n", True),  # 매핑이어야 할 자리에 문자열: 해석 불가
        ("quality_checklists: {common: 7, lifestyle: 8}\n", False),  # 방어적으로 읽어 해당 항목만 건너뛴다
    ],
)
def test_wrongly_shaped_values_never_crash_generation(tmp_path: Path, broken: str, falls_back: bool) -> None:
    path = tmp_path / "typo.yaml"
    path.write_text(MINI_RULES.split("restricted_expressions:")[0] + broken, encoding="utf-8")

    profile = load_style_profile(path, "주민등록등본 발급 방법")

    assert profile.title_chars and profile.system_rules
    assert (profile.source == "builtin") is falls_back
    if falls_back:
        assert "형식이 올바르지 않습니다(AttributeError" in profile.fallback_reason
    else:
        assert profile.fallback_reason == ""


def test_rules_without_the_requested_mode_use_lifestyle(mini_rules_path: Path) -> None:
    profile = load_style_profile(mini_rules_path, "Spring Bean이란")

    assert profile.mode == LIFESTYLE and profile.fallback_reason == ""


def test_rules_are_cached_until_the_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rules.yaml"
    base = MINI_RULES
    path.write_text(base, encoding="utf-8")
    loads: list[int] = []
    real_load = yaml.safe_load
    monkeypatch.setattr(style.yaml, "safe_load", lambda text: (loads.append(1), real_load(text))[1])

    first = load_style_profile(path, "주제")
    second = load_style_profile(path, "주제")
    assert "미니 페르소나 지시문" in first.system_rules and len(loads) == 1 and second == first

    path.write_text(base.replace("미니 페르소나 지시문", "바뀐 페르소나"), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    reloaded = load_style_profile(path, "주제")

    assert "바뀐 페르소나" in reloaded.system_rules and len(loads) == 2


def test_relative_rules_path_is_resolved_against_the_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    profile = load_style_profile(Path("configs/tistory_blog_style_rules.yaml"), "주민등록등본 발급 방법")

    assert profile.source == str(Path("configs/tistory_blog_style_rules.yaml"))
    assert profile.fallback_reason == ""
