"""글 생성 테스트용 입력 생성기. 미니 규칙(tests/conftest.py)의 범위를 충족하는 글을 기본값으로 만든다."""

from __future__ import annotations

from app.llm.client import PerplexityCompletion

# 글 생성 테스트가 사용자의 실제 스타일 규칙(수치가 자주 바뀐다)에 묶이지 않도록 쓰는 최소 규칙
MINI_RULES = """\
modes:
  lifestyle:
    profile:
      target_audience:
        primary: "일반 독자"
        secondary: ["부모님의 생활 문제를 대신 알아보는 자녀"]
        knowledge_level: beginner
    voice:
      base_register: "해요체"
    sentence_style:
      preferred_sentence_length_chars: {min: 10, max: 30}
      paragraph_sentence_count: {min: 1, max: 2}
      first_answer_position_percent_max: 15
      rhetorical_questions_per_article: {min: 0, max: 2}
      exclamatory_sentences_per_article: {min: 0, max: 3}
    tone_patterns:
      opening: ["미니 도입 문장"]
    article_blueprints:
      how_to:
        sections: [reader_problem, numbered_steps, summary]
      comparison:
        sections: [comparison_criteria, comparison_table]
    headings:
      recommended_patterns: ["1. {x}부터 확인하세요"]
      avoid: ["참고"]
    formatting:
      use_bold_for: [button_name]
      table:
        use_when: [compare_two_or_more_options]
        max_columns: 3
    emoji:
      enabled: true
      per_article: {min: 2, max: 4}
      allowed: {tip: "💡"}
    title_rules:
      length_chars: {min: 20, max: 40}
      primary_keyword_position: front_half
      max_strong_marketing_phrase: 1
      patterns: ["{a} 방법"]
    generation_controls:
      recommended_body_length_chars: {min: 1000, max: 2000}
      minimum_h2_sections: 3
      maximum_h2_sections: 5
      include_summary: true
restricted_expressions:
  lifestyle:
    max_total_per_article: 2
    items: ["대박", "무조건"]
llm_instructions:
  lifestyle: "미니 페르소나 지시문"
seo:
  lifestyle_meta_description:
    length_chars: {min: 50, max: 100}
    structure: "문제 + 해결 범위"
quality_checklists:
  common: ["체크 A"]
  lifestyle: ["체크 B"]
"""

SENTENCE = (
    "신청 전에 준비물과 조건을 먼저 확인해 보세요. "
    "2026년 10월 기준 안내이니 공식 화면의 최신 내용을 함께 봐 주세요. "
)
DEFAULT_TITLE = "등본 발급 방법, 정부24에서 5분 만에 끝내는 순서"
DEFAULT_SUMMARY = "정부24에서 주민등록등본을 발급받는 순서와 준비물, 막힐 때 확인할 점까지 한 번에 확인할 수 있게 단계별로 정리했어요."
DEFAULT_TAGS = "정부24, 주민등록등본, 등본발급, 민원24, 발급방법"
OFFICIAL_URL = "https://www.gov.kr/portal/service/serviceInfo/PTR000050"
SEARCH_RESULTS = [
    {"id": 1, "title": "정부24 주민등록등본 발급 안내", "url": OFFICIAL_URL},
    {"id": 2, "title": "누군가의 블로그 후기", "url": "https://blog.naver.com/someone/1"},
    {"id": 3, "title": "연합뉴스 기사", "url": "https://www.yna.co.kr/view/AKR1"},
]


def make_body(
    *, h2: int = 4, paragraphs_per_h2: int = 3, emoji: int = 3, steps: bool = True, extra: str = ""
) -> str:
    icons = ["💡", "✅", "⚠️", "📌", "🔍", "📊"]
    parts = ["<p>결론부터 말씀드리면 정부24에서 바로 발급받을 수 있어요.</p>"]
    for index in range(h2):
        icon = f"{icons[index % len(icons)]} " if index < emoji else ""
        parts.append(f"<h2>{icon}소제목 {index + 1}</h2>")
        parts.extend(f"<p>{SENTENCE * 2}</p>" for _ in range(paragraphs_per_h2))
    if steps:
        parts.append("<ol><li>로그인합니다</li><li>발급을 누릅니다</li></ol>")
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def make_article(
    *,
    title: str = DEFAULT_TITLE,
    summary: str = DEFAULT_SUMMARY,
    tags: str = DEFAULT_TAGS,
    body: str | None = None,
    sources: str = "1, 3",
) -> str:
    body = make_body() if body is None else body
    sources_line = f"<article_sources>{sources}</article_sources>\n" if sources else ""
    return (
        f"<article_title>{title}</article_title>\n"
        f"<article_summary>{summary}</article_summary>\n"
        f"<article_tags>{tags}</article_tags>\n"
        f"{sources_line}"
        f"<article_body>\n{body}\n</article_body>"
    )


class UnexpectedLLMCall(BaseException):
    """준비한 응답보다 많이 호출됐다. 생성기가 `except Exception`으로 삼키지 못하도록 BaseException으로 둔다."""


class FakeClient:
    """PerplexityClient 대역. 응답은 문자열, (문자열, finish_reason) 또는 예외를 순서대로 돌려준다."""

    def __init__(
        self,
        *responses: object,
        search_results: list[dict] | None = None,
        results_by_call: dict[int, list[dict]] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.search_results = SEARCH_RESULTS if search_results is None else search_results
        self.results_by_call = results_by_call or {}  # 호출 순번(0부터)별로 검색 결과를 달리 줄 때

    def completion_response(self, messages, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append({"messages": messages, **kwargs})
        if not self.responses:
            raise UnexpectedLLMCall(f"준비한 응답 {len(self.calls) - 1}개보다 많이 호출됐다")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        content, finish_reason = item if isinstance(item, tuple) else (item, "stop")
        results = self.results_by_call.get(len(self.calls) - 1, self.search_results)
        return PerplexityCompletion(
            content=content,
            citations=[result["url"] for result in results],
            search_results=list(results),
            finish_reason=finish_reason,
            usage={"input_tokens": 100, "output_tokens": 200, "cost": {"total_cost": 0.002}},
            model="test-model",
        )
