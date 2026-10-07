import asyncio
import base64

import pytest
from fastapi import HTTPException

import main


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeAsyncClient:
    requests = []
    response_payload = {}

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, headers, json):
        self.requests.append({
            "url": url,
            "headers": headers,
            "json": json
        })
        return FakeResponse(self.response_payload)


def test_text_generation_uses_openai_model_and_key(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "choices": [{"message": {"content": "{\"ok\": true}"}}]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("XAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("GROQ_API_KEY", "must-not-be-used")

    result = asyncio.run(main.call_ai_llm("openai", "system", "user"))

    assert result == {"ok": True}
    request = FakeAsyncClient.requests[0]
    assert request["url"] == "https://api.openai.com/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-openai-key"
    assert request["json"]["model"] == "gpt-6-luna"


def test_text_generation_uses_trimmed_xai_key(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "choices": [{"message": {"content": "{\"ok\": true}"}}]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("XAI_API_KEY", "  test-xai-key\r\n")

    result = asyncio.run(main.call_ai_llm("grok", "system", "user"))

    assert result == {
        "content": {"ok": True},
        "model": "grok-4.7",
        "usage": {}
    }
    request = FakeAsyncClient.requests[0]
    assert request["url"] == "https://api.x.ai/v1/chat/completions"
    assert request["headers"]["Authorization"].endswith("test-xai-key")
    assert request["json"]["model"] == "grok-4.7"


def test_carousel_image_generation_uses_openai_image_api(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    image = asyncio.run(main.generate_carousel_image("editorial still life"))

    assert image == b"png-bytes"
    request = FakeAsyncClient.requests[0]
    assert request["url"] == "https://api.openai.com/v1/images/generations"
    assert request["headers"]["Authorization"] == "Bearer test-openai-key"
    assert request["json"]["model"] == "gpt-image-2.5-flare"
    assert request["json"]["size"] == "1024x1280"


def test_legacy_keys_do_not_replace_missing_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "legacy-key")
    monkeypatch.setenv("GROK_API_KEY", "legacy-key")

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.generate_carousel_image("editorial still life"))

    assert error.value.status_code == 500
    assert error.value.detail == "OPENAI_API_KEY не найден"
