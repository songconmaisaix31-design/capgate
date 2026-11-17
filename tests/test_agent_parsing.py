from __future__ import annotations

import pytest

from capgate.agents.cli import _parse as parse_cli
from capgate.agents.openai_compat import OpenAICompatAdapter
from capgate.agents.base import AgentError


def test_openai_parse_plain_json():
    out = OpenAICompatAdapter({})._parse('{"artifacts": {"a": 1}, "summary": "ok"}', [], {})
    assert out.artifacts == {"a": 1} and out.text == "ok"


def test_openai_parse_strips_fence():
    raw = '这是结果：\n```json\n{"artifacts": {"b": [1, 2]}, "summary": "fenced"}\n```'
    out = OpenAICompatAdapter({})._parse(raw, [], {})
    assert out.artifacts == {"b": [1, 2]} and out.text == "fenced"


def test_openai_parse_falls_back_to_text():
    out = OpenAICompatAdapter({})._parse("模型没按格式来", [], {})
    assert out.artifacts == {} and "没按格式" in out.text


def test_openai_parse_rejects_non_object_artifacts():
    with pytest.raises(AgentError):
        OpenAICompatAdapter({})._parse('{"artifacts": [1,2]}', [], {})


def test_cli_parse_tolerates_log_noise():
    raw = "[info] starting\n[info] done\n{\"artifacts\": {\"c\": true}, \"summary\": \"s\"}\n[info] bye"
    assert parse_cli(raw)["artifacts"] == {"c": True}


def test_cli_parse_rejects_non_json():
    with pytest.raises(AgentError):
        parse_cli("完全没有 JSON")
