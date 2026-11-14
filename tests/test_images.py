"""Tests for concept-image generation (OpenAI-compatible images API)."""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from print3d.errors import AdapterUnavailable, InvalidArgument, NotFound
from print3d.images import (
    GeneratedImage,
    ImageGenerator,
    ReferenceStore,
    generator_from_config,
    sniff_image_mime,
    validate_reference_id,
)


def png_bytes(size: tuple[int, int] = (8, 8), color: tuple[int, int, int] = (200, 80, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def generator() -> ImageGenerator:
    return ImageGenerator("sk-test", base_url="https://api.openai.com/v1")


@respx.mock
def test_generate_decodes_base64_image(generator: ImageGenerator) -> None:
    png = png_bytes()
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(png).decode()}], "model": "gpt-image-1"},
        )
    )

    image = generator.generate("a bracket with two holes")

    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["prompt"] == "a bracket with two holes"
    assert body["response_format"] == "b64_json"
    assert body["n"] == 1
    assert body["size"] == "1024x1024"
    assert image.data == png
    assert image.mime == "image/png"
    assert image.provider == "openai"
    assert len(image.sha256) == 64


@respx.mock
def test_generate_downloads_url_without_the_api_key(generator: ImageGenerator) -> None:
    png = png_bytes()
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200, json={"data": [{"url": "https://cdn.example.com/out.png"}]}
        )
    )
    download = respx.get("https://cdn.example.com/out.png").mock(
        return_value=httpx.Response(200, content=png)
    )

    image = generator.generate("a gear")

    assert image.data == png
    assert "authorization" not in download.calls[0].request.headers
    assert "authorization" in respx.calls[0].request.headers


@respx.mock
def test_generate_surfaces_a_rejected_key(generator: ImageGenerator) -> None:
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(AdapterUnavailable) as caught:
        generator.generate("a part")
    assert "rejected the API key" in str(caught.value)


@respx.mock
def test_generate_surfaces_rate_limits(generator: ImageGenerator) -> None:
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(429, json={"error": "slow down"})
    )
    with pytest.raises(AdapterUnavailable) as caught:
        generator.generate("a part")
    assert "rate limit" in str(caught.value)


@respx.mock
def test_generate_rejects_non_json(generator: ImageGenerator) -> None:
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(200, text="<html>nope</html>")
    )
    with pytest.raises(AdapterUnavailable) as caught:
        generator.generate("a part")
    assert "non-JSON" in str(caught.value)


@respx.mock
def test_generate_reports_an_empty_image_list(generator: ImageGenerator) -> None:
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    with pytest.raises(AdapterUnavailable) as caught:
        generator.generate("a part")
    assert "no images" in str(caught.value)


@respx.mock
def test_generate_rejects_data_that_is_not_an_image(generator: ImageGenerator) -> None:
    payload = base64.b64encode(b"definitely not a png").decode()
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"data": [{"b64_json": payload}]})
    )
    with pytest.raises(AdapterUnavailable) as caught:
        generator.generate("a part")
    assert "not PNG/JPEG/WebP" in str(caught.value)


@respx.mock
def test_generate_maps_timeouts(generator: ImageGenerator) -> None:
    respx.post("https://api.openai.com/v1/images/generations").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    with pytest.raises(AdapterUnavailable) as caught:
        generator.generate("a part")
    assert "timed out" in str(caught.value)


def test_generate_requires_a_key() -> None:
    with pytest.raises(AdapterUnavailable) as caught:
        ImageGenerator(None)
    assert "No API key" in str(caught.value)


def test_generate_rejects_an_empty_prompt(generator: ImageGenerator) -> None:
    with pytest.raises(InvalidArgument):
        generator.generate("   ")


def test_generator_from_config_maps_settings() -> None:
    generator = generator_from_config(
        {
            "base_url": "https://images.example.com/v1/",
            "model": "flux-pro",
            "size": "1536x1024",
            "timeout_s": 30,
        },
        api_key="k",
        provider_id="custom",
    )
    assert generator.base_url == "https://images.example.com/v1"
    assert generator.model == "flux-pro"
    assert generator.size == "1536x1024"
    assert generator.timeout_s == 30
    assert generator.provider_id == "custom"


def test_sniff_image_mime() -> None:
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 8) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0" + b"x" * 8) == "image/jpeg"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    assert sniff_image_mime(b"not an image") is None


def test_reference_store_round_trip(workspace) -> None:
    store = ReferenceStore(workspace)
    image = GeneratedImage(
        data=png_bytes(),
        provider="openai",
        model="gpt-image-1",
        size="1024x1024",
        prompt="a cube",
    )

    meta = store.create(image, prompt="a cube", provider="openai", model="gpt-image-1")

    assert meta["reference_id"].startswith("r_")
    assert meta["file"] == "image.png"
    assert meta["bytes"] == len(image.data)
    assert meta["sha256"] == image.sha256
    directory = store.directory(meta["reference_id"])
    assert (directory / "image.png").read_bytes() == image.data
    assert json.loads((directory / "meta.json").read_text())["prompt"] == "a cube"

    loaded = store.load(meta["reference_id"])
    assert loaded["reference_id"] == meta["reference_id"]
    assert store.image_path(meta["reference_id"]) == directory / "image.png"


def test_reference_store_rejects_bad_ids_and_misses(workspace) -> None:
    store = ReferenceStore(workspace)
    with pytest.raises(InvalidArgument):
        validate_reference_id("not-a-reference")
    with pytest.raises(NotFound):
        store.load("r_20260101T000000Z_abcdef")
