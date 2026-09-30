import asyncio

from fastapi import HTTPException

from main import generate_reels_video_fallback


async def _fake_success(_reels, _base_url):
    return {"video_id": "abc", "url": "https://example.com/video.mp4"}


async def _fake_error(_reels, _base_url):
    raise HTTPException(status_code=503, detail="Video generation unavailable")


def test_reels_fallback_reports_unavailable_without_video_provider():
    result = asyncio.run(
        generate_reels_video_fallback({"timeline": []}, "https://example.com")
    )

    assert result["status"] == "video_generation_unavailable"
    assert "OpenAI Videos API" in result["detail"]


def test_reels_fallback_returns_success_for_injected_video_generator():
    result = asyncio.run(
        generate_reels_video_fallback(
            {"timeline": []}, "https://example.com", _fake_success
        )
    )

    assert result["status"] == "success"
    assert result["video"]["video_id"] == "abc"


def test_reels_fallback_returns_failed_when_video_generation_errors():
    result = asyncio.run(
        generate_reels_video_fallback(
            {"timeline": []}, "https://example.com", _fake_error
        )
    )

    assert result["status"] == "video_generation_failed"
    assert "Video generation unavailable" in str(result["detail"])