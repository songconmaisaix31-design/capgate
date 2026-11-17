"""账本：append-only 事件 + 可聚合的运行结果。

为什么先做账本、后做路由算法：路由表必须由数据长出来。没有
``(env, 任务类型, agent/model)`` 的样本和成败标签，任何打分都只是"我觉得"。

表结构：
    tasks      任务（含 task_type，路由的学习单位）
    events     事件流（决策链的事实来源，append-only）
    decisions  每次路由的候选、分数、理由
    runs       一次 env 执行
    artifacts  产物登记（内容在 ArtifactStore 里）
    outcomes   人的验收标签（可后补，覆盖 run 的自动判定）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import Locked, connect
from .models import Artifact, Decision, EnvManifest, RunResult, TaskSpec, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    task_type    TEXT NOT NULL,
    goal         TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    spec         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    error        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    task_id  TEXT NOT NULL,
    env_id   TEXT,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    stage      TEXT NOT NULL,
    strategy   TEXT NOT NULL,
    chosen     TEXT NOT NULL,
    candidates TEXT NOT NULL DEFAULT '[]',
    reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_decisions_task ON decisions(task_id, id);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    env_id      TEXT NOT NULL,
    adapter     TEXT NOT NULL,
    model       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_runs_env ON runs(env_id);
CREATE INDEX IF NOT EXISTS ix_runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    run_id      TEXT,
    env_id      TEXT,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_artifacts_task ON artifacts(task_id, name);

CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    success     INTEGER NOT NULL,
    quality     REAL,
    human_edits INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS ix_outcomes_task ON outcomes(task_id);
"""


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn = Locked(connect(self.path))
        self.init_schema()

    # ------------------------------------------------------------ 基础设施

    def init_schema(self) -> None:
        with self._conn as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 写

    def add_task(self, spec: TaskSpec) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks(id,created_at,task_type,goal,tags,spec,status)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    spec.id,
                    utcnow(),
                    spec.task_type,
                    spec.goal,
                    json.dumps(spec.tags, ensure_ascii=False),
                    spec.model_dump_json(),
                    "running",
                ),
            )
            conn.commit()

    def finish_task(self, task_id: str, status: str, error: str | None = None) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, utcnow(), task_id),
            )
            conn.commit()

    def event(self, task_id: str, kind: str, payload: dict[str, Any] | None = None, env_id: str | None = None) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT INTO events(ts,task_id,env_id,kind,payload) VALUES (?,?,?,?,?)",
                (utcnow(), task_id, env_id, kind, json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
            conn.commit()

    def decide(self, task_id: str, decision: Decision) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT INTO decisions(ts,task_id,stage,strategy,chosen,candidates,reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    utcnow(),
                    task_id,
                    decision.stage,
                    decision.strategy,
                    json.dumps(decision.chosen, ensure_ascii=False),
                    json.dumps([c.model_dump() for c in decision.candidates], ensure_ascii=False, default=str),
                    decision.reason,
                ),
            )
            conn.commit()
        self.event(task_id, f"decision.{decision.stage}", decision.model_dump())

    def start_run(self, task_id: str, env: EnvManifest, adapter: str, model: str) -> str:
        from .models import new_id

        run_id = new_id("run")
        with self._conn as conn:
            conn.execute(
                "INSERT INTO runs(id,task_id,env_id,adapter,model,status,started_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, task_id, env.id, adapter, model, "running", utcnow()),
            )
            conn.commit()
        return run_id

    def finish_run(self, result: RunResult, meta: dict[str, Any] | None = None) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE runs SET status=?, ended_at=?, latency_ms=?, error=?, meta=? WHERE id=?",
                (
                    result.status,
                    utcnow(),
                    result.latency_ms,
                    result.error,
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                    result.run_id,
                ),
            )
            conn.commit()

    def add_artifact(self, task_id: str, artifact: Artifact) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT INTO artifacts(ts,task_id,run_id,env_id,name,version,sha256,path,bytes)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    artifact.created_at,
                    task_id,
                    artifact.producer_run,
                    artifact.producer_env,
                    artifact.name,
                    artifact.version,
                    artifact.sha256,
                    artifact.path,
                    artifact.bytes,
                ),
            )
            conn.commit()

    def outcome(
        self,
        task_id: str,
        success: bool,
        quality: float | None = None,
        human_edits: int = 0,
        note: str | None = None,
    ) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT INTO outcomes(ts,task_id,success,quality,human_edits,note) VALUES (?,?,?,?,?,?)",
                (utcnow(), task_id, 1 if success else 0, quality, human_edits, note),
            )
            conn.commit()

    # ------------------------------------------------------------ 读

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._conn as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with self._conn as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = _loads(d["payload"])
            out.append(d)
        return out

    def decisions(self, task_id: str) -> list[dict[str, Any]]:
        with self._conn as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["chosen"] = _loads(d["chosen"])
            d["candidates"] = _loads(d["candidates"])
            out.append(d)
        return out

    def artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._conn as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def runs(self, task_id: str) -> list[dict[str, Any]]:
        with self._conn as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY started_at", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 聚合（路由打分用）

    def perf(
        self,
        env_id: str | None = None,
        task_type: str | None = None,
        adapter: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """按 (env, adapter, model) 聚合的成败与耗时。

        成败判定：run 成功 **且** 没有人工差评。人工标签优先于自动判定。
        """
        sql = """
        SELECT r.env_id AS env_id, r.adapter AS adapter, r.model AS model,
               COUNT(*) AS n,
               SUM(CASE WHEN r.status='ok' AND COALESCE(o.success,1)=1 THEN 1 ELSE 0 END) AS wins,
               AVG(CASE WHEN r.latency_ms>0 THEN r.latency_ms END) AS avg_ms
        FROM runs r
        JOIN tasks t ON t.id = r.task_id
        LEFT JOIN (
            SELECT o1.task_id AS task_id, o1.success AS success
            FROM outcomes o1
            WHERE o1.id = (SELECT MAX(o2.id) FROM outcomes o2 WHERE o2.task_id = o1.task_id)
        ) o ON o.task_id = r.task_id
        WHERE (?1 IS NULL OR r.env_id = ?1)
          AND (?2 IS NULL OR t.task_type = ?2)
          AND (?3 IS NULL OR r.adapter = ?3)
          AND (?4 IS NULL OR r.model = ?4)
        GROUP BY r.env_id, r.adapter, r.model
        """
        with self._conn as conn:
            rows = conn.execute(sql, (env_id, task_type, adapter, model)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["n"] = int(d["n"] or 0)
            d["wins"] = int(d["wins"] or 0)
            d["fails"] = d["n"] - d["wins"]
            d["rate"] = d["wins"] / d["n"] if d["n"] else 0.0
            d["avg_ms"] = float(d["avg_ms"] or 0.0)
            out.append(d)
        return out

    def stats(self) -> dict[str, Any]:
        with self._conn as conn:
            tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
            ok = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE status IN ('ok','degraded')"
            ).fetchone()["c"]
            runs = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
            bad_runs = conn.execute("SELECT COUNT(*) c FROM runs WHERE status='failed'").fetchone()["c"]
            arts = conn.execute("SELECT COUNT(*) c FROM artifacts").fetchone()["c"]
            # 人工验收只取每个任务的最新一条
            rated = conn.execute(
                "SELECT COUNT(*) c, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) bad FROM ("
                " SELECT o1.task_id, o1.success FROM outcomes o1"
                " WHERE o1.id = (SELECT MAX(o2.id) FROM outcomes o2 WHERE o2.task_id = o1.task_id)"
                ")"
            ).fetchone()
        return {
            "tasks": tasks,
            "tasks_ok": ok,
            "runs": runs,
            "runs_failed": bad_runs,
            "artifacts": arts,
            "success_rate": (ok / tasks) if tasks else 0.0,
            "rated": int(rated["c"] or 0),
            "rated_bad": int(rated["bad"] or 0),
        }

    def chain(self, task_id: str) -> dict[str, Any]:
        """一条人可读的决策链：为什么走这条路，每一步花了多少。"""
        return {
            "task": self.task(task_id),
            "decisions": self.decisions(task_id),
            "runs": self.runs(task_id),
            "artifacts": self.artifacts(task_id),
            "events": self.events(task_id),
        }


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def default_ledger(path: Path | str) -> Ledger:
    return Ledger(path)


__all__ = ["Ledger", "SCHEMA", "default_ledger", "sqlite3"]
