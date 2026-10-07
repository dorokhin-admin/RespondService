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


def test_text_generation_uses_grok_model_and_xai_key(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "choices": [{"message": {"content": "{\"ok\": true}"}}]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")

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


def test_text_generation_trims_xai_key(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "choices": [{"message": {"content": "{\"ok\": true}"}}]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("XAI_API_KEY", "  test-xai-key\r\n")

    asyncio.run(main.call_ai_llm("grok", "system", "user"))

    request = FakeAsyncClient.requests[0]
    assert request["headers"]["Authorization"].endswith("test-xai-key")


def test_carousel_image_generation_uses_xai_imagine_api(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")

    image = asyncio.run(main.generate_carousel_image("editorial still life"))

    assert image["bytes"] == b"png-bytes"
    request = FakeAsyncClient.requests[0]
    assert request["url"] == "https://api.x.ai/v1/images/generations"
    assert request["headers"]["Authorization"].endswith("test-xai-key")
    assert request["json"]["model"] == "grok-imagine-image-2.0"
    assert request["json"]["aspect_ratio"] == "3:4"
    assert request["json"]["resolution"] == "1k"
    assert request["json"]["response_format"] == "b64_json"


def test_carousel_image_review_uses_xai_vision_api(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_payload = {
        "choices": [{
            "message": {
                "content": "{\"pass\": true}"
            }
        }]
    }
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")

    review = asyncio.run(
        main.review_carousel_image(
            b"image-bytes",
            {"title": "Title", "text": "Text"}
        )
    )

    assert review["review"]["pass"] is True
    request = FakeAsyncClient.requests[0]
    assert request["url"] == "https://api.x.ai/v1/chat/completions"
    assert request["headers"]["Authorization"].endswith("test-xai-key")
    assert request["json"]["model"] == "grok-4.7"


def test_non_xai_keys_do_not_replace_missing_xai_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "legacy-key")

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.generate_carousel_image("editorial still life"))

    assert error.value.status_code == 500
    assert error.value.detail == "XAI_API_KEY не найден"
