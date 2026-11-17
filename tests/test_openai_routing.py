from __future__ import annotations

from capgate.models import TaskSpec
from capgate.system import System


def test_model_environment_requires_explicit_routing(system: System) -> None:
    goal = "检索文档并回答问题"
    implicit = system.router.route_domains(TaskSpec(goal=goal))
    assert "doc-qa-model" not in {candidate.id for candidate in implicit.candidates}

    tagged = system.router.route_domains(TaskSpec(goal=goal, tags=["模型知识问答"]))
    assert tagged.chosen == ["doc-qa-model"]
    hinted = system.router.route_domains(TaskSpec(goal=goal, hint_envs=["doc-qa-model"]))
    assert hinted.chosen == ["doc-qa-model"]
