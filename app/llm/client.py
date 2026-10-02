from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import random
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from app.core.settings import Settings

# 일시적 장애로 보고 재시도하는 HTTP 상태 코드 (429: 요청 한도, 5xx: 서버 오류)
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 20.0
MAX_RESULT_ID = 1_000
ERROR_BODY_LIMIT = 500
PLACEHOLDER_KEY_PREFIX = "pplx-your"

# Perplexity 공식 마이그레이션 가이드의 Sonar 모델 → Agent API 프리셋 매핑
SONAR_MODEL_TO_PRESET = {
    "sonar": "fast",
    "sonar-pro": "fast",
    "sonar-reasoning": "low",
    "sonar-reasoning-pro": "low",
    "sonar-deep-research": "high",
}
AGENT_PRESETS = frozenset({"fast", "low", "medium", "high", "xhigh"})
# Agent API에는 search_language_filter가 없어 지시문으로 한국어 자료 우선을 요청한다.
KOREAN_SOURCE_HINT = "When you search, prefer Korean-language sources and the official pages of Korean institutions."


class PerplexityAPIError(Exception):
    """Perplexity API 호출 실패 예외."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class PerplexityCompletion:
    content: str
    citations: list[str]
    search_results: list[dict[str, Any]]
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None


def resolve_agent_model(name: str) -> dict[str, str]:
    """설정의 모델 이름을 Agent API의 preset 또는 model 필드로 바꾼다.

    Sonar 모델명과 프리셋 이름은 preset으로, "provider/model" 형식은 model로 보낸다.
    """
    key = name.strip()
    lowered = key.lower()
    if lowered in SONAR_MODEL_TO_PRESET:
        return {"preset": SONAR_MODEL_TO_PRESET[lowered]}
    if lowered in AGENT_PRESETS:
        return {"preset": lowered}
    if "/" in key:
        return {"model": key}
    return {"model": f"perplexity/{key}"}


def build_agent_payload(
    messages: list[dict[str, str]],
    *,
    model_name: str,
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None = None,
    search_recency_filter: str | None = None,
    search_language_filter: list[str] | None = None,
    search_enabled: bool = True,
    country: str = "",
) -> dict[str, Any]:
    """Sonar 스타일 인자(messages 등)를 POST /v1/agent 요청 본문으로 변환한다."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    turns = [m for m in messages if m.get("role") != "system"]
    language = search_language_filter[0] if search_language_filter else None
    if language == "ko":
        system_parts.append(KOREAN_SOURCE_HINT)

    resolved = resolve_agent_model(model_name)
    payload: dict[str, Any] = {**resolved, "max_output_tokens": max_tokens, "store": False}
    # 실호출(2026-10-01): fast 프리셋(추론 없음)은 temperature를 받지만 low는 400(invalid request)이었다.
    # 추론을 켜는 프리셋은 샘플링 파라미터를 거부하는 것으로 보고 처음부터 보내지 않는다(원인은 미확정).
    if resolved.get("preset") in (None, "fast"):
        payload["temperature"] = temperature
    if system_parts:
        payload["instructions"] = "\n\n".join(system_parts)
    if len(turns) == 1 and turns[0].get("role") == "user":
        payload["input"] = turns[0]["content"]
    else:
        payload["input"] = [{"type": "message", "role": t["role"], "content": t["content"]} for t in turns]
    if language:
        payload["language_preference"] = language
    if response_format is not None:
        payload["response_format"] = response_format
    if search_enabled:
        tool: dict[str, Any] = {"type": "web_search"}
        if search_recency_filter:
            tool["filters"] = {"search_recency_filter": search_recency_filter}
        if country:
            tool["user_location"] = {"country": country}
        payload["tools"] = [tool]
    return payload


def parse_agent_response(data: Any) -> PerplexityCompletion:
    """POST /v1/agent 응답을 PerplexityCompletion으로 변환한다."""
    if not isinstance(data, dict):
        raise PerplexityAPIError(f"잘못된 API 응답 포맷: {str(data)[:ERROR_BODY_LIMIT]}")
    status = data.get("status")
    if status in ("failed", "cancelled") or data.get("error"):
        raise PerplexityAPIError(f"Agent API 실행 실패 (status={status}): {str(data.get('error'))[:ERROR_BODY_LIMIT]}")

    output = data.get("output")
    output = output if isinstance(output, list) else []
    last_message_text: str | None = None
    results: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            parts = item.get("content")
            texts = [
                part["text"]
                for part in (parts if isinstance(parts, list) else [])
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
            ]
            joined = "".join(texts)
            if joined.strip():  # 뒤따르는 빈 message가 앞의 본문을 덮어쓰지 않게 한다
                last_message_text = joined
        elif item.get("type") == "search_results":
            found = item.get("results")
            results.extend(r for r in (found if isinstance(found, list) else []) if isinstance(r, dict))

    content = last_message_text
    if content is None and isinstance(data.get("output_text"), str):
        content = data["output_text"]
    if content is None:
        raise PerplexityAPIError("API 응답에 본문이 없습니다.")

    usage = data.get("usage")
    return PerplexityCompletion(
        content=content,
        citations=_citations_by_id(results),
        search_results=sorted(results, key=_result_sort_key),
        finish_reason="length" if status == "incomplete" else "stop",
        usage=usage if isinstance(usage, dict) else {},
        model=data.get("model") if isinstance(data.get("model"), str) else None,
    )


