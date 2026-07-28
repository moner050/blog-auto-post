from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

import requests

from app.core.settings import Settings
from app.llm.prompts import build_system_prompt, build_user_prompt


@dataclass(frozen=True)
class GeneratedArticle:
    title: str
    body_html: str
    tags: list[str]
    summary: str = ""


class ArticleGenerator:
    """Perplexity LLM API를 호출하여 블로그 포스팅 생성."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, topic: str) -> GeneratedArticle:
        """주제를 입력받아 Perplexity Sonar API로 게시글 생성 및 [1], [9] 참조 번호 정제."""
        if not self.settings.perplexity_api_key or self.settings.perplexity_api_key.startswith("pplx-your"):
            raise ValueError("PERPLEXITY_API_KEY가 올바르게 설정되지 않았습니다.")

        url = f"{self.settings.perplexity_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.perplexity_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.perplexity_model,
            "messages": [
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_user_prompt(topic)},
            ],
            "temperature": 0.2,
        }

        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        data = response.json()
        content_text = data["choices"][0]["message"]["content"]

        parsed = _parse_llm_json_response(content_text)
        
        # [1], [2], [9], [10] 형태의 AI 참조각주 번호 100% 정제 필터링
        clean_body = re.sub(r"\[\d+\]", "", parsed.get("body_html", ""))
        clean_title = re.sub(r"\[\d+\]", "", parsed.get("title", ""))

        return GeneratedArticle(
            title=clean_title.strip(),
            body_html=clean_body.strip(),
            tags=parsed.get("tags", []),
            summary=parsed.get("summary", ""),
        )


def _parse_llm_json_response(raw_text: str) -> dict[str, Any]:
    """LLM 응답 텍스트에서 JSON 추출 및 파싱."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        raise ValueError(f"LLM 응답 파싱 실패: {raw_text[:100]}")
