"""블로그 문체·구조 규칙(configs/tistory_blog_style_rules.yaml)을 읽어 글 생성 프롬프트와 검증 기준으로 변환한다.

YAML이 단일 진실 공급원이다. 규칙 문구를 코드에 복제하지 않고, 파일에 적힌 값을 그대로 렌더링한다.
파일을 읽지 못하면 내장 기본 규칙으로 대체하고 그 사실을 profile.fallback_reason에 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

import yaml

from app.core.logging import log_event

_logger = logging.getLogger("tistory_automation")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIFESTYLE = "lifestyle"
TECHNICAL = "technical"
# 제목 방향: clickbait는 주제 탐색(discovery)의 어그로·고CTR 지향을 따르고, persona는 YAML의 차분한 제목 규칙을 따른다.
CLICKBAIT = "clickbait"
PERSONA = "persona"

DEFAULT_RANGES: dict[str, tuple[int, int]] = {
    "title_chars": (28, 44),
    "body_chars": (2200, 4200),
    "h2_count": (4, 8),
    "emoji_count": (3, 6),
    "summary_chars": (90, 150),
}
TECHNICAL_RANGES: dict[str, tuple[int, int]] = {
    "title_chars": (15, 60),
    "body_chars": (1800, 5000),
    "h2_count": (3, 10),
    "emoji_count": (0, 0),
    "summary_chars": (80, 150),
}
DEFAULT_RESTRICTED = ("완벽 정리", "싹 다 조사", "전기세 폭탄", "무조건", "100% 해결", "지금 안 보면 손해", "이것만 하면 끝")

# 오탐을 줄이기 위해 "코드", "서버"처럼 생활 글에도 흔한 단어는 기술 글 신호에서 뺀다.
_TECHNICAL_ASCII_TOKENS = (
    "api", "sdk", "sql", "java", "spring", "python", "javascript", "typescript", "react", "docker",
    "kubernetes", "git", "json", "http", "linux", "aws", "jpa", "orm", "html", "css", "nginx", "redis",
)
_TECHNICAL_KOREAN_WORDS = (
    "프로그래밍", "개발자", "디버깅", "프레임워크", "라이브러리", "데이터베이스", "알고리즘", "컴파일",
    "소스 코드", "소스코드", "코딩", "스택 트레이스", "예외 처리", "쿼리",
)
_TECHNICAL_ASCII_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(_TECHNICAL_ASCII_TOKENS) + r")(?![a-z0-9])")
# 기술 단어 하나만으로는 기술 글이 아니다("개발자 종합소득세 신고", "HTML 몰라도 되는 정부24 신청"). 구현·개념 설명을 뜻하는 단어가 함께 있어야 한다.
_TECHNICAL_INTENT_WORDS = (
    "연동", "구현", "설정", "설치", "코드", "예제", "문법", "함수", "변수", "클래스", "객체", "인터페이스", "메서드", "어노테이션",
    "컴포넌트", "상태 관리", "의존성", "트랜잭션", "비동기", "스레드", "빌드", "배포", "호출", "예외", "에러", "exception",
    "디버깅", "이란", "란 무엇", "개념", "원리", "아키텍처", "엔드포인트",
)

_BLUEPRINT_RULES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    LIFESTYLE: (
        ("troubleshooting", re.compile(r"오류|에러|안\s?될\s?때|안\s?되는|안\s?돼|실패|해결|문제|불가|거절|반려")),
        ("comparison", re.compile(r"차이|비교|vs|versus|어떤\s?게|뭐가\s?더|중\s?어느|고르는|선택")),
        ("saving", re.compile(r"절약|아끼|줄이는|줄이기|싸게|최저가|알뜰|절세")),
    ),
    TECHNICAL: (
        ("concept_comparison", re.compile(r"차이|비교|vs|versus")),
        ("debugging", re.compile(r"오류|에러|exception|예외|실패|안\s?될\s?때|해결")),
        ("implementation_howto", re.compile(r"방법|사용법|설정|구현|연동|적용|설치|만들기")),
    ),
}
_DEFAULT_BLUEPRINT = {LIFESTYLE: "how_to", TECHNICAL: "concept_definition"}
# 고단가(법률·금융·보험·세금 등) 카테고리 글이 쓰는 구조. 어떤 카테고리가 해당하는지는 YAML의 content_routing.commercial_categories가 정한다.
COMMERCIAL_BLUEPRINT = "commercial_solution"

SECTION_LABELS = {
    "reader_problem": "독자가 겪는 문제 공감",
    "early_conclusion": "결론 먼저 제시",
    "requirements": "준비물·신청 조건",
    "numbered_steps": "번호별 진행 절차",
    "mobile_pc_difference_if_needed": "PC·모바일 차이(필요할 때만)",
    "troubleshooting": "막힐 때 해결 방법",
    "warnings": "주의사항",
    "summary": "핵심 요약",
    "next_action": "다음 행동·공식 확인 안내",
    "common_misunderstanding": "흔한 오해",
    "comparison_criteria": "비교 기준",
    "option_analysis": "항목별 분석",
    "comparison_table": "비교표",
    "situation_based_choice": "상황별 선택 기준",
    "pre_selection_checks": "선택 전 확인사항",
    "cost_concern": "비용 걱정 공감",
    "cost_driver": "비용이 달라지는 원리",
    "conditions_to_check": "먼저 확인할 조건",
    "three_to_five_tips": "방법 3~5가지",
    "reason_for_each_tip": "방법별 이유",
    "actions_that_can_backfire": "오히려 손해가 되는 행동",
    "most_likely_cause": "가장 흔한 원인",
    "solutions_by_cause": "원인별 해결 방법",
    "verification_order": "확인 순서",
    "official_contact_if_unresolved": "해결되지 않을 때 공식 문의처",
    "privacy_or_cost_warning": "개인정보·비용 주의",
    "term_context": "용어가 등장하는 상황",
    "demystifying_statement": "어려워 보이는 인상 걷어내기",
    "one_sentence_definition": "한 문장 정의",
    "origin_or_problem_background": "등장 배경·해결하려던 문제",
    "positive_example_if_applicable": "좋은 예제",
    "example_explanation": "예제 해설",
    "misconception_or_negative_example": "흔한 오해·반대 예제",
    "judgment_checklist": "판단 기준 체크리스트",
    "concise_summary": "짧은 정리",
    "historical_origin": "역사적 배경(도움이 될 때만)",
    "authoritative_quote": "검증된 인용(도움이 될 때만)",
    "related_frameworks": "관련 기술",
    "advantages": "장점",
    "limitations": "단점·한계",
    "why_confused": "헷갈리는 이유",
    "definition_a": "A의 정의",
    "definition_b": "B의 정의",
    "common_ground": "공통점",
    "core_differences": "핵심 차이",
    "same_problem_code_examples": "같은 문제의 코드 예제",
    "selection_criteria": "선택 기준",
    "compact_table_if_useful": "비교표(필요할 때만)",
    "problem_to_solve": "해결할 문제",
    "prerequisites_and_versions": "전제 조건과 버전",
    "minimal_configuration": "최소 설정",
    "stepwise_code": "단계별 코드",
    "expected_result": "실행 결과",
    "common_errors": "자주 발생하는 오류",
    "why_it_works": "동작 원리",
    "production_cautions": "운영 시 주의점",
    "symptom_or_error": "증상·오류 메시지",
    "likely_causes": "가능한 원인",
    "minimal_reproduction": "최소 재현",
    "fix_by_cause": "원인별 해결",
    "regression_check": "재발 확인",
    "core_facts_and_regulations": "핵심 기준·자격 조건(표로 정리)",
    "deadline_and_risks": "기한·놓치면 생기는 불이익(확인된 것만)",
    "documents_and_costs": "필요 서류·예상 비용(확인된 것만)",
    "official_free_help": "공공 무료 상담·조회 창구(대표번호·누리집)",
    "expert_consultation_criteria": "전문가 상담이 필요한 경우와 업체·상품 비교 기준",
    "faq": "자주 묻는 질문(<h3> 질문 3~5개와 짧은 답)",
}
BLUEPRINT_LABELS = {
    "how_to": "방법 안내형",
    "comparison": "비교·선택형",
    "saving": "절약형",
    "troubleshooting": "문제 해결형",
    "concept_definition": "개념 정의형",
    "concept_comparison": "개념 비교형",
    "implementation_howto": "구현 방법형",
    "debugging": "디버깅형",
    "commercial_solution": "문제 해결·행동 안내형",
}
_MUST_NOT_SOUND_LIKE = {
    "unverified_expert_authority": "검증되지 않은 전문가 행세",
    "aggressive_sales_copy": "공격적인 판매 문구",
    "fear_based_clickbait": "공포를 이용한 낚시",
    "reader_mockery": "독자 조롱",
    "fabricated_firsthand_reviewer": "직접 써본 척하는 후기",
}
_BOLD_TARGETS = {
    "actual_menu_name": "실제 메뉴명",
    "button_name": "버튼명",
    "key_condition": "핵심 조건",
    "warning_term": "경고어",
}
_SOURCE_REQUIRED_FOR = {
    "numbers": "숫자",
    "ratios": "비율",
    "savings_effect": "절감 효과",
    "prices": "가격",
    "fees": "수수료",
    "benefit_amounts": "혜택 금액",
    "application_periods": "신청 기간",
    "eligibility_conditions": "자격·신청 조건",
    "health_effects": "건강 효과",
    "product_ingredients": "제품 성분",
    "legal_tax_or_policy_claims": "법령·세금·정책 내용",
    "institutional_quotes": "기관 인용",
}
_EMOJI_KINDS = {
    "tip": "실전 팁",
    "warning": "주의사항",
    "comparison": "비교표",
    "summary": "핵심 정리",
    "remember": "기억할 내용",
    "check": "확인 방법",
}
_POSITION_LABELS = {"front": "맨 앞부분", "front_half": "앞쪽 절반"}

_BUILTIN_RULES_TEXT = """\
[페르소나]
복잡한 생활 정보를 대신 확인해 비전문가도 바로 이해하고 따라 할 수 있게 설명하는 친근한 생활 정보 안내자다. 독자가 어렵다고 느끼는 주제의 거품을 걷어내고 핵심 구조를 보여 준다. 정확성이 문체보다 우선한다.

