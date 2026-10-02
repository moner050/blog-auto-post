"""고단가 카테고리 글 구조 라우팅과 애드센스 정책·YMYL 안전장치 테스트."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from app.llm.prompts import ArticleRequest, build_system_prompt, build_user_prompt
from app.llm.sanitize import sanitize_html
from app.llm.style import COMMERCIAL_BLUEPRINT, LIFESTYLE, StyleProfile, load_style_profile
from app.llm.validation import ERROR, WARN, validate_article
from app.topics.discovery import ALLOWED_CATEGORIES, COMMERCIAL_INTENT_GUIDE, _build_messages
from tests.article_helpers import DEFAULT_SUMMARY, DEFAULT_TITLE, make_body

REAL_RULES = Path(__file__).resolve().parents[1] / "configs" / "tistory_blog_style_rules.yaml"


def test_every_discovery_category_uses_the_commercial_blueprint() -> None:
    for category in ALLOWED_CATEGORIES:
        profile = load_style_profile(REAL_RULES, "교통사고 합의금 계산법", category)
        assert (profile.mode, profile.blueprint) == (LIFESTYLE, COMMERCIAL_BLUEPRINT), category
        assert "문제 해결·행동 안내형" in profile.system_rules
        assert "공공 무료 상담·조회 창구" in profile.system_rules
        assert "자주 묻는 질문" in profile.system_rules


def test_other_categories_keep_topic_based_blueprints() -> None:
    assert load_style_profile(REAL_RULES, "정부24 등본 발급 방법", "정부지원·민원").blueprint == "how_to"
    assert load_style_profile(REAL_RULES, "정부24 발급 오류 해결", None).blueprint == "troubleshooting"


def test_commercial_blueprint_falls_back_when_rules_do_not_define_it(mini_rules_path: Path) -> None:
    # 사용자가 YAML에서 commercial_solution을 지워도 글 생성은 기본 구조로 계속된다
    assert load_style_profile(mini_rules_path, "개인회생 신청 방법", "대출·부채·금융").blueprint == "how_to"


def _validate(profile: StyleProfile, body_html: str) -> dict[str, object]:
    issues = validate_article(
        title=DEFAULT_TITLE,
        summary=DEFAULT_SUMMARY,
        tags=["가", "나", "다", "라", "마"],
        body=sanitize_html(body_html, title=DEFAULT_TITLE),
        profile=profile,
    )
    return {issue.code: issue for issue in issues}


def test_ad_click_inducement_is_an_error(mini_profile: StyleProfile) -> None:
    body = make_body() + "\n<p>도움이 되셨다면 아래 광고를 한 번 클릭해 주세요.</p>"

    issue = _validate(mini_profile, body)["policy.ad_click_inducement"]

    assert issue.severity == ERROR and "애드센스" in issue.message


def test_scam_warning_about_ads_is_not_flagged(mini_profile: StyleProfile) -> None:
    body = make_body() + "\n<p>배너 광고를 클릭하기 전에 주소가 공식 기관인지 확인하세요. 허위 광고에 속지 마세요.</p>"

    assert "policy.ad_click_inducement" not in _validate(mini_profile, body)


def test_commercial_blueprint_wants_steps_and_a_table(mini_profile: StyleProfile) -> None:
    profile = replace(mini_profile, blueprint=COMMERCIAL_BLUEPRINT)
    body = make_body().replace("<ol>", "<ul>").replace("</ol>", "</ul>")

    issues = _validate(profile, body)

    assert issues["structure.steps_missing"].severity == WARN
    assert issues["structure.table_missing"].severity == WARN


def test_guarantee_phrases_are_restricted_in_the_real_rules() -> None:
    profile = load_style_profile(REAL_RULES, "개인회생 신청 방법", "대출·부채·금융")
    assert {"수익 보장", "승소 보장", "100% 승인"} <= set(profile.restricted_phrases)


def test_lifestyle_prompt_carries_ad_policy_and_ymyl_rules(mini_profile: StyleProfile) -> None:
    system = build_system_prompt(mini_profile)
    user = build_user_prompt(ArticleRequest(topic="실손보험 청구 거절 대처법"), mini_profile, date(2026, 10, 2))

    assert "광고를 클릭" in system and "누르라고 권하거나" in system
    assert "매수·매도 시점" in system
    # 행동 안내는 확인 가능한 공공 창구로, 업체 추천 없이
    assert "금융감독원 1332" in user and "웹 검색으로 확인한 것만" in user
    assert "특정 업체·전문가·상품을 추천하지 않는다" in user


def test_technical_prompt_has_no_ad_policy_block() -> None:
    profile = load_style_profile(REAL_RULES, "Spring JPA 연동 방법 예제 코드")
    assert "광고를 클릭" not in build_system_prompt(profile)


def test_discovery_prompts_ask_for_commercial_search_intent() -> None:
    for focus_sns in (False, True):
        user = _build_messages(focus_sns=focus_sns)[1]["content"]
        assert COMMERCIAL_INTENT_GUIDE in user
    assert "신청 자격" in COMMERCIAL_INTENT_GUIDE and "predict the price" in COMMERCIAL_INTENT_GUIDE
