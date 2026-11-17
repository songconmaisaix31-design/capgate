from __future__ import annotations

import pytest
from pydantic import ValidationError

from capgate.models import (
    ArtifactContract,
    Capability,
    CapabilityVersion,
    EnvManifest,
    resolve_version,
)

VERSIONS = [
    CapabilityVersion(version="0.1.0"),
    CapabilityVersion(version="0.1.5"),
    CapabilityVersion(version="0.2.0"),
    CapabilityVersion(version="1.0.0"),
]


def test_resolve_version_picks_highest_matching():
    assert resolve_version(VERSIONS, ">=0.1,<0.2").version == "0.1.5"
    assert resolve_version(VERSIONS, "*").version == "1.0.0"
    assert resolve_version(VERSIONS, "==0.2.0").version == "0.2.0"


def test_resolve_version_refuses_silent_fallback():
    with pytest.raises(LookupError):
        resolve_version(VERSIONS, ">=9")


def test_capability_pick_matches_pool_semantics():
    cap = Capability(id="c", versions=VERSIONS)
    assert cap.pick("<0.2").version == "0.1.5"


def test_env_requires_at_least_one_tag():
    with pytest.raises(ValidationError):
        EnvManifest(id="x", domain="d", tags=[])


def test_artifact_ref_parsing():
    assert ArtifactContract.parse("crawl_batch") == ("crawl_batch", None)
    assert ArtifactContract.parse("crawl_batch@2") == ("crawl_batch", 2)
