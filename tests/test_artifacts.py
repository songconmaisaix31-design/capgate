from __future__ import annotations

from pathlib import Path

import pytest

from capgate.artifacts import ArtifactStore, SchemaError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "art", ROOT / "schemas")


def test_roundtrip(store):
    art = store.put("task_1", "crawl_batch", {"url": "u", "links": ["a"], "text": "t"}, env_id="e", run_id="r")
    assert store.load(art)["url"] == "u"
    assert art.sha256 and len(art.sha256) == 64
    assert art.path.endswith(".json")


def test_schema_violation_is_loud(store):
    with pytest.raises(SchemaError):
        store.put("task_1", "crawl_batch", {"url": "u"})


def test_unknown_artifact_needs_object(store):
    with pytest.raises(SchemaError):
        store.put("task_1", "free_form", "just a string")
    assert store.put("task_1", "free_form", {"ok": True}).name == "free_form"


def test_missing_inputs_helper():
    from capgate.models import ArtifactContract

    contract = ArtifactContract(consumes=["crawl_batch", "other@2"])
    assert ArtifactStore.missing_inputs(contract, {"crawl_batch"}) == ["other@2"]