def _result_id(result: dict[str, Any]) -> int | None:
    # 번호 목록은 가장 큰 번호만큼 만들어지므로, 비정상적으로 큰 번호가 메모리를 쓰지 못하게 상한을 둔다.
    value = result.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_RESULT_ID else None


def _result_sort_key(result: dict[str, Any]) -> tuple[int, int]:
    rid = _result_id(result)
    return (0, rid) if rid is not None else (1, 0)


def _citations_by_id(results: list[dict[str, Any]]) -> list[str]:
    """본문의 [n] 번호가 results[].id와 같으므로 citations[n-1]이 그 URL이 되도록 나열한다.

    비어 있는 번호는 빈 문자열로 두어, 그 번호를 가리키는 후보가 조용히 엉뚱한 URL에 매핑되지 않게 한다.
    """
    by_id: dict[int, str] = {}
    for result in results:
        rid, url = _result_id(result), result.get("url")
        if rid is not None and isinstance(url, str) and rid not in by_id:
            by_id[rid] = url
    if not by_id:
        return [r["url"] for r in results if isinstance(r.get("url"), str)]
    return [by_id.get(number, "") for number in range(1, max(by_id) + 1)]


class PerplexityClient:
    """Perplexity REST API 연동 클라이언트 (기본: Agent API, 설정으로 레거시 Sonar 선택 가능).

    timeout_seconds는 호출 1회의 대기 예산이다. 재시도와 재시도 사이의 대기는 이 예산 안에서만 일어나고,
    남은 예산이 각 요청의 소켓 타임아웃이 된다. 다만 소켓 타임아웃은 연결·수신 한 번의 대기 시간이라,
    응답을 아주 조금씩 흘려보내는 서버는 총 시간을 넘길 수 있다(엄격한 총 시간 제한이 아니다).
    """

    def __init__(
        self,
        settings: Settings,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep

    def completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> str:
        """모델에 completion 요청을 보내고 응답 텍스트를 반환."""
        return self.completion_response(messages, temperature, max_tokens).content

    def completion_response(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4000,
        response_format: dict[str, Any] | None = None,
        search_recency_filter: str | None = None,
        search_language_filter: list[str] | None = None,
        *,
        model: str | None = None,
        disable_search: bool = False,
    ) -> PerplexityCompletion:
        """본문과 검색 근거 메타데이터를 함께 반환.

        disable_search는 레거시 Sonar 모드에서만 의미가 있다(서버가 거부하면 옵션 없이 한 번 더 요청).
        Agent API는 프리셋의 도구를 끌 수 없으므로 이 값을 무시한다. Agent API에서 400이 오면
        temperature를 빼고 한 번 더 요청한다.
        """
        api_key = self.settings.perplexity_api_key
        if not api_key or api_key.startswith(PLACEHOLDER_KEY_PREFIX):
            raise PerplexityAPIError("PERPLEXITY_API_KEY가 설정되지 않았습니다.")

        base_url = self.settings.perplexity_base_url.rstrip("/")
        model_name = model or self.settings.perplexity_model
        if self.settings.perplexity_api_mode == "sonar":
            url = f"{base_url}/v1/sonar"
            payload = self._sonar_payload(
                messages,
                model_name,
                temperature,
                max_tokens,
                response_format,
                search_recency_filter,
                search_language_filter,
                disable_search,
            )
            parse: Callable[[Any], PerplexityCompletion] = self._extract_completion
        else:
            url = f"{base_url}/v1/agent"
            payload = build_agent_payload(
                messages,
                model_name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                search_recency_filter=search_recency_filter,
                search_language_filter=search_language_filter,
                country=self.settings.perplexity_country.strip(),
            )
            parse = parse_agent_response

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        deadline = time.monotonic() + self.timeout_seconds
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PerplexityAPIError("API 응답 대기 시간이 초과되었습니다.")
            try:
                return parse(self._send(url, payload, headers, remaining))
            except PerplexityAPIError as error:
                if error.status_code in (400, 422):
                    if "disable_search" in payload:
                        del payload["disable_search"]
                        continue
                    if "temperature" in payload and self.settings.perplexity_api_mode != "sonar":
                        del payload["temperature"]  # 모델·프리셋에 따라 거부될 수 있어 빼고 한 번 더 시도(검증 오류는 과금되지 않는다)
                        continue
                if not error.retryable or attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(attempt, error.retry_after)
                if delay >= deadline - time.monotonic():
                    raise
                self._sleep(delay)
                attempt += 1

    @staticmethod
    def _sonar_payload(
        messages: list[dict[str, str]],
        model_name: str,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None,
        search_recency_filter: str | None,
        search_language_filter: list[str] | None,
        disable_search: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if search_recency_filter is not None:
            payload["search_recency_filter"] = search_recency_filter
        if search_language_filter is not None:
            payload["search_language_filter"] = search_language_filter
        if disable_search:
            payload["disable_search"] = True
        return payload

    def _send(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> Any:
        """요청을 보내고 파싱된 JSON 응답을 반환한다."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                error_body = error.read().decode("utf-8", errors="ignore")
            except (OSError, http.client.HTTPException):  # 오류 응답 본문을 읽다 연결이 끊긴 경우에도 재시도 판단은 상태 코드로 한다
                error_body = ""
            raise PerplexityAPIError(
                f"API HTTP 에러 발생 (코드 {error.code}): {error_body[:ERROR_BODY_LIMIT]}",
                status_code=error.code,
                retryable=error.code in RETRYABLE_STATUS_CODES,
                retry_after=_retry_after_seconds(error.headers, error.code),
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise PerplexityAPIError("API 응답 대기 시간이 초과되었습니다.") from error
            raise PerplexityAPIError(f"API 네트워크 연결 실패: {error.reason}", retryable=True) from error
        except TimeoutError as error:
            raise PerplexityAPIError("API 응답 대기 시간이 초과되었습니다.") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise PerplexityAPIError(f"API 응답 파싱 실패: {error}") from error
        except (OSError, http.client.HTTPException) as error:
            # 응답을 읽는 도중 연결이 끊긴 경우(ConnectionResetError, IncompleteRead 등)
            raise PerplexityAPIError(f"API 네트워크 오류: {type(error).__name__}: {error}", retryable=True) from error

    @staticmethod
    def _retry_delay(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_AFTER_SECONDS)
        return min(2.0 * (2**attempt), 15.0) + random.uniform(0.0, 0.5)

    @staticmethod
    def _extract_content(response_data: dict[str, Any]) -> str:
        """레거시 Sonar 응답 JSON에서 텍스트 추출."""
        try:
            content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise PerplexityAPIError(f"잘못된 API 응답 포맷: {response_data}") from error
        if not isinstance(content, str):
            raise PerplexityAPIError("API 응답에 본문이 없습니다.")
        return content

    @classmethod
    def _extract_completion(cls, response_data: Any) -> PerplexityCompletion:
        """레거시 Sonar 응답을 PerplexityCompletion으로 변환."""
        if not isinstance(response_data, dict):
            raise PerplexityAPIError(f"잘못된 API 응답 포맷: {str(response_data)[:ERROR_BODY_LIMIT]}")
        # 검색을 끄거나 결과가 없으면 null로 오는 경우가 있어 빈 목록으로 취급한다.
        citations = response_data.get("citations") or []
        search_results = response_data.get("search_results") or []
        if not isinstance(citations, list) or not isinstance(search_results, list):
            raise PerplexityAPIError(f"잘못된 검색 근거 응답 포맷: {response_data}")
        usage = response_data.get("usage")
        return PerplexityCompletion(
            content=cls._extract_content(response_data),
            # 문자열이 아닌 항목을 지우면 뒤 번호가 한 칸씩 밀려 엉뚱한 출처가 되므로 빈 문자열로 자리를 지킨다.
            citations=[citation if isinstance(citation, str) else "" for citation in citations],
            search_results=[result for result in search_results if isinstance(result, dict)],
            finish_reason=_first_choice_finish_reason(response_data),
            usage=usage if isinstance(usage, dict) else {},
            model=response_data.get("model") if isinstance(response_data.get("model"), str) else None,
        )


def _first_choice_finish_reason(response_data: dict[str, Any]) -> str | None:
    try:
        reason = response_data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return reason if isinstance(reason, str) else None


def _retry_after_seconds(headers: Any, status_code: int) -> float | None:
    """재시도까지 기다릴 초를 헤더에서 구한다.

    Retry-After(초 단위만 해석, 날짜 형식은 무시)를 우선하고, 429에서는 Perplexity가 주는
    x-ratelimit-reset(epoch 초)까지 남은 시간을 쓴다.
    """
    if not headers:
        return None
    seconds = _parse_seconds(headers.get("Retry-After"))
    if seconds is not None:
        return seconds
    if status_code == 429:
        reset_at = _parse_seconds(headers.get("x-ratelimit-reset"))
        if reset_at is not None and reset_at > 1e9:
            return max(0.0, reset_at - time.time()) + 0.5
    return None


def _parse_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
