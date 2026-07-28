from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.core.settings import Settings
from app.llm.generator import ArticleGenerator
from app.llm.prompts import build_system_prompt, build_user_prompt


def test_build_prompts():
    sys_prompt = build_system_prompt()
    user_prompt = build_user_prompt("주민등록등본 발급")
    assert "티스토리" in sys_prompt
    assert "HTML <a> 바로가기" in sys_prompt
    assert "각주 번호" in sys_prompt
    assert "주민등록등본 발급" in user_prompt


@patch("requests.post")
def test_generator_cleans_citation_numbers(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "title": "주민등록등본 발급방법[1]",
                            "body_html": "<p>정부24[9]에서 신청하면 됩니다.[10]</p>",
                            "tags": ["정부24", "등본"],
                        }
                    )
                }
            }
        ]
    }
    mock_post.return_value = mock_response

    settings = Settings(perplexity_api_key="pplx-valid-key")
    generator = ArticleGenerator(settings)
    article = generator.generate("주민등록등본 발급")

    assert article.title == "주민등록등본 발급방법"
    assert article.body_html == "<p>정부24에서 신청하면 됩니다.</p>"
    assert "[1]" not in article.title
    assert "[9]" not in article.body_html
    assert "[10]" not in article.body_html
