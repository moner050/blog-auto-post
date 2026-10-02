from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from app.core.settings import Settings
from app.llm.client import (
    MAX_RESULT_ID,
    PerplexityAPIError,
    PerplexityClient,
    build_agent_payload,
    parse_agent_response,
    resolve_agent_model,
)

# 실제 POST /v1/agent 응답(2026-10-01 검증)에서 외부 기사 내용만 중립 값으로 바꾼 픽스처
FIXTURE = Path(__file__).parent / "fixtures" / "perplexity_agent_response.json"
MESSAGES = [
    {"role": "system", "content": "시스템 지시"},
    {"role": "user", "content": "사용자 요청"},
]


def agent_response() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, perplexity_api_key="pplx-valid-key", **overrides)


def urlopen_returning(body: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    context = MagicMock()
    context.__enter__.return_value = response
    return context


def http_error(code: int, headers: dict | None = None, body: bytes = b'{"error": {"message": "boom"}}'):
    return urllib.error.HTTPError("https://api.perplexity.ai/v1/agent", code, "error", headers or {}, io.BytesIO(body))


def test_resolve_agent_model_maps_sonar_names_presets_and_model_ids() -> None:
    assert resolve_agent_model("sonar") == {"preset": "fast"}
    assert resolve_agent_model("sonar-pro") == {"preset": "fast"}
    assert resolve_agent_model("sonar-reasoning-pro") == {"preset": "low"}
    assert resolve_agent_model("sonar-deep-research") == {"preset": "high"}
    assert resolve_agent_model(" Medium ") == {"preset": "medium"}
    assert resolve_agent_model("anthropic/claude-sonnet-5-5") == {"model": "anthropic/claude-sonnet-5-5"}
    assert resolve_agent_model("sonar-next") == {"model": "perplexity/sonar-next"}


def test_build_agent_payload_maps_sonar_style_arguments() -> None:
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}

    payload = build_agent_payload(
        MESSAGES,
        model_name="sonar",
        temperature=0.7,
        max_tokens=2500,
        response_format=schema,
        search_recency_filter="week",
        search_language_filter=["ko"],
        country="KR",
    )

    assert payload["preset"] == "fast"
    assert payload["input"] == "사용자 요청"
    assert payload["instructions"].startswith("시스템 지시")
    assert "Korean-language sources" in payload["instructions"]
    assert payload["language_preference"] == "ko"
    assert payload["max_output_tokens"] == 2500
    assert payload["temperature"] == 0.7
    assert payload["store"] is False
    assert payload["response_format"] == schema
    assert payload["tools"] == [
        {"type": "web_search", "filters": {"search_recency_filter": "week"}, "user_location": {"country": "KR"}}
    ]
    assert "messages" not in payload and "max_tokens" not in payload


@pytest.mark.parametrize(
    ("model_name", "sends_temperature"),
    [("sonar", True), ("fast", True), ("anthropic/claude-sonnet-5-5", True), ("low", False), ("medium", False), ("high", False), ("xhigh", False), ("sonar-pro", True), ("sonar-reasoning-pro", False)],
)
def test_temperature_is_only_sent_where_it_was_observed_to_work(model_name: str, sends_temperature: bool) -> None:
    """실호출: fast는 수락, low는 400. 추론을 켜는 프리셋에는 temperature를 보내지 않는다."""
    payload = build_agent_payload(MESSAGES, model_name=model_name, temperature=0.4, max_tokens=100)

    assert ("temperature" in payload) is sends_temperature


def test_client_retries_without_temperature_when_the_agent_api_rejects_the_request() -> None:
    client = PerplexityClient(make_settings(), sleep=lambda seconds: None)
    rejected = http_error(400, body=b'{"error":{"message":"invalid request","type":"invalid_request","code":400}}')
    with patch("urllib.request.urlopen", side_effect=[rejected, urlopen_returning(agent_response())]) as mock_urlopen:
        completion = client.completion_response(MESSAGES, temperature=0.4, model="anthropic/claude-sonnet-5-5")

    first, second = (json.loads(call.args[0].data.decode("utf-8")) for call in mock_urlopen.call_args_list)
    assert first["temperature"] == 0.4 and "temperature" not in second
    assert second["model"] == "anthropic/claude-sonnet-5-5" and completion.finish_reason == "stop"


