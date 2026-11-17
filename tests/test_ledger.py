from __future__ import annotations

from capgate.ledger import Ledger
from capgate.models import EnvManifest, RunResult, TaskSpec

ENV = EnvManifest(id="e1", domain="d", tags=["t"])


def _task(ledger: Ledger, tag: str = "t") -> TaskSpec:
    task = TaskSpec(goal="g", tags=[tag])
    ledger.add_task(task)
    return task


def _run(ledger: Ledger, task: TaskSpec, status: str = "ok", model: str = "m1") -> None:
    run_id = ledger.start_run(task.id, ENV, "scripted", model)
    ledger.finish_run(
        RunResult(
            task_id=task.id,
            env_id=ENV.id,
            run_id=run_id,
            status=status,  # type: ignore[arg-type]
            adapter="scripted",
            model=model,
            latency_ms=12,
        )
    )


def test_perf_counts_wins(tmp_path):
    led = Ledger(tmp_path / "l.db")
    task = _task(led)
    _run(led, task)
    perf = led.perf(env_id="e1")
    assert perf[0]["n"] == 1 and perf[0]["wins"] == 1 and perf[0]["fails"] == 0
    led.close()


def test_human_outcome_overrides_run_status(tmp_path):
    led = Ledger(tmp_path / "l.db")
    task = _task(led)
    _run(led, task, status="ok")
    led.outcome(task.id, success=False, human_edits=3, note="结果不能用")
    perf = led.perf(env_id="e1")
    assert perf[0]["wins"] == 0 and perf[0]["fails"] == 1
    led.close()


def test_latest_outcome_wins_and_does_not_duplicate_rows(tmp_path):
    led = Ledger(tmp_path / "l.db")
    task = _task(led)
    _run(led, task)
    led.outcome(task.id, success=False)
    led.outcome(task.id, success=True)  # 人改主意了
    perf = led.perf(env_id="e1")
    assert perf[0]["n"] == 1, "多次验收不能把 run 重复计数"
    assert perf[0]["wins"] == 1
    led.close()


def test_perf_filters_by_task_type(tmp_path):
    led = Ledger(tmp_path / "l.db")
    _run(led, _task(led, "crawl"))
    _run(led, _task(led, "doc"))
    assert sum(r["n"] for r in led.perf(task_type="crawl")) == 1
    led.close()


def test_chain_has_decisions_and_runs(tmp_path):
    from capgate.models import Candidate, Decision

    led = Ledger(tmp_path / "l.db")
    task = _task(led)
    led.decide(task.id, Decision(stage="domain", strategy="static", chosen=["e1"], reason="t"))
    _run(led, task)
    chain = led.chain(task.id)
    assert chain["task"]["id"] == task.id
    assert chain["decisions"][0]["chosen"] == ["e1"]
    assert len(chain["runs"]) == 1
    led.close()
