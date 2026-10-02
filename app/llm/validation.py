"""생성된 글을 스타일 규칙(StyleProfile) 기준으로 검사한다. LLM을 부르지 않는 결정적 검사만 한다.

severity가 error인 항목은 수정 요청(재작성)의 대상이고, warn은 경고로만 남긴다.
메시지는 그대로 모델에게 줄 수정 지시로 읽히도록 작성한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.llm.parsing import count_emoji
from app.llm.sanitize import SanitizedHtml
from app.llm.style import LIFESTYLE, PERSONA, StyleProfile

ERROR = "error"
WARN = "warn"
TITLE_HARD_MAX = 70
TITLE_HARD_MIN = 12
# 권장 최소 분량의 이 비율에 못 미치면 경고가 아니라 재작성 대상(오류)으로 본다.
BODY_TOO_SHORT_RATIO = 0.75
# 숫자와 바로 뒤의 단위 글자(만·원·%·월 등). 제목의 숫자가 본문에 같은 숫자로 나오는지 볼 때 단위가 다르면(10만 ↔ 10월) 다른 숫자로 본다.
_QUANTITY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?([가-힣%]?)")
_UNITS = frozenset("만억조천백원%명건개월일년세시분초배회차점위호대주")
# 제공되지 않은 개인 경험을 지어낸 문장(스타일 규칙의 fabricated_personal_experience 금지). 공백을 모두 뺀 글에서 찾는다.
_FIRST_PERSON = r"(?:제가|저는|저도|저희(?:가족|집)?(?:은|이)?|내가)"
_TRIED = r"(?:해|써|가|받아|겪어|이용해|사용해|신청해|발급받아|발급해|경험해|확인해|다녀)"
_FABRICATED_EXPERIENCE = re.compile(
    rf"{_FIRST_PERSON}[^.!?]{{0,14}}?(?:{_TRIED}(?:봤|보니|본결과)|다녀왔)"
    rf"|{_FIRST_PERSON}(?:지난[가-힣]{{1,2}}|작년|올해|어제|그저께|얼마전)[^.!?]{{0,14}}?[았었했](?:어요|습니다|는데|다)"
    rf"|(?:직접|실제로){_TRIED}(?:봤|보니|본결과)"
    r"|제경험(?:상|으로는|에의하면)"
)
_SPACES = re.compile(r"\s+")
# 코드 블록 안의 `# 주석`·`**kwargs`는 마크다운이 아니다
_CODE_BLOCKS = re.compile(r"<(pre|code)\b[^>]*>.*?</\1>", re.DOTALL)
_MARKDOWN_LEFTOVER = re.compile(r"\*\*[^*\n]{1,80}\*\*|```|>\s*#{2,6}\s+\S")
_DATE_HINT = re.compile(r"\d{4}년|\d{1,2}월\s*(?:\d{1,2}일\s*)?기준|기준으로")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str


def score(issues: list[Issue]) -> int:
    """수정본이 초안보다 나아졌는지 비교하는 점수(낮을수록 좋다)."""
    return sum(10 if issue.severity == ERROR else 1 for issue in issues)


def validate_article(
    *,
    title: str,
    summary: str,
    tags: list[str],
    body: SanitizedHtml,
    profile: StyleProfile,
) -> list[Issue]:
    issues: list[Issue] = []
    text = body.text
    body_len = len(text)

    _check_title(issues, title, text, profile)
    _check_body_length(issues, body_len, profile)

    h2_min, h2_max = profile.h2_count
    if body.h2_count == 0:
        issues.append(Issue("h2.missing", ERROR, f"소제목(<h2>)이 하나도 없다. 소제목 {h2_min}~{h2_max}개로 글을 구성한다."))
    elif not h2_min <= body.h2_count <= h2_max:
        issues.append(Issue("h2.count", WARN, f"소제목(<h2>)이 {body.h2_count}개다. {h2_min}~{h2_max}개로 맞춘다."))

    emoji_min, emoji_max = profile.emoji_count
    emoji = count_emoji(text)
    if emoji_max == 0 and emoji:
        issues.append(Issue("emoji.forbidden", WARN, f"이모지가 {emoji}개 쓰였다. 이 글에는 이모지를 쓰지 않는다."))
    elif emoji_max and not emoji_min <= emoji <= emoji_max:
        issues.append(Issue("emoji.count", WARN, f"이모지가 {emoji}개다. 소제목 앞에 구분용으로 {emoji_min}~{emoji_max}개만 쓴다."))

    scope = f"{title} {text}" if profile.title_style == PERSONA else text
    found = {phrase: scope.count(phrase) for phrase in profile.restricted_phrases if phrase in scope}
    total = sum(found.values())
    if total > profile.restricted_max_total:
        listed = ", ".join(f"'{phrase}' {count}회" for phrase, count in found.items())
        issues.append(
            Issue(
                "restricted.phrases",
                ERROR,
                f"제한 표현이 {total}회 쓰였다({listed}). 합계 {profile.restricted_max_total}회 이하가 되도록 다른 표현으로 바꾼다.",
            )
        )

    experience = _FABRICATED_EXPERIENCE.search(_SPACES.sub("", text))
    if experience:
        issues.append(
            Issue(
                "experience.fabricated",
                ERROR,
                f"직접 겪은 경험·후기처럼 읽히는 문장이 있다('{experience.group()[:30]}'). 제공되지 않은 개인 경험은 빼고 확인된 정보로 바꾼다.",
            )
        )
    if _MARKDOWN_LEFTOVER.search(_CODE_BLOCKS.sub("", body.html)):
        issues.append(Issue("markdown.leftover", ERROR, "마크다운 기호(**굵게**, ```, ## 등)가 본문에 그대로 남아 있다. HTML 태그로 바꾼다."))
    _check_structure(issues, body, profile)
    _check_meta(issues, summary, tags, text, profile)
    return issues


def _check_title(issues: list[Issue], title: str, text: str, profile: StyleProfile) -> None:
    if not title.strip():
        issues.append(Issue("title.empty", ERROR, "제목이 비어 있다."))
        return
    low, high = profile.title_chars
    length = len(title)
    if length > TITLE_HARD_MAX or length < TITLE_HARD_MIN:
        issues.append(Issue("title.length", ERROR, f"제목이 {length}자다. 검색 결과에서 잘리거나 정보가 부족하다. {low}~{high}자 안팎으로 다시 쓴다."))
    elif not low <= length <= high:
        issues.append(Issue("title.length", WARN, f"제목이 {length}자다. {low}~{high}자 안팎으로 맞춘다."))

    allowed_exclaim = 1 if profile.title_style != PERSONA else 0
    if title.count("!") + title.count("！") > allowed_exclaim:
        issues.append(Issue("title.exclaim", WARN, f"제목의 느낌표를 {allowed_exclaim}개 이하로 줄인다."))
    if profile.title_style != PERSONA and not _has_title_hook(title):
        issues.append(
            Issue(
                "title.flat",
                ERROR,
                "제목에 클릭을 부르는 장치(질문형, 구체적 숫자, 손해 회피, 달라진 점 등)가 없다. 제목만 다시 써서 "
                "장치를 하나 이상 넣되, 본문이 실제로 답하는 내용만 약속한다.",
            )
        )

    body_quantities = _quantities(text, integer_parts=True)
    for number, unit in _title_numbers(title):
        if not any(n == number and (not unit or not u or u == unit) for n, u in body_quantities):
            issues.append(
                Issue(
                    "title.number_unsupported",
                    ERROR,
                    f"제목에 쓴 숫자 '{number}'은(는) 본문에서 확인되지 않는다. 본문에서 근거와 함께 다루거나 제목에서 뺀다.",
                )
            )


_TITLE_HOOK_WORDS = (
    "모르면", "손해", "꼭 ", "반드시", "함정", "놓치", "아직도", "왜 ", "주의", "마감", "실수", "숨은", "진짜", "이제", "당장", "필수",
    "이것만", "몰랐", "후회", "비밀", "충격", "의외", "알고 보니", "달라지", "달라져", "달라진", "바뀌", "바뀝", "바뀐",
)
# 질문 부호, 또는 글자에 붙지 않은 숫자. '정부24'·'민원24'처럼 이름에 붙은 숫자는 구체적인 숫자 장치가 아니다.
_TITLE_HOOK_PATTERN = re.compile(r"[?？]|(?<![가-힣A-Za-z\d])\d")


def _has_title_hook(title: str) -> bool:
    """제목에 클릭을 부르는 장치(질문, 구체적 숫자, 손해 회피·변화를 말하는 단어)가 하나라도 있는가."""
    return bool(_TITLE_HOOK_PATTERN.search(title)) or any(word in title for word in _TITLE_HOOK_WORDS)


def _quantities(text: str, *, integer_parts: bool = False) -> list[tuple[str, str]]:
    """(숫자, 단위) 목록. 쉼표를 뺀 숫자 전체로 비교한다(70이 170이나 2070에 걸리지 않게). 단위는 목록에 있는 글자일 때만 단위로 본다.

    integer_parts=True이면 70.5를 70으로도 읽는다(본문이 소수점까지 쓰고 제목이 반올림한 경우를 근거로 인정).
    """
    found: list[tuple[str, str]] = []
    for match in _QUANTITY.finditer(text):
        digits = match.group(1).replace(",", "")
        unit = match.group(2) if match.group(2) in _UNITS else ""
        found.append((digits, unit))
        if integer_parts and "." in digits:
            found.append((digits.split(".")[0], unit))
    return found


def _title_numbers(title: str) -> list[tuple[str, str]]:
    """제목의 두 자리 이상 숫자와 단위. 한 자리 수('3가지' 등)는 한글 표기와 섞여 오탐이 많아 제외한다."""
    numbers: list[tuple[str, str]] = []
    for digits, unit in _quantities(title):
        if len(digits.replace(".", "")) >= 2 and (digits, unit) not in numbers:
            numbers.append((digits, unit))
    return numbers


def _check_body_length(issues: list[Issue], length: int, profile: StyleProfile) -> None:
    low, high = profile.body_chars
    if length < low * BODY_TOO_SHORT_RATIO:
        issues.append(
            Issue(
                "body.too_short",
                ERROR,
                f"본문이 공백 포함 {length:,}자로 너무 짧다. {low:,}~{high:,}자로 늘린다. 내용을 지어내지 말고 확인된 사실을 구체적으로 설명한다.",
            )
        )
    elif length < low:
        issues.append(Issue("body.short", WARN, f"본문이 {length:,}자다. {low:,}~{high:,}자가 권장된다."))
    elif length > high * 1.5:
        issues.append(Issue("body.long", WARN, f"본문이 {length:,}자로 길다. {low:,}~{high:,}자로 줄인다."))


def _check_structure(issues: list[Issue], body: SanitizedHtml, profile: StyleProfile) -> None:
    if profile.blueprint in ("comparison", "concept_comparison") and body.table_count == 0:
        issues.append(Issue("structure.table_missing", WARN, "비교하는 글인데 표가 없다. 비교 기준을 표(<table>)로 정리한다."))
    if profile.blueprint in ("how_to", "implementation_howto") and body.ol_count == 0:
        issues.append(Issue("structure.steps_missing", WARN, "절차를 번호 목록(<ol>)으로 정리한다."))


def _check_meta(issues: list[Issue], summary: str, tags: list[str], text: str, profile: StyleProfile) -> None:
    low, high = profile.summary_chars
    if not summary:
        issues.append(Issue("summary.missing", WARN, "요약문(<article_summary>)이 없다."))
    elif not low <= len(summary) <= high:
        issues.append(Issue("summary.length", WARN, f"요약문이 {len(summary)}자다. {low}~{high}자로 쓴다."))
    if len(tags) < 3:
        issues.append(Issue("tags.few", WARN, f"태그가 {len(tags)}개다. 검색 키워드 5~8개로 쓴다."))
    if profile.mode == LIFESTYLE and not _DATE_HINT.search(text):
        issues.append(Issue("date.missing", WARN, "시점에 민감한 내용에는 '2026년 10월 기준'처럼 기준 시점을 본문에 밝힌다."))