def test_client_gives_up_when_a_400_persists_after_dropping_temperature() -> None:
    client = PerplexityClient(make_settings(), sleep=lambda seconds: None)
    with patch("urllib.request.urlopen", side_effect=[http_error(400), http_error(400)]) as mock_urlopen:
        with pytest.raises(PerplexityAPIError, match="코드 400"):
            client.completion_response(MESSAGES, temperature=0.4)

    assert mock_urlopen.call_count == 2


def test_build_agent_payload_without_search_filters_or_system_prompt() -> None:
    history = [
        {"role": "user", "content": "첫 질문"},
        {"role": "assistant", "content": "답변"},
        {"role": "user", "content": "추가 질문"},
    ]

    payload = build_agent_payload(history, model_name="low", temperature=0.2, max_tokens=100, search_enabled=False)

    assert "instructions" not in payload and "tools" not in payload and "language_preference" not in payload
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": "첫 질문"},
        {"type": "message", "role": "assistant", "content": "답변"},
        {"type": "message", "role": "user", "content": "추가 질문"},
    ]
    assert payload["preset"] == "low"


def test_parse_agent_response_reads_real_response_shape() -> None:
    completion = parse_agent_response(agent_response())

    assert json.loads(completion.content)["candidates"][0]["topic"] == "테스트 주제 A"
    assert completion.citations == [f"https://news.example.com/article/{n}" for n in (1, 2, 3)]
    assert [result["id"] for result in completion.search_results] == [1, 2, 3]
    assert completion.finish_reason == "stop"
    assert completion.model == "openai/gpt-6-luna"
    assert completion.usage["cost"]["total_cost"] == 0.00225
    assert completion.usage["total_tokens"] == 7095


def test_parse_agent_response_marks_incomplete_as_length() -> None:
    data = agent_response()
    data["status"] = "incomplete"

    assert parse_agent_response(data).finish_reason == "length"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_parse_agent_response_raises_when_run_failed(status: str) -> None:
    data = agent_response()
    data["status"] = status
    data["error"] = {"message": "tool error"}

    with pytest.raises(PerplexityAPIError, match="Agent API 실행 실패"):
        parse_agent_response(data)


def test_parse_agent_response_requires_message_text() -> None:
    data = agent_response()
    data["output"] = [item for item in data["output"] if item["type"] != "message"]

    with pytest.raises(PerplexityAPIError, match="본문이 없습니다"):
        parse_agent_response(data)


def test_parse_agent_response_uses_last_message_and_keeps_citation_numbers_aligned() -> None:
    data = agent_response()
    message = data["output"][-1]
    interim = json.loads(json.dumps(message))
    interim["content"][0]["text"] = "검색해 볼게요."
    data["output"].insert(0, interim)
    results = data["output"][1]["results"]
    data["output"][1]["results"] = [results[0], results[2]]  # id 2 누락

    completion = parse_agent_response(data)

    assert json.loads(completion.content)["candidates"]
    assert completion.citations == ["https://news.example.com/article/1", "", "https://news.example.com/article/3"]


def test_parse_agent_response_without_ids_keeps_result_order() -> None:
    data = agent_response()
    for result in data["output"][0]["results"]:
        del result["id"]

    assert parse_agent_response(data).citations == [f"https://news.example.com/article/{n}" for n in (1, 2, 3)]


