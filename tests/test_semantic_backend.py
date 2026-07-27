"""Tests for rce.semantic.backend (S1). Every case monkeypatches
urllib.request.urlopen with a fixture/queue -- no real network call is ever
made, so the suite stays closed and reproducible in CI with no LM Studio
(or any other server) running."""

import json
import logging
import urllib.error

import pytest

from rce.semantic import backend

_SCHEMA = {
    "type": "object",
    "required": ["decision", "confidence"],
    "properties": {
        "decision": {"type": "string", "enum": ["confirm", "reject", "uncertain"]},
        "confidence": {"type": "number"},
    },
}


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _chat_payload(content: dict | str) -> dict:
    text = content if isinstance(content, str) else json.dumps(content)
    return {"choices": [{"message": {"content": text}}]}


def _queue_urlopen(monkeypatch, items: list, requests_log: list | None = None):
    """items: a list of dict payloads or Exception instances, popped in
    order as urlopen is called; every request object is appended to
    requests_log (if given) before being resolved."""
    queue = list(items)

    def _fake(request, timeout=None):
        if requests_log is not None:
            requests_log.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    monkeypatch.setattr(backend.urllib.request, "urlopen", _fake)


def test_env_vars_override_all_defaults(monkeypatch):
    monkeypatch.setenv("RCE_LLM_BASE_URL", "http://example.local:9999/v1/")
    monkeypatch.setenv("RCE_LLM_MODEL", "some-other-model")
    monkeypatch.setenv("RCE_LLM_TIMEOUT", "12.5")
    monkeypatch.setenv("RCE_LLM_API_KEY", "env-key")

    client = backend.LlmBackend()

    assert client.base_url == "http://example.local:9999/v1"  # trailing slash stripped
    assert client.model == "some-other-model"
    assert client.timeout == 12.5
    assert client.api_key == "env-key"


def test_complete_json_uses_constrained_output_when_supported(monkeypatch):
    requests_log: list = []
    _queue_urlopen(monkeypatch, [_chat_payload({"decision": "confirm", "confidence": 0.9})], requests_log)
    client = backend.LlmBackend(api_key=None)

    result = client.complete_json("sys", "user", _SCHEMA)

    assert result == {"decision": "confirm", "confidence": 0.9}
    sent_body = json.loads(requests_log[0].data)
    assert sent_body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": _SCHEMA},
    }


def test_complete_json_falls_back_when_response_format_unsupported(monkeypatch):
    requests_log: list = []
    unsupported = urllib.error.HTTPError("http://x/v1/chat/completions", 400, "Bad Request", hdrs=None, fp=None)
    _queue_urlopen(
        monkeypatch,
        [unsupported, _chat_payload({"decision": "reject", "confidence": 0.2})],
        requests_log,
    )
    client = backend.LlmBackend()

    result = client.complete_json("sys", "user", _SCHEMA)

    assert result == {"decision": "reject", "confidence": 0.2}
    assert len(requests_log) == 2
    second_body = json.loads(requests_log[1].data)
    assert "response_format" not in second_body
    assert "JSON Schema" in second_body["messages"][0]["content"]  # schema hint appended to system prompt


def test_complete_json_retries_once_after_validation_failure_then_succeeds(monkeypatch):
    _queue_urlopen(
        monkeypatch,
        [
            _chat_payload({"decision": "not-a-real-choice", "confidence": 0.5}),  # fails enum
            _chat_payload({"decision": "confirm", "confidence": 0.5}),
        ],
    )
    client = backend.LlmBackend()

    result = client.complete_json("sys", "user", _SCHEMA)

    assert result == {"decision": "confirm", "confidence": 0.5}


def test_complete_json_raises_after_second_validation_failure(monkeypatch):
    _queue_urlopen(
        monkeypatch,
        [
            _chat_payload({"decision": "confirm"}),  # missing required "confidence" both times
            _chat_payload({"decision": "confirm"}),
        ],
    )
    client = backend.LlmBackend()

    with pytest.raises(backend.LlmError, match="failed schema validation twice"):
        client.complete_json("sys", "user", _SCHEMA)


def test_probe_returns_model_ids(monkeypatch):
    _queue_urlopen(monkeypatch, [{"data": [{"id": "model-a"}, {"id": "model-b"}]}])
    client = backend.LlmBackend()

    assert client.probe() == ["model-a", "model-b"]


def test_probe_server_not_started_gives_actionable_error(monkeypatch):
    _queue_urlopen(monkeypatch, [urllib.error.URLError(ConnectionRefusedError("Connection refused"))])
    client = backend.LlmBackend()

    with pytest.raises(backend.LlmError, match="LM Studio") as exc_info:
        client.probe()
    assert "RCE_LLM_BASE_URL" in str(exc_info.value)


def test_request_timeout_raises_clear_llm_error(monkeypatch):
    _queue_urlopen(monkeypatch, [TimeoutError("timed out")])
    client = backend.LlmBackend()

    with pytest.raises(backend.LlmError, match="timed out"):
        client.probe()


@pytest.mark.parametrize(
    "api_key, expect_header",
    [("secret-123", "Bearer secret-123"), (None, None)],
)
def test_authorization_header_reflects_api_key(monkeypatch, api_key, expect_header):
    requests_log: list = []
    _queue_urlopen(monkeypatch, [{"data": []}], requests_log)
    client = backend.LlmBackend(api_key=api_key)

    client.probe()

    assert requests_log[0].get_header("Authorization") == expect_header


# -- Privacy: DESIGN.md section 7 says "local by default", not "local by
# construction" -- RCE_LLM_BASE_URL is user-configurable, so a non-local
# value must warn loudly and every time, never silently ship data offline. --


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:1234/v1",
        "http://my-desktop.local:1234/v1",
        backend.DEFAULT_BASE_URL,
    ],
)
def test_local_base_urls_do_not_warn(base_url, caplog):
    with caplog.at_level(logging.WARNING):
        backend.LlmBackend(base_url=base_url)

    assert caplog.text == ""


def test_remote_base_url_warns_every_construction(caplog):
    with caplog.at_level(logging.WARNING):
        backend.LlmBackend(base_url="http://example.com:8000/v1")
        backend.LlmBackend(base_url="http://example.com:8000/v1")  # second construction, second warning

    assert caplog.text.count("NON-LOCAL") == 2
    assert "example.com" in caplog.text


def test_remote_base_url_from_env_var_also_warns(monkeypatch, caplog):
    monkeypatch.setenv("RCE_LLM_BASE_URL", "https://api.remote-inference.example/v1")

    with caplog.at_level(logging.WARNING):
        backend.LlmBackend()

    assert "NON-LOCAL" in caplog.text
    assert "api.remote-inference.example" in caplog.text
