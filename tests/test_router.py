from __future__ import annotations

import random

from capgate.envs import EnvRegistry
from capgate.ledger import Ledger
from capgate.models import EnvManifest, RunResult, TaskSpec
from capgate.router import Router, RouterConfig


def _registry() -> EnvRegistry:
    reg = EnvRegistry(None)
    reg.envs = {
        "a": EnvManifest(id="a", domain="da", tags=["t"], prior=1.0),
        "b": EnvManifest(id="b", domain="db", tags=["t"], prior=1.0),
    }
    return reg


def _seed(ledger: Ledger, env: EnvManifest, wins: int, fails: int, tag: str = "t") -> None:
    for i in range(wins + fails):
        task = TaskSpec(goal="g", tags=[tag])
        ledger.add_task(task)
        run_id = ledger.start_run(task.id, env, "scripted", "m")
        ledger.finish_run(
            RunResult(
                task_id=task.id,
                env_id=env.id,
                run_id=run_id,
                status="ok" if i < wins else "failed",
                adapter="scripted",
                model="m",
            )
        )


def test_thompson_prefers_the_proven_env(tmp_path):
    led = Ledger(tmp_path / "l.db")
    reg = _registry()
    _seed(led, reg.get("a"), wins=8, fails=0)
    _seed(led, reg.get("b"), wins=0, fails=4)
    router = Router(led, reg, RouterConfig(strategy="thompson"), rng=random.Random(11))

    picks = [router.route_domains(TaskSpec(goal="g", tags=["t"])).chosen[0] for _ in range(200)]
    assert picks.count("a") > 160, "样本证据足够时应该压倒性偏向表现好的 env"
    led.close()


def test_thompson_still_explores(tmp_path):
    led = Ledger(tmp_path / "l.db")
    reg = _registry()
    _seed(led, reg.get("a"), wins=6, fails=1)
    router = Router(led, reg, RouterConfig(strategy="thompson"), rng=random.Random(3))

    picks = [router.route_domains(TaskSpec(goal="g", tags=["t"])).chosen[0] for _ in range(300)]
    assert picks.count("b") > 0, "没有探索就会锁死在第一个还行的组合上"
    led.close()


def test_pin_overrides_everything(tmp_path):
    led = Ledger(tmp_path / "l.db")
    reg = _registry()
    _seed(led, reg.get("a"), wins=20, fails=0)
    router = Router(
        led, reg, RouterConfig(strategy="thompson", pins={"t": {"env": "b"}}), rng=random.Random(1)
    )
    decision = router.route_domains(TaskSpec(goal="g", tags=["t"]))
    assert decision.chosen == ["b"] and decision.strategy == "pin"
    led.close()


def test_no_tag_match_falls_back_and_says_so(tmp_path):
    led = Ledger(tmp_path / "l.db")
    reg = _registry()
    router = Router(led, reg, RouterConfig(strategy="static"), rng=random.Random(1))
    decision = router.route_domains(TaskSpec(goal="g", tags=["nobody-covers-this"]))
    assert decision.chosen
    assert "回退" in decision.reason
    led.close()


def test_second_layer_picks_best_model(tmp_path):
    led = Ledger(tmp_path / "l.db")
    reg = _registry()
    env = EnvManifest(
        id="m", domain="d", tags=["t"], agent={"adapter": "scripted", "models": ["m1", "m2"]}
    )  # type: ignore[arg-type]
    reg.envs["m"] = env

    def seed(model: str, wins: int, fails: int) -> None:
        for i in range(wins + fails):
            task = TaskSpec(goal="g", tags=["t"])
            led.add_task(task)
            run_id = led.start_run(task.id, env, "scripted", model)
            led.finish_run(
                RunResult(
                    task_id=task.id,
                    env_id=env.id,
                    run_id=run_id,
                    status="ok" if i < wins else "failed",
                    adapter="scripted",
                    model=model,
                )
            )

    seed("m1", 6, 0)
    seed("m2", 0, 3)
    router = Router(led, reg, RouterConfig(strategy="thompson"), rng=random.Random(5))
    picks = [router.route_agent(env, "t").chosen[0] for _ in range(120)]
    assert picks.count("m1") > 80
    led.close()
