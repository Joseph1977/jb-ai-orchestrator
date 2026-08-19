# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import base64

from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.multimodal import (
    build_multimodal_user_message,
    detect_media,
    model_supports_multimodal,
)
from app.services.workspace_manager import resolve_within


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_detect_png_and_pdf(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    txt = tmp_path / "c.txt"
    txt.write_text("hello")

    assert detect_media(png, png.read_bytes()[:32]) == ("image", "image/png")
    assert detect_media(pdf, pdf.read_bytes()[:32]) == ("pdf", "application/pdf")
    assert detect_media(txt, txt.read_bytes()[:32]) is None


def test_read_file_returns_multimodal_marker(tmp_path):
    img = tmp_path / "shot.png"
    # Minimal valid-ish PNG header
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    ctx = LocalToolContext(workspace_path=str(tmp_path), in_place=True)
    result = run(local_tool_provider.execute("read_file_local", {"path": "shot.png"}, ctx))
    assert result.get("multimodal") is True
    assert result.get("kind") == "image"
    assert result.get("mimeType") == "image/png"
    assert "content" not in result  # no base64 in tool payload
    assert result.get("absolutePath")


def test_read_file_text_unchanged(tmp_path):
    (tmp_path / "notes.txt").write_text("abc")
    ctx = LocalToolContext(workspace_path=str(tmp_path), in_place=True)
    result = run(local_tool_provider.execute("read_file_local", {"path": "notes.txt"}, ctx))
    assert result.get("content") == "abc"
    assert not result.get("multimodal")


def test_model_supports_multimodal(monkeypatch):
    monkeypatch.setattr("app.config.Config.MULTIMODAL_ENABLED", True)
    monkeypatch.setattr("app.config.Config.MULTIMODAL_MODEL_ALLOWLIST", "")
    monkeypatch.setattr(
        "app.config.Config.MULTIMODAL_MODEL_DENYLIST",
        "gpt-3.5,gpt-3.5-turbo,text-embedding,embedding,whisper,tts,davinci,babbage,curie",
    )
    monkeypatch.setattr(
        "app.config.Config.MULTIMODAL_VISION_MARKERS",
        "gpt-4o,gpt-4.1,gpt-4-turbo,gpt-5,o1,o3,o4,"
        "claude-3,claude-4,claude-sonnet,claude-opus,claude-haiku,"
        "gemini,gemini-1.5,gemini-2,vision",
    )
    assert model_supports_multimodal("gpt-4o") is True
    assert model_supports_multimodal("claude-sonnet-4") is True
    assert model_supports_multimodal("gemini-2.0-flash") is True
    assert model_supports_multimodal("gpt-3.5-turbo") is False


def test_build_multimodal_user_message_image(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.MULTIMODAL_ENABLED", True)
    monkeypatch.setattr("app.config.Config.MULTIMODAL_MAX_BYTES", 1_000_000)
    monkeypatch.setattr("app.config.Config.MULTIMODAL_MODEL_ALLOWLIST", "")
    monkeypatch.setattr("app.config.Config.MULTIMODAL_MODEL_DENYLIST", "")
    monkeypatch.setattr(
        "app.config.Config.MULTIMODAL_VISION_MARKERS",
        "gpt-4o,claude-sonnet,gemini,vision",
    )
    img = tmp_path / "x.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x01\x02\x03"
    img.write_bytes(payload)
    marker = {
        "path": "x.png",
        "kind": "image",
        "mimeType": "image/png",
        "bytes": len(payload),
        "multimodal": True,
        "absolutePath": str(img.resolve()),
        "tooLarge": False,
    }
    msg = build_multimodal_user_message([marker], model="gpt-4o")
    assert msg is not None
    assert msg["role"] == "user"
    types = [p["type"] for p in msg["content"]]
    assert "text" in types
    assert "image_url" in types
    url = next(p["image_url"]["url"] for p in msg["content"] if p["type"] == "image_url")
    assert url.startswith("data:image/png;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw == payload


def test_build_multimodal_skips_non_vision_model(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.MULTIMODAL_ENABLED", True)
    monkeypatch.setattr("app.config.Config.MULTIMODAL_MAX_BYTES", 1_000_000)
    monkeypatch.setattr("app.config.Config.MULTIMODAL_MODEL_ALLOWLIST", "")
    monkeypatch.setattr(
        "app.config.Config.MULTIMODAL_MODEL_DENYLIST",
        "gpt-3.5,gpt-3.5-turbo",
    )
    monkeypatch.setattr(
        "app.config.Config.MULTIMODAL_VISION_MARKERS",
        "gpt-4o,claude-sonnet,gemini,vision",
    )
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00")
    marker = {
        "path": "x.png",
        "kind": "image",
        "mimeType": "image/png",
        "bytes": img.stat().st_size,
        "multimodal": True,
        "absolutePath": str(img.resolve()),
        "tooLarge": False,
    }
    msg = build_multimodal_user_message([marker], model="gpt-3.5-turbo")
    assert msg is not None
    assert all(p["type"] == "text" for p in msg["content"])
    assert "not treated as vision-capable" in msg["content"][0]["text"]


def test_resolve_within_still_works_for_images(tmp_path):
    img = tmp_path / "nested" / "a.png"
    img.parent.mkdir()
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert resolve_within(str(tmp_path), "nested/a.png").endswith("a.png")