def test_parse_agent_response_ignores_an_empty_trailing_message() -> None:
    data = agent_response()
    empty = json.loads(json.dumps(data["output"][-1]))
    empty["content"][0]["text"] = "  \n"
    data["output"].append(empty)

    assert json.loads(parse_agent_response(data).content)["candidates"]  # 앞의 본문이 빈 message에 덮이지 않는다


def test_parse_agent_response_ignores_search_results_that_are_not_a_list() -> None:
    data = agent_response()
    data["output"][0]["results"] = 5

    completion = parse_agent_response(data)

    assert completion.search_results == [] and completion.citations == []


def test_absurd_result_ids_are_ignored_instead_of_allocating_a_huge_citation_list() -> None:
    data = agent_response()
    data["output"][0]["results"][0]["id"] = 50_000_000
    started = time.perf_counter()

    completion = parse_agent_response(data)

    assert time.perf_counter() - started < 1
    assert completion.citations == ["", "https://news.example.com/article/2", "https://news.example.com/article/3"]
    assert len(completion.search_results) == 3 and MAX_RESULT_ID == 1_000


def test_client_posts_to_agent_endpoint_with_agent_payload() -> None:
    client = PerplexityClient(make_settings())
    with patch("urllib.request.urlopen", return_value=urlopen_returning(agent_response())) as mock_urlopen:
        completion = client.completion_response(
            MESSAGES, temperature=0.5, max_tokens=900, search_recency_filter="week", search_language_filter=["ko"]
        )

    request = mock_urlopen.call_args.args[0]
    sent = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.perplexity.ai/v1/agent"
    assert request.get_header("Authorization") == "Bearer pplx-valid-key"
    assert sent["preset"] == "fast" and sent["max_output_tokens"] == 900 and sent["language_preference"] == "ko"
    assert sent["tools"][0]["filters"] == {"search_recency_filter": "week"}
    assert completion.model == "openai/gpt-6-luna"


def test_client_agent_mode_ignores_disable_search_and_model_override() -> None:
    client = PerplexityClient(make_settings())
    with patch("urllib.request.urlopen", return_value=urlopen_returning(agent_response())) as mock_urlopen:
        client.completion_response(MESSAGES, model="anthropic/claude-sonnet-5-5", disable_search=True)

    sent = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
    assert sent["model"] == "anthropic/claude-sonnet-5-5" and "preset" not in sent
    assert "disable_search" not in sent


def test_client_waits_for_ratelimit_reset_after_429_then_succeeds() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=60, sleep=sleeps.append)
    limited = http_error(429, {"x-ratelimit-reset": str(time.time() + 3)})
    with patch("urllib.request.urlopen", side_effect=[limited, urlopen_returning(agent_response())]) as mock_urlopen:
        completion = client.completion_response(MESSAGES)

    assert mock_urlopen.call_count == 2
    assert len(sleeps) == 1 and 3.0 < sleeps[0] <= 3.6
    assert completion.finish_reason == "stop"


def test_client_never_sleeps_a_negative_time_for_a_reset_that_already_passed() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=60, sleep=sleeps.append)
    already_reset = http_error(429, {"x-ratelimit-reset": str(time.time() - 100)})
    with patch("urllib.request.urlopen", side_effect=[already_reset, urlopen_returning(agent_response())]):
        client.completion_response(MESSAGES)

    assert sleeps == [0.5]


def test_a_ratelimit_reset_that_is_not_an_epoch_time_is_ignored() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=60, sleep=sleeps.append)
    with patch("urllib.request.urlopen", side_effect=[http_error(429, {"x-ratelimit-reset": "30"}), urlopen_returning(agent_response())]):
        client.completion_response(MESSAGES)

    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.5  # 30을 epoch(1970년)로 읽어 0.5초만 쉬면 한도를 바로 다시 두드린다


def test_a_negative_retry_after_is_ignored_in_favour_of_the_backoff() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=60, sleep=sleeps.append)
    with patch("urllib.request.urlopen", side_effect=[http_error(503, {"Retry-After": "-5"}), urlopen_returning(agent_response())]):
        client.completion_response(MESSAGES)

    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.5


