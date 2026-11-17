from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from capgate.mcp.client import _normalize


def _text_result(*texts: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text) for text in texts])


def test_dict_payload_roundtrips():
    assert _normalize(_text_result('{"a": 1}')) == {"a": 1}


def test_plain_text_stays_text():
    assert _normalize(_text_result("hello")) == "hello"


def test_multiple_blocks_merge():
    assert _normalize(_text_result("line1", "line2")) == "line1\nline2"


def test_empty_result():
    assert _normalize(_text_result()) is None
