"""ArticleGenerator를 실제 PerplexityClient와 urllib 전송 계층까지 연결해 검증한다 (네트워크 호출은 모두 모킹)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.settings import Settings
from app.llm.generator import ArticleGenerator
from tests.article_helpers import DEFAULT_TITLE, OFFICIAL_URL, SEARCH_RESULTS, make_article

FIXTURE = Path(__file__).parent / "fixtures" / "perplexity_agent_response.json"


def agent_response_with(article_text: str) -> dict:
    """실제 /v1/agent 응답 구조(픽스처)에 글 본문과 검색 결과만 바꿔 끼운다."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["output"][0]["results"] = [{**data["output"][0]["results"][0], **result} for result in SEARCH_RESULTS]
    data["output"][1]["content"][0]["text"] = article_text
    return data


def urlopen_returning(body: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    context = MagicMock()
    context.__enter__.return_value = response
    return context


def test_generator_talks_to_the_agent_api_and_builds_the_article(mini_rules_path: Path) -> None:
    settings = Settings(_env_file=None, perplexity_api_key="pplx-valid-key", article_style_rules_path=mini_rules_path)
    body = agent_response_with(make_article())

    with patch("urllib.request.urlopen", return_value=urlopen_returning(body)) as mock_urlopen:
        article = ArticleGenerator(settings).generate("주민등록등본 발급 방법", category="정부지원·민원")

    request = mock_urlopen.call_args.args[0]
    sent = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.perplexity.ai/v1/agent"
    assert sent["preset"] == "fast"  # 기본 모델 이름 'sonar'는 공식 매핑에 따라 fast 프리셋이 된다
    assert sent["max_output_tokens"] == 8000 and sent["temperature"] == 0.4 and sent["store"] is False
    assert sent["tools"] == [{"type": "web_search", "user_location": {"country": "KR"}}]
    assert "미니 페르소나 지시문" in sent["instructions"] and "[출력 형식]" in sent["instructions"]
    assert "[주제] 주민등록등본 발급 방법" in sent["input"]

    assert article.title == DEFAULT_TITLE
    assert article.sources[0] == {"title": "정부24 주민등록등본 발급 안내", "url": OFFICIAL_URL}
    assert "blog.naver.com" not in article.body_html
    assert article.revisions == 0


def test_truncated_agent_response_is_reported_instead_of_published(mini_rules_path: Path) -> None:
    settings = Settings(_env_file=None, perplexity_api_key="pplx-valid-key", article_style_rules_path=mini_rules_path)
    body = agent_response_with("<article_title>제목</article_title>\n<article_body>\n<p>도중에 잘린 본문")
    body["status"] = "incomplete"

    with patch("urllib.request.urlopen", return_value=urlopen_returning(body)):
        try:
            ArticleGenerator(settings).generate("주민등록등본 발급 방법")
        except ValueError as error:
            assert "길이 제한" in str(error)
        else:
            raise AssertionError("잘린 응답이 글로 만들어졌다")
