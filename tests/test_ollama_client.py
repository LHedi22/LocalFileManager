from __future__ import annotations

import json

import httpx
import pytest

from filebot.ollama_client import (
    ModelNotFound,
    OllamaClient,
    OllamaError,
    OllamaTimeout,
    OllamaUnavailable,
    interpret,
)
from filebot.schemas import INTENT_JSON_SCHEMA


def client_with(handler) -> OllamaClient:
    return OllamaClient("http://localhost:11434", "tiny:1b", transport=httpx.MockTransport(handler))


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_connection_refused_is_friendly():
    def handler(request):
        raise httpx.ConnectError("refused")

    c = client_with(handler)
    with pytest.raises(OllamaUnavailable, match="ollama serve"):
        c.check()
    with pytest.raises(OllamaUnavailable):
        c.chat_json([], INTENT_JSON_SCHEMA)


def test_timeout_is_friendly():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(OllamaTimeout):
        client_with(handler).chat_json([], INTENT_JSON_SCHEMA)


def test_missing_model_detected_by_check():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "other:7b"}]})

    with pytest.raises(ModelNotFound, match="ollama pull tiny:1b"):
        client_with(handler).check()


def test_missing_model_on_chat():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(404, json={"error": "model 'tiny:1b' not found"})
        return httpx.Response(404, json={"error": "model 'tiny:1b' not found"})

    with pytest.raises(ModelNotFound):
        client_with(handler).chat_json([], INTENT_JSON_SCHEMA)


def test_server_error():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(OllamaError, match="500"):
        client_with(handler).chat_json([], INTENT_JSON_SCHEMA)


def test_request_uses_structured_output_and_think_flag():
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "thinking"]})
        seen.update(body)
        return chat_reply('{"action":"off_topic","path":"","destination":"","content":"","mode":""}')

    c = client_with(handler)
    out = c.chat_json([{"role": "user", "content": "hi"}], INTENT_JSON_SCHEMA)
    assert json.loads(out)["action"] == "off_topic"
    assert seen["format"] == INTENT_JSON_SCHEMA and seen["stream"] is False
    assert seen["think"] is False and seen["options"]["temperature"] == 0


def test_no_think_flag_for_non_thinking_models():
    seen = {}

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        seen.update(json.loads(request.content))
        return chat_reply("{}")

    client_with(handler).chat_json([], INTENT_JSON_SCHEMA)
    assert "think" not in seen


def test_interpret_retry_then_give_up():
    replies = iter(["oops", '{"action": "x"}'])

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": []})
        return chat_reply(next(replies))

    result = interpret(client_with(handler), "delete a.txt", "")
    assert result.intent is None and len(result.errors) == 2 and len(result.raw_responses) == 2


def test_interpret_success():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": []})
        return chat_reply('{"action":"delete_file","path":"a.txt","destination":"","content":"","mode":""}')

    result = interpret(client_with(handler), "delete a.txt", "")
    assert result.intent.action == "delete_file" and result.intent.path == "a.txt"


def test_unexpected_shape():
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": []})
        return httpx.Response(200, json={"weird": True})

    with pytest.raises(OllamaError):
        client_with(handler).chat_json([], INTENT_JSON_SCHEMA)
