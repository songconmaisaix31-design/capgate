from __future__ import annotations

import pytest

from capgate.models import AgentSpec, ArtifactContract, EnvManifest, FailurePolicy, TaskSpec


def _env(
    env_id: str,
    *,
    failure_policy: FailurePolicy = "abort",
    fail: bool = False,
    produces: list[str] | None = None,
    consumes: list[str] | None = None,
    tags: list[str] | None = None,
    text: str = "{{ input.text }}",
) -> EnvManifest:
    outputs = produces if produces is not None else ["report"]
    if fail:
        text = "{{ input.missing_" + env_id + " }}"
    return EnvManifest(
        id=env_id,
        domain="重试验证",
        tags=tags or ["重试"],
        failure_policy=failure_policy,
        artifacts=ArtifactContract(produces=outputs, consumes=consumes or []),
        agent=AgentSpec(
            recipe=[
                {"emit": {"name": name, "body": {"text": text, "env": "{{ env.id }}"}}}
                for name in outputs
            ]
        ),
    )


def _register(system, *envs: EnvManifest) -> None:
    for index, env in enumerate(envs):
        env.prior = float(len(envs) - index)
    system.registry.envs = {env.id: env for env in envs}


def _task(**kwargs) -> TaskSpec:
    return TaskSpec(goal="验证替代候选执行", tags=["重试"], inputs={"text": "能力网关"}, **kwargs)


async def test_retry_alt_success_records_decision_and_preserves_failures(system):
    _register(
        system,
        _env("original", failure_policy="retry-alt", fail=True),
        _env("alternative"),
    )
    result = await system.orchestrator.run(_task())

    assert result.status == "ok", result.error
    assert result.error is None
    assert [(run.env_id, run.status) for run in result.runs] == [
        ("original", "failed"),
        ("alternative", "ok"),
    ]
    assert system.store.load(result.artifacts[0]) == {"text": "能力网关", "env": "alternative"}
    domain = next(decision for decision in result.decisions if decision.stage == "domain")
    retry = next(decision for decision in result.decisions if decision.stage == "retry")
    assert retry.chosen == ["alternative"]
    assert [(c.id, c.score) for c in retry.candidates] == [
        (c.id, c.score) for c in domain.candidates
    ]
    assert "已经尝试过" in retry.candidates[0].detail
    assert "original" in retry.reason and "alternative" in retry.reason
    assert result.runs[0].error in retry.reason
    recorded = next(d for d in system.ledger.decisions(result.task.id) if d["stage"] == "retry")
    assert recorded["candidates"] == [candidate.model_dump() for candidate in retry.candidates]
    assert recorded["chosen"] == retry.chosen
    events = system.ledger.events(result.task.id)
    assert sum(event["kind"] == "decision.retry" for event in events) == 1
    event = next(event for event in events if event["kind"] == "env.retry_alt")
    assert event["payload"] == {
        "failed_env": "original",
        "replacement_env": "alternative",
        "attempt": 1,
    }
    assert system.ledger.runs(result.task.id)[0]["status"] == "failed"
    assert system.ledger.task(result.task.id)["status"] == "ok"
    assert system.ledger.perf(env_id="original")[0]["fails"] == 1
    assert system.ledger.perf(env_id="alternative")[0]["wins"] == 1


async def test_retry_alt_second_attempt_keeps_original_output_contract(system):
    _register(
        system,
        _env("original", failure_policy="retry-alt", fail=True),
        _env("first", fail=True, produces=["report", "extra"]),
        _env("second"),
    )
    result = await system.orchestrator.run(_task())

    assert result.status == "ok", result.error
    assert result.error is None
    assert [run.env_id for run in result.runs] == ["original", "first", "second"]
    retries = [decision for decision in result.decisions if decision.stage == "retry"]
    assert [decision.chosen for decision in retries] == [["first"], ["second"]]
    assert "first" in retries[1].reason
    assert "已经尝试过" in retries[1].candidates[1].detail
    assert system.store.load(result.artifacts[0])["env"] == "second"


@pytest.mark.parametrize("candidate_count", [1, 2, 4])
async def test_retry_alt_exhaustion_degrades_with_all_failure_reasons(system, candidate_count):
    envs = [_env("original", failure_policy="retry-alt", fail=True)]
    envs.extend(_env(f"alternative{index}", fail=index < 3) for index in range(1, candidate_count))
    _register(system, *envs)
    result = await system.orchestrator.run(_task())

    assert result.status == "degraded"
    assert [run.env_id for run in result.runs] == [env.id for env in envs[:3]]
    for run in result.runs:
        assert run.status == "failed"
        assert f"env {run.env_id}：{run.error}" in result.error
    retries = [decision for decision in result.decisions if decision.stage == "retry"]
    assert len(retries) == min(candidate_count, 2)
    assert sum(bool(decision.chosen) for decision in retries) == min(candidate_count - 1, 2)
    if candidate_count < 3:
        assert retries[-1].chosen == []
        assert all("已经尝试过" in candidate.detail for candidate in retries[-1].candidates)
    assert system.ledger.task(result.task.id)["status"] == "degraded"
    assert system.ledger.task(result.task.id)["error"] == result.error


