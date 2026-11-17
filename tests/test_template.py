from __future__ import annotations

import pytest

from capgate.agents.base import AgentError, lookup, render


def test_render_single_placeholder_keeps_type():
    ns = {"input": {"url": "u", "n": 3}, "rows": [1, 2, 3]}
    assert render("{{ input.url }}", ns) == "u"
    assert render("{{ input.n }}", ns) == 3
    assert render("{{ rows }}", ns) == [1, 2, 3]


def test_render_inside_text_stringifies():
    ns = {"url": "http://x"}
    assert render("url is {{ url }}", ns) == "url is http://x"


def test_render_deep_structures():
    ns = {"a": {"b": [{"c": "d"}]}}
    assert render({"x": ["{{ a.b.0.c }}"]}, ns) == {"x": ["d"]}


def test_lookup_missing_raises_readable_error():
    with pytest.raises(AgentError) as exc:
        lookup({"a": {}}, "a.b")
    assert "a.b" in str(exc.value)
