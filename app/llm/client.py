from __future__ import annotations

import json
from typing import Any
import urllib.error
import urllib.request

from app.core.settings import Settings


class PerplexityAPIError(Exception):
    """Perplexity API 호출 실패 예외."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PerplexityClient:
    """Perplexity Sonar REST API 연동 클라이언트."""

    def __init__(self, settings: Settings, timeout_seconds: float = 60.0) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """Sonar 모델에 completion 요청을 보내고 응답 텍스트를 반환."""
        if not self.settings.perplexity_api_key:
            raise PerplexityAPIError("PERPLEXITY_API_KEY가 설정되지 않았습니다.")

        url = f"{self.settings.perplexity_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.settings.perplexity_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.settings.perplexity_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                parsed = json.loads(response_body)
                return self._extract_content(parsed)
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8", errors="ignore")
            raise PerplexityAPIError(
                f"API HTTP 에러 발생 (코드 {error.code}): {error_body}",
                status_code=error.code,
            ) from error
        except urllib.error.URLError as error:
            raise PerplexityAPIError(f"API 네트워크 연결 실패: {error.reason}") from error
        except (json.JSONDecodeError, KeyError, IndexError) as error:
            raise PerplexityAPIError(f"API 응답 파싱 실패: {error}") from error

    @staticmethod
    def _extract_content(response_data: dict[str, Any]) -> str:
        """API 응답 JSON 데이터에서 텍스트 추출."""
        try:
            return response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise PerplexityAPIError(f"잘못된 API 응답 포맷: {response_data}") from error
