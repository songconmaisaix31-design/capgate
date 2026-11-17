from __future__ import annotations

from capgate.ledger import Ledger
from capgate.models import EnvManifest, RunResult, TaskSpec

ENV = EnvManifest(id="e1", domain="d", tags=["t"])


def test_stats_separates_execution_from_human_acceptance(tmp_path):
    led = Ledger(tmp_path / "l.db")
    task = TaskSpec(goal="g", tags=["t"])
    led.add_task(task)
    run_id = led.start_run(task.id, ENV, "scripted", "m")
    led.finish_run(
        RunResult(
            task_id=task.id,
            env_id=ENV.id,
            run_id=run_id,
            status="ok",
            adapter="scripted",
            model="m",
        )
    )
    led.finish_task(task.id, "ok")

    stats = led.stats()
    assert stats["tasks_ok"] == 1 and stats["rated"] == 0

    led.outcome(task.id, success=False, note="跑通了但结果不能用")
    stats = led.stats()
    assert stats["tasks_ok"] == 1, "执行状态不该被人工标签篡改"
    assert stats["rated"] == 1 and stats["rated_bad"] == 1
    led.close()