[문체]
- 기본 어투는 해요체이고, 정확한 기준과 주의사항은 합니다체로 쓴다.
- 한 문단은 1~3문장, 한 문장은 대체로 20~45자로 쓴다.
- 글의 앞쪽 20% 안에 핵심 답(결론)을 먼저 제시한다.

[글 구조]
독자 문제 공감 → 결론 먼저 → 준비물·조건 → 번호별 절차 → 주의사항 → 핵심 요약 → 공식 확인 안내 순서로 쓴다. 비교가 필요하면 표(<table>)를 쓴다.

[사실성 규칙]
- 개인 경험·후기·인용문을 지어내지 않는다.
- 숫자·금액·기간·신청 조건·법령 내용은 확인된 출처가 있을 때만 쓰고, 확인되지 않으면 삭제하거나 한정해서 쓴다."""

_BUILTIN_PERSONA_TITLE = """\
[제목(<article_title>)]
- 핵심 검색어를 앞쪽에 두고, 느낌표와 이모지를 쓰지 않는다.

[금지·제한 표현]
- 과장·낚시 표현(예: 완벽 정리, 무조건, 100% 해결, 지금 안 보면 손해)은 글 전체에서 합계 1회 이하로 제한한다."""


@dataclass(frozen=True)
class StyleProfile:
    mode: str
    blueprint: str
    system_rules: str
    title_chars: tuple[int, int]
    body_chars: tuple[int, int]
    h2_count: tuple[int, int]
    emoji_count: tuple[int, int]
    summary_chars: tuple[int, int]
    restricted_phrases: tuple[str, ...]
    restricted_max_total: int
    checklist: tuple[str, ...]
    source: str
    title_style: str = CLICKBAIT
    fallback_reason: str = ""


_rules_cache: dict[Path, tuple[int, dict[str, Any]]] = {}


def route_mode(topic: str, category: str | None = None) -> str:
    """주제가 기술 글(코드·개념 설명)인지 생활 글인지 분류한다.

    기술 단어가 둘 이상이거나, 하나라도 구현·개념 설명 단어와 함께 있을 때만 기술 글이다. 모호하면 생활 글이다.
    """
    text = f"{topic} {category or ''}".lower()
    signals = {match.group() for match in _TECHNICAL_ASCII_RE.finditer(text)}
    signals |= {word for word in _TECHNICAL_KOREAN_WORDS if word in text}
    if len(signals) >= 2:
        return TECHNICAL
    if signals and any(word in text for word in _TECHNICAL_INTENT_WORDS):
        return TECHNICAL
    return LIFESTYLE


def route_blueprint(topic: str, mode: str) -> str:
    """주제 문구로 글 구조(블루프린트)를 고른다."""
    lowered = topic.lower()
    for blueprint, pattern in _BLUEPRINT_RULES.get(mode, ()):
        if pattern.search(lowered):
            return blueprint
    return _DEFAULT_BLUEPRINT.get(mode, "how_to")


def load_style_profile(
    path: Path,
    topic: str,
    category: str | None = None,
    mode: str | None = None,
    title_style: str = CLICKBAIT,
) -> StyleProfile:
    """규칙 파일과 주제를 바탕으로 StyleProfile을 만든다. 파일을 읽지 못하면 내장 기본 규칙을 쓴다."""
    title_style = title_style if title_style in (CLICKBAIT, PERSONA) else CLICKBAIT
    chosen_mode = mode if mode in (LIFESTYLE, TECHNICAL) else route_mode(topic, category)
    rules, error = _read_rules(path)
    if rules is not None and chosen_mode not in rules["modes"]:
        chosen_mode = LIFESTYLE if LIFESTYLE in rules["modes"] else ""
        if not chosen_mode:
            rules, error = None, "스타일 규칙 파일에 사용할 수 있는 모드(lifestyle/technical)가 없습니다."
    if rules is None:
        chosen_mode = LIFESTYLE
        log_event(_logger, "style_rules_fallback", path=str(path), reason=error)
        return _builtin_profile(route_blueprint(topic, LIFESTYLE), error, title_style)

    blueprint = route_blueprint(topic, chosen_mode)
    if chosen_mode == LIFESTYLE and _is_commercial_category(rules, category):
        blueprint = COMMERCIAL_BLUEPRINT
    if blueprint not in _dig(rules, "modes", chosen_mode, "article_blueprints", default={}):
        blueprint = _DEFAULT_BLUEPRINT[chosen_mode]
    try:
        return _profile_from_rules(rules, chosen_mode, blueprint, str(path), title_style)
    except (AttributeError, TypeError, KeyError, ValueError) as error:
        # 사용자가 YAML을 고치다 값의 형식을 잘못 넣은 경우: 글 생성을 막지 않고 내장 규칙으로 쓰되 이유를 남긴다.
        reason = f"스타일 규칙의 형식이 올바르지 않습니다({type(error).__name__}: {error})."
        log_event(_logger, "style_rules_fallback", path=str(path), reason=reason)
        return _builtin_profile(route_blueprint(topic, LIFESTYLE), reason, title_style)


def _is_commercial_category(rules: dict[str, Any], category: str | None) -> bool:
    """카테고리가 YAML content_routing.commercial_categories 중 하나인가(공백 차이는 무시)."""
    if not category:
        return False
    wanted = re.sub(r"\s+", "", category)
    return any(re.sub(r"\s+", "", item) == wanted for item in _string_list(_dig(rules, "content_routing", "commercial_categories")))


def _read_rules(path: Path) -> tuple[dict[str, Any] | None, str]:
    candidate = path
    if not candidate.is_absolute() and not candidate.exists():
        candidate = PROJECT_ROOT / path
    try:
        mtime = candidate.stat().st_mtime_ns
    except OSError:
        return None, f"스타일 규칙 파일을 찾을 수 없습니다: {path}"
    cached = _rules_cache.get(candidate)
    if cached and cached[0] == mtime:
        return cached[1], ""
    try:
        rules = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return None, f"스타일 규칙 파일을 읽지 못했습니다: {error}"
    if not isinstance(rules, dict) or not isinstance(rules.get("modes"), dict):
        return None, "스타일 규칙 파일 형식이 올바르지 않습니다(modes 항목 필요)."
    _rules_cache[candidate] = (mtime, rules)
    return rules, ""


def _builtin_profile(blueprint: str, reason: str, title_style: str) -> StyleProfile:
    ranges = DEFAULT_RANGES
    title_rules = _clickbait_title_section(ranges["title_chars"]) if title_style == CLICKBAIT else _BUILTIN_PERSONA_TITLE
    return StyleProfile(
        mode=LIFESTYLE,
        blueprint=blueprint if blueprint in ("how_to", "comparison", "saving", "troubleshooting") else "how_to",
        system_rules=f"{_BUILTIN_RULES_TEXT}\n\n{title_rules}",
        title_chars=ranges["title_chars"],
        body_chars=ranges["body_chars"],
        h2_count=ranges["h2_count"],
        emoji_count=ranges["emoji_count"],
        summary_chars=ranges["summary_chars"],
        restricted_phrases=DEFAULT_RESTRICTED,
        restricted_max_total=1,
        checklist=(),
        source="builtin",
        title_style=title_style,
        fallback_reason=reason,
    )


def _clickbait_title_section(title_chars: tuple[int, int]) -> str:
    """주제 탐색의 어그로·고CTR 지향을 글 제목에도 적용하되, 제목이 약속한 내용은 본문이 지키게 한다."""
    return f"""\