async def test_retry_alt_excludes_incompatible_contracts_and_stays_in_domain_candidates(system):
    _register(
        system,
        _env("original", failure_policy="retry-alt", fail=True, produces=["report", "detail"]),
        _env("partial", produces=["report"]),
        _env("needs_source", consumes=["source"], produces=["report", "detail"]),
        _env("compatible", produces=["report", "detail"]),
        _env("outside_domain", tags=["其他"], produces=["report", "detail"]),
    )
    result = await system.orchestrator.run(_task())

    assert result.status == "ok", result.error
    assert [run.env_id for run in result.runs] == ["original", "compatible"]
    retry = next(decision for decision in result.decisions if decision.stage == "retry")
    candidates = {candidate.id: candidate for candidate in retry.candidates}
    assert "未声明原 env 所需的产物：['detail']" in candidates["partial"].detail
    assert "缺少已产出的输入产物：['source']" in candidates["needs_source"].detail
    assert "outside_domain" not in candidates
    assert {artifact.name for artifact in result.artifacts} == {"report", "detail"}


@pytest.mark.parametrize("allow_parallel", [True, False])
async def test_retry_alt_uses_wave_artifacts_without_repeating_env(system, allow_parallel):
    _register(
        system,
        _env("original", failure_policy="retry-alt", fail=True),
        _env("source", produces=["source"], tags=["上游"]),
        _env("alternative", consumes=["source"], text="{{ artifacts.source.text }}"),
        _env(
            "downstream", consumes=["report"], produces=["summary"], text="{{ artifacts.report }}"
        ),
    )
    task = TaskSpec(
        goal="替代执行后继续接力",
        tags=["重试", "上游"],
        inputs={"text": "能力网关"},
        allow_parallel=allow_parallel,
    )
    result = await system.orchestrator.run(task)

    assert result.status == "ok", result.error
    assert [run.env_id for run in result.runs] == [
        "original",
        "source",
        "alternative",
        "downstream",
    ]
    plan = next(decision for decision in result.decisions if decision.stage == "pipeline")
    assert "alternative" in plan.chosen
    summary = next(artifact for artifact in result.artifacts if artifact.name == "summary")
    assert system.store.load(summary)["text"] == {"text": "能力网关", "env": "alternative"}
    retry = next(decision for decision in result.decisions if decision.stage == "retry")
    assert "已经尝试过" in next(c for c in retry.candidates if c.id == "source").detail


async def test_retry_alt_excludes_already_attempted_parallel_candidate(system):
    _register(
        system,
        _env("original", failure_policy="retry-alt", fail=True),
        _env("parallel", tags=["并行"]),
        _env("alternative"),
    )
    result = await system.orchestrator.run(
        TaskSpec(goal="避免重复执行", tags=["重试", "并行"], inputs={"text": "能力网关"})
    )

    assert result.status == "ok", result.error
    assert [run.env_id for run in result.runs] == ["original", "parallel", "alternative"]
    retry = next(decision for decision in result.decisions if decision.stage == "retry")
    assert retry.chosen == ["alternative"]
    assert "已经尝试过" in next(c for c in retry.candidates if c.id == "parallel").detail


async def test_retry_alt_accepts_degraded_alternative(system):
    alternative = _env("alternative", produces=["report", "extra"])
    alternative.agent.recipe.pop()
    _register(system, _env("original", failure_policy="retry-alt", fail=True), alternative)
    result = await system.orchestrator.run(_task())

    assert result.status == "degraded"
    assert [run.status for run in result.runs] == ["failed", "degraded"]
    assert result.error is None
    assert system.store.load(result.artifacts[0])["env"] == "alternative"


@pytest.mark.parametrize("failure_policy", ["abort", "degrade"])
async def test_other_failure_policies_keep_existing_behavior(system, failure_policy):
    _register(
        system,
        _env("original", failure_policy=failure_policy, fail=True),
        _env("alternative"),
        _env("downstream", consumes=["report"], produces=["summary"]),
    )
    result = await system.orchestrator.run(_task())

    assert result.status == "failed"
    assert result.error == result.runs[0].error
    assert not any(decision.stage == "retry" for decision in result.decisions)
    assert [run.env_id for run in result.runs] == (
        ["original"] if failure_policy == "abort" else ["original", "downstream"]
    )