def test_client_does_not_send_a_request_when_its_budget_is_already_spent() -> None:
    client = PerplexityClient(make_settings(), timeout_seconds=0.0)
    with patch("urllib.request.urlopen") as mock_urlopen:
        with pytest.raises(PerplexityAPIError, match="대기 시간이 초과"):
            client.completion_response(MESSAGES)

    mock_urlopen.assert_not_called()


def test_client_honors_retry_after_header_with_upper_bound() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=60, sleep=sleeps.append)
    with patch(
        "urllib.request.urlopen",
        side_effect=[http_error(503, {"Retry-After": "999"}), urlopen_returning(agent_response())],
    ):
        client.completion_response(MESSAGES)

    assert sleeps == [20.0]


def test_client_does_not_retry_client_errors() -> None:
    """뺄 파라미터가 없는 요청(추론 프리셋은 temperature를 보내지 않는다)의 400은 그대로 실패한다."""
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), sleep=sleeps.append)
    with patch("urllib.request.urlopen", side_effect=[http_error(400)]) as mock_urlopen:
        with pytest.raises(PerplexityAPIError, match="코드 400") as raised:
            client.completion_response(MESSAGES, model="low")

    assert mock_urlopen.call_count == 1 and sleeps == []
    assert raised.value.status_code == 400 and raised.value.retryable is False


def http_error_with_unreadable_body(code: int) -> urllib.error.HTTPError:
    stream = MagicMock()
    stream.read.side_effect = http.client.IncompleteRead(b"par", 100)  # 오류 응답 본문을 읽다 연결이 끊긴다
    return urllib.error.HTTPError("https://api.perplexity.ai/v1/agent", code, "error", {}, stream)


def test_a_503_whose_body_cannot_be_read_is_still_retried_by_its_status() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), sleep=sleeps.append)
    with patch("urllib.request.urlopen", side_effect=[http_error_with_unreadable_body(503), urlopen_returning(agent_response())]):
        assert client.completion_response(MESSAGES).finish_reason == "stop"

    assert len(sleeps) == 1


def test_a_persistent_503_with_an_unreadable_body_raises_an_api_error_not_a_raw_http_exception() -> None:
    client = PerplexityClient(make_settings(), max_retries=0)
    with patch("urllib.request.urlopen", side_effect=[http_error_with_unreadable_body(503)]):
        with pytest.raises(PerplexityAPIError, match="코드 503") as raised:
            client.completion_response(MESSAGES)

    assert raised.value.status_code == 503 and raised.value.retryable is True


def test_client_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=60, max_retries=2, sleep=sleeps.append)
    with patch("urllib.request.urlopen", side_effect=[http_error(502)] * 3) as mock_urlopen:
        with pytest.raises(PerplexityAPIError, match="코드 502"):
            client.completion_response(MESSAGES)

    assert mock_urlopen.call_count == 3 and len(sleeps) == 2


def test_client_does_not_sleep_past_its_total_budget() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), timeout_seconds=1.0, sleep=sleeps.append)
    with patch("urllib.request.urlopen", side_effect=[http_error(503, {"Retry-After": "10"})]):
        with pytest.raises(PerplexityAPIError, match="코드 503"):
            client.completion_response(MESSAGES)

    assert sleeps == []


def test_client_does_not_retry_read_timeouts() -> None:
    client = PerplexityClient(make_settings(), sleep=lambda seconds: None)
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as mock_urlopen:
        with pytest.raises(PerplexityAPIError, match="대기 시간이 초과"):
            client.completion_response(MESSAGES)

    assert mock_urlopen.call_count == 1


def test_client_retries_connection_errors() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), sleep=sleeps.append)
    with patch(
        "urllib.request.urlopen",
        side_effect=[urllib.error.URLError(ConnectionResetError("reset")), urlopen_returning(agent_response())],
    ):
        assert client.completion_response(MESSAGES).finish_reason == "stop"

    assert len(sleeps) == 1