[제목(<article_title>) — 클릭을 부르는 제목]
- 이 글의 제목은 클릭을 부르는 제목으로 쓴다. 위 [페르소나]의 '공포 자극·단정 지양' 지침은 본문에 적용하고, 제목은 이 섹션을 우선한다.
- 다음 장치 중 하나 이상을 반드시 쓴다: 구체적인 숫자, '~일까?'·'~했다면?' 같은 질문형, 놓치기 쉬운 함정과 손해 회피('모르면 손해'), 달라진 점(Before/After), 시행·마감 시점.
- 형태 예시(문장을 베끼지 말고 구조만 참고한다): "{{현상}}? {{신청·확인 전에}} 꼭 볼 {{N}}가지" / "{{주제}}, 아직도 {{비효율적인 방식}}이라면? {{핵심 결과}}" / "{{수치}}가 달라진다? {{대상}}이라면 지금 확인할 것"
- 길이는 {title_chars[0]}~{title_chars[1]}자 안팎으로 쓰고, 핵심 검색어는 제목의 앞쪽 절반에 그대로 넣는다.
- 주제 후보 문장이 이미 클릭을 부르는 제목 형태라면 그 문장의 훅을 살리고 길이와 정확성만 다듬는다.
- 제목이 약속한 내용은 본문이 실제로 답해야 하고, 본문과 출처에 없는 숫자·마감·혜택은 제목에 쓰지 않는다.
- 느낌표는 최대 1개, 이모지는 쓰지 않는다."""


def _profile_from_rules(
    rules: dict[str, Any], mode: str, blueprint: str, source: str, title_style: str
) -> StyleProfile:
    defaults = TECHNICAL_RANGES if mode == TECHNICAL else DEFAULT_RANGES
    mode_rules = rules["modes"][mode]
    controls = _dig(mode_rules, "generation_controls", default={})
    seo_key = "technical_meta_description" if mode == TECHNICAL else "lifestyle_meta_description"
    restricted = _dig(rules, "restricted_expressions", mode, default={})
    emoji_enabled = _dig(mode_rules, "emoji", "enabled", default=mode != TECHNICAL)

    emoji_range = (
        _range(_dig(mode_rules, "emoji", "per_article"), defaults["emoji_count"]) if emoji_enabled else (0, 0)
    )
    h2_min = _as_int(controls.get("minimum_h2_sections"), defaults["h2_count"][0])
    h2_max = _as_int(controls.get("maximum_h2_sections"), defaults["h2_count"][1])
    checklist = _string_list(_dig(rules, "quality_checklists", "common")) + _string_list(
        _dig(rules, "quality_checklists", mode)
    )
    return StyleProfile(
        mode=mode,
        blueprint=blueprint,
        system_rules=_render_rules(rules, mode, blueprint, title_style),
        title_chars=_range(_dig(mode_rules, "title_rules", "length_chars"), defaults["title_chars"]),
        body_chars=_range(controls.get("recommended_body_length_chars"), defaults["body_chars"]),
        h2_count=(h2_min, max(h2_min, h2_max)),
        emoji_count=emoji_range,
        summary_chars=_range(_dig(rules, "seo", seo_key, "length_chars"), defaults["summary_chars"]),
        restricted_phrases=tuple(_string_list(restricted.get("items"))) or DEFAULT_RESTRICTED,
        restricted_max_total=_as_int(restricted.get("max_total_per_article"), 1),
        checklist=tuple(checklist),
        source=source,
        title_style=PERSONA if mode == TECHNICAL else title_style,
    )


def _render_rules(rules: dict[str, Any], mode: str, blueprint: str, title_style: str) -> str:
    mode_rules = rules["modes"][mode]
    technical = mode == TECHNICAL
    out: list[str] = []

    def section(title: str, lines: list[str]) -> None:
        lines = [line for line in lines if line]
        if lines:
            out.append(f"[{title}]\n" + "\n".join(lines))

    persona = _text(_dig(rules, "llm_instructions", mode))
    voice_goal = _text(_dig(rules, "common_voice", "role_description"))
    avoid_voices = [
        _MUST_NOT_SOUND_LIKE.get(item, item) for item in _string_list(_dig(rules, "common_voice", "must_not_sound_like"))
    ]
    section(
        "페르소나",
        [
            persona,
            f"공통 원칙: {voice_goal} 정확성이 문체보다 우선한다." if voice_goal else "",
            f"다음처럼 들리지 않게 쓴다: {', '.join(avoid_voices)}." if avoid_voices else "",
        ],
    )

    audience = _dig(mode_rules, "profile", "target_audience", default={})
    section(
        "독자",
        [
            f"- 주 독자: {_text(audience.get('primary'))}" if audience.get("primary") else "",
            f"- 함께 고려할 독자: {'; '.join(_string_list(audience.get('secondary')))}" if audience.get("secondary") else "",
            "- 지식 수준: 입문(전문용어는 쉬운 말로 풀어 쓴다)" if "beginner" in _text(audience.get("knowledge_level")) else "",
        ],
    )

    style = _dig(mode_rules, "sentence_style", default={})
    sentence = _range(style.get("preferred_sentence_length_chars"), (20, 45))
    paragraph = _range(style.get("paragraph_sentence_count"), (1, 3))
    question = style.get("rhetorical_questions_per_article")
    exclaim = style.get("exclamatory_sentences_per_article")
    answer_pct = style.get("first_answer_position_percent_max") or style.get("definition_position_percent_max")
    section(
        "문체",
        [
            f"- 기본 어투: {_text(_dig(mode_rules, 'voice', 'base_register'))}",
            f"- 한 문장은 대체로 {sentence[0]}~{sentence[1]}자, 한 문단은 {paragraph[0]}~{paragraph[1]}문장으로 쓰고 문단마다 핵심은 하나만 둔다.",
            f"- 글의 앞쪽 {answer_pct}% 안에 핵심 답(결론·정의)을 먼저 제시한다." if answer_pct else "",
            _count_rule("수사 의문문", question, "회"),
            _count_rule("감탄문", exclaim, "문장"),
            "- 느낌표를 연속해서 쓰지 않는다." if style.get("multiple_exclamation_marks") is False else "",
            *_tone_examples(_dig(mode_rules, "tone_patterns", default={})),
        ],
    )

    blueprint_rules = _dig(mode_rules, "article_blueprints", blueprint, default={})
    required = _string_list(blueprint_rules.get("sections") or blueprint_rules.get("required_sections"))
    conditional = _string_list(blueprint_rules.get("conditional_sections"))
    flow = " → ".join(f"{i}) {SECTION_LABELS.get(key, key.replace('_', ' '))}" for i, key in enumerate(required, 1))
    common = _dig(rules, "common_structure", default={})
    section(
        f"글 구조: {BLUEPRINT_LABELS.get(blueprint, blueprint)}",
        [
            f"- 다음 흐름을 따른다(내용에 맞게 자연스럽게 합치거나 생략할 수 있다): {flow}" if flow else "",
            f"- 필요할 때만 추가: {', '.join(SECTION_LABELS.get(key, key) for key in conditional)}" if conditional else "",
            "- 예시·표·코드·절차 뒤에는 무엇을 봐야 하는지 해설을 붙인다." if common.get("example_requires_explanation") else "",
            "- 장점만 나열하지 말고 의미 있는 한계와 주의점도 함께 쓴다(근거 없는 단점을 만들지 않는다)." if common.get("tradeoffs_when_relevant") else "",
        ],
    )

    headings = _dig(mode_rules, "headings", default={})
    controls = _dig(mode_rules, "generation_controls", default={})
    h2_min = _as_int(controls.get("minimum_h2_sections"), 4)
    h2_max = _as_int(controls.get("maximum_h2_sections"), 8)
    body = _range(controls.get("recommended_body_length_chars"), DEFAULT_RANGES["body_chars"])
    section(
        "소제목과 분량",
        [
            f"- 소제목은 <h2> {h2_min}~{h2_max}개, 필요하면 <h3>까지만 쓴다. 소제목은 내용을 구체적으로 드러낸다.",
            f"- 권장 소제목 형태: {' / '.join(_string_list(headings.get('recommended_patterns')))}" if headings.get("recommended_patterns") else "",
            f"- 소제목에 쓰지 않는 표현: {', '.join(_string_list(headings.get('avoid')))}" if headings.get("avoid") else "",
            f"- 본문 분량은 공백 포함 {body[0]:,}~{body[1]:,}자(HTML 태그 제외)로 쓴다. 분량을 채우려고 같은 말을 반복하지 않는다.",
            "- 핵심 요약 구역을 포함한다." if controls.get("include_summary") else "",
            "- 금액·기간·요건처럼 시점에 민감한 내용에는 '2026년 10월 기준'처럼 기준 시점을 본문에 밝힌다."
            if controls.get("include_verified_date_for_time_sensitive_topics")
            else "",
            f"- 비유는 최대 {controls.get('one_analogy_max')}회만 쓴다." if controls.get("one_analogy_max") else "",
            "- 개인 경험담은 제공되지 않았으므로 쓰지 않는다." if controls.get("use_personal_anecdote") == "only_if_explicitly_supplied" else "",
        ],
    )

    fmt = _dig(mode_rules, "formatting", default={})
    table = _dig(fmt, "table", default={})
    bold = [_BOLD_TARGETS.get(item, item) for item in _string_list(fmt.get("use_bold_for"))]
    format_lines = [
        "- 서식은 마크다운이 아니라 HTML로 쓴다(굵게=<strong>, 번호 목록=<ol>, 목록=<ul>, 표=<table>).",
        f"- <strong>으로 강조할 대상: {', '.join(bold)}" if bold else "",
        f"- 표는 {_table_use_cases(table)}에 쓰고, 최대 {table.get('max_columns', 4)}열·짧은 셀 문장으로 쓴다."
        if table
        else "",
        f"- 선택지가 {controls.get('comparison_table_when_options_gte')}개 이상이면 비교표를 넣는다." if controls.get("comparison_table_when_options_gte") else "",
        f"- 실행할 행동이 {controls.get('numbered_steps_when_actions_gte')}개 이상이면 번호 목록으로 쓴다." if controls.get("numbered_steps_when_actions_gte") else "",
    ]
    if technical:
        code = _dig(mode_rules, "code_rules", default={})
        format_lines += [
            "- 코드는 <pre><code class=\"language-java\"> 형식(언어명 표기)으로 쓰고, 코드 바로 아래에 해설을 2~5문장 붙인다.",
            "- 존재하지 않는 클래스·메서드·설정을 만들지 않고, 버전에 따라 달라지는 내용에는 버전을 표시한다."
            if code.get("fake_api_or_class_allowed") is False
            else "",
            f"- 코드 블록은 기본적으로 {code.get('max_code_blocks_default')}개 이하로 쓴다." if code.get("max_code_blocks_default") else "",
        ]
    section("서식", format_lines)

    emoji = _dig(mode_rules, "emoji", default={})
    if emoji.get("enabled") and mode != TECHNICAL:
        per = _range(emoji.get("per_article"), DEFAULT_RANGES["emoji_count"])
        allowed = " ".join(f"{icon}({_EMOJI_KINDS.get(kind, kind)})" for kind, icon in _dig(emoji, "allowed", default={}).items())
        section(
            "이모지",
            [
                f"- 소제목 앞에 구분용으로만 글 전체 {per[0]}~{per[1]}개를 쓴다. 허용: {allowed}" if allowed else f"- 글 전체 {per[0]}~{per[1]}개",
                "- 제목과 본문 문장 속에는 이모지를 쓰지 않고, 같은 이모지를 반복하지 않는다.",
            ],
        )
    else:
        section("이모지", ["- 이모지를 쓰지 않는다."])

    title_rules = _dig(mode_rules, "title_rules", default={})
    title_len = _range(title_rules.get("length_chars"), DEFAULT_RANGES["title_chars"] if not technical else TECHNICAL_RANGES["title_chars"])
    clickbait = title_style == CLICKBAIT and not technical  # 기술 글은 항상 YAML의 차분한 제목 규칙을 따른다
    if clickbait:
        out.append(_clickbait_title_section(title_len))
    else:
        position = _POSITION_LABELS.get(_text(title_rules.get("primary_keyword_position")), "")
        section(
            "제목(<article_title>)",
            [
                f"- 길이는 {title_len[0]}~{title_len[1]}자로 쓴다.",
                f"- 핵심 검색어를 제목의 {position}에 그대로 넣는다." if position else "",
                "- 느낌표와 이모지를 쓰지 않는다.",
                f"- 강한 홍보 문구는 제목에 최대 {title_rules.get('max_strong_marketing_phrase')}개만 쓴다." if title_rules.get("max_strong_marketing_phrase") is not None else "",
                f"- 권장 패턴({{}}는 내용에 맞게 채운다): {' / '.join(_string_list(title_rules.get('patterns')))}" if title_rules.get("patterns") else "",
                f"- 제목에 쓰지 않는 표현: {', '.join(_string_list(title_rules.get('avoid')))}" if title_rules.get("avoid") else "",
                "- 제목과 본문 내용이 어긋나지 않게 쓴다.",
            ],
        )

    restricted = _dig(rules, "restricted_expressions", mode, default={})
    limit = _as_int(restricted.get("max_total_per_article"), 1)
    items = _string_list(restricted.get("items"))
    preferred = _dig(rules, "preferred_expressions", default={})
    repeat = _dig(preferred, "common", "max_repetition_per_expression", default=2)
    scope = "본문 전체에서(제목의 훅 표현은 예외)" if clickbait else "제목과 본문을 합쳐 전체에서"
    section(
        "금지·제한 표현",
        [
            (
                f"- 다음 표현은 {scope} {limit}회 이하로만 쓴다: " + ", ".join(f"'{item}'" for item in items)
                if limit
                else "- 다음 표현은 쓰지 않는다: " + ", ".join(f"'{item}'" for item in items)
            )
            if items
            else "",
            f"- 자주 쓰는 말버릇은 같은 표현을 {repeat}회를 넘겨 반복하지 않는다.",
        ],
    )

    guard = _dig(rules, "factuality_guardrails", default={})
    mode_guard = _dig(guard, mode, default={})
    required_for = [_SOURCE_REQUIRED_FOR.get(item, item) for item in _string_list(mode_guard.get("source_required_for"))]
    fact_lines = [
        "- 웹 검색으로 확인한 사실과 제공된 자료만 근거로 쓴다. 공식 기관 자료를 우선한다.",
        "- 직접 써 본 경험, 후기, 가족 사례, 인용문을 지어내지 않는다.",
        f"- 다음 내용에는 확인된 출처가 있어야 한다: {', '.join(required_for)}." if required_for else "",
        "- 확인되지 않은 숫자·조건은 삭제하거나 '~로 안내되어 있어요'처럼 한정해서 쓴다." if mode_guard.get("unverified_numeric_claim_action") else "",
        "- 정책·제도 내용은 바뀔 수 있으므로 단정하지 말고 공식 안내 확인을 안내한다." if mode_guard.get("professional_confirmation_when_needed") else "",
    ]
    if technical:
        fact_lines += [
            "- 정의는 공식 문서 기준으로 검증하고, 하나의 신호(상속·어노테이션 등)만으로 개념을 단정하지 않는다.",
            "- 역사적 사실과 인용문은 출처를 확인한 경우에만 쓴다.",
        ]
    section("사실성 규칙(문체보다 우선)", fact_lines)

    seo = _dig(rules, "seo", default={})
    summary_range = _range(_dig(seo, seo_key_for(mode), "length_chars"), DEFAULT_RANGES["summary_chars"])
    summary_structure = _text(_dig(seo, seo_key_for(mode), "structure"))
    section(
        "검색 노출(SEO)",
        [
            "- 핵심 키워드를 제목, 글 앞부분, 소제목 한 곳(자연스러울 때), 요약문에 그대로 넣되 반복해서 도배하지 않는다." if seo.get("use_exact_primary_keyword") else "",
            "- 검색한 사람이 알고 싶은 답을 먼저 준다." if seo.get("search_intent_first") else "",
            f"- <article_summary>는 {summary_range[0]}~{summary_range[1]}자로, '{summary_structure}' 구조로 쓴다." if summary_structure else f"- <article_summary>는 {summary_range[0]}~{summary_range[1]}자로 쓴다.",
        ],
    )

    checklist = _string_list(_dig(rules, "quality_checklists", "common")) + _string_list(_dig(rules, "quality_checklists", mode))
    section("작성 후 스스로 점검할 항목", [f"- {_checklist_item(item)}" for item in checklist])

    return "\n\n".join(out)


def _checklist_item(item: str) -> str:
    # 확인일 안내 문구는 시스템이 글 끝에 붙인다(prompts의 출력 규칙). 본문에는 기준 시점만 밝히게 해서 두 지시가 부딪히지 않게 한다.
    if "확인일" in item:
        return f"{item} (글 끝의 확인일 안내 문구는 시스템이 붙인다. 본문에는 'YYYY년 M월 기준'처럼 기준 시점만 밝힌다)"
    return item


def seo_key_for(mode: str) -> str:
    return "technical_meta_description" if mode == TECHNICAL else "lifestyle_meta_description"


def _tone_examples(tone: dict[str, Any]) -> list[str]:
    labels = {
        "opening": "도입",
        "early_answer": "결론 제시",
        "demystify": "진입 장벽 낮추기",
        "definition": "정의",
        "transition": "전환",
        "closing": "마무리",
    }
    lines: list[str] = []
    for key, label in labels.items():
        examples = _string_list(tone.get(key))[:2]
        if examples:
            lines.append(f"  · {label}: " + " / ".join(f'"{example}"' for example in examples))
    if lines:
        lines.insert(0, "- 말투 예시({}는 내용에 맞게 채운다. 문장을 그대로 베끼지 말고 어조만 참고한다):")
    return lines


_TABLE_USE_CASES = {
    "compare_two_or_more_options": "둘 이상의 선택지를 비교할 때",
    "summarize_conditions_fees_or_periods": "조건·비용·기간을 요약할 때",
    "present_selection_criteria": "선택 기준을 보여 줄 때",
    "compare_multiple_dimensions": "여러 기준을 나란히 비교할 때",
    "summarize_selection_criteria": "선택 기준을 요약할 때",
}


def _table_use_cases(table: dict[str, Any]) -> str:
    cases = [_TABLE_USE_CASES.get(item, item.replace("_", " ")) for item in _string_list(table.get("use_when"))]
    return ", ".join(cases) if cases else "비교·요약이 필요할 때"


def _count_rule(label: str, value: Any, unit: str) -> str:
    if not isinstance(value, dict):
        return ""
    low, high = _as_int(value.get("min"), 0), _as_int(value.get("max"), 0)
    if high == 0:
        return f"- {label}은 쓰지 않는다."
    return f"- {label}은 글 전체에서 {low}~{high}{unit} 이내로 쓴다."


def _dig(data: Any, *keys: str, default: Any = None) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current if current is not None else default


def _range(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    if not isinstance(value, dict):
        return default
    low = _as_int(value.get("min"), default[0])
    high = _as_int(value.get("max"), default[1])
    return (low, max(low, high))


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]
