from __future__ import annotations

from capgate.mcp.client import _normalize


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, *texts: str) -> None:
        self.content = [_Text(t) for t in texts]


def test_dict_payload_roundtrips():
    assert _normalize(_Result('{"a": 1}')) == {"a": 1}


def test_plain_text_stays_text():
    assert _normalize(_Result("hello")) == "hello"


def test_multiple_blocks_merge():
    assert _normalize(_Result("line1", "line2")) == "line1\nline2"


def test_empty_result():
    assert _normalize(_Result()) is None