def broken_body(error: Exception) -> MagicMock:
    response = MagicMock()
    response.read.side_effect = error
    context = MagicMock()
    context.__enter__.return_value = response
    return context


def test_client_retries_connections_dropped_while_reading_the_body() -> None:
    sleeps: list[float] = []
    client = PerplexityClient(make_settings(), sleep=sleeps.append)
    with patch(
        "urllib.request.urlopen",
        side_effect=[broken_body(ConnectionResetError("reset by peer")), urlopen_returning(agent_response())],
    ):
        assert client.completion_response(MESSAGES).finish_reason == "stop"

    assert len(sleeps) == 1


def test_client_wraps_incomplete_reads_as_retryable_api_errors() -> None:
    client = PerplexityClient(make_settings(), max_retries=0)
    with patch("urllib.request.urlopen", side_effect=[broken_body(http.client.IncompleteRead(b"par", 100))]):
        with pytest.raises(PerplexityAPIError, match="IncompleteRead") as raised:
            client.completion_response(MESSAGES)

    assert raised.value.retryable is True


@pytest.mark.parametrize("key", ["", "pplx-your_api_key_here"])
def test_client_rejects_missing_or_placeholder_key_without_calling_api(key: str) -> None:
    client = PerplexityClient(Settings(_env_file=None, perplexity_api_key=key))
    with patch("urllib.request.urlopen") as mock_urlopen:
        with pytest.raises(PerplexityAPIError, match="PERPLEXITY_API_KEY"):
            client.completion_response(MESSAGES)

    mock_urlopen.assert_not_called()


def test_sonar_mode_keeps_legacy_endpoint_and_drops_rejected_disable_search() -> None:
    legacy = {
        "choices": [{"message": {"content": "본문"}, "finish_reason": "length"}],
        "citations": ["https://example.com/a"],
        "search_results": [{"url": "https://example.com/a", "title": "A"}],
        "usage": {"total_tokens": 5},
    }
    client = PerplexityClient(make_settings(perplexity_api_mode="sonar"), sleep=lambda seconds: None)
    with patch("urllib.request.urlopen", side_effect=[http_error(400), urlopen_returning(legacy)]) as mock_urlopen:
        completion = client.completion_response(MESSAGES, disable_search=True, search_language_filter=["ko"])

    first, second = (json.loads(call.args[0].data.decode("utf-8")) for call in mock_urlopen.call_args_list)
    assert mock_urlopen.call_args.args[0].full_url == "https://api.perplexity.ai/v1/sonar"
    assert first["disable_search"] is True and "disable_search" not in second
    assert second["messages"] == MESSAGES and second["search_language_filter"] == ["ko"]
    assert (completion.content, completion.finish_reason, completion.usage) == ("본문", "length", {"total_tokens": 5})


def test_sonar_mode_keeps_citation_positions_when_an_entry_is_not_a_string() -> None:
    legacy = {
        "choices": [{"message": {"content": "본문"}}],
        "citations": ["https://a.example", None, "https://c.example"],
    }
    client = PerplexityClient(make_settings(perplexity_api_mode="sonar"))
    with patch("urllib.request.urlopen", return_value=urlopen_returning(legacy)):
        completion = client.completion_response(MESSAGES)

    assert completion.citations == ["https://a.example", "", "https://c.example"]  # 번호가 한 칸씩 밀리지 않는다


def test_sonar_mode_tolerates_null_citations() -> None:
    legacy = {"choices": [{"message": {"content": "본문"}}], "citations": None, "search_results": None}
    client = PerplexityClient(make_settings(perplexity_api_mode="sonar"))
    with patch("urllib.request.urlopen", return_value=urlopen_returning(legacy)):
        completion = client.completion_response(MESSAGES)

    assert completion.citations == [] and completion.search_results == []
