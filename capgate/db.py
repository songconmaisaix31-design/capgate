"""SQLite 连接与 PRAGMA。

本地单用户网关，读写都在一个进程里，因此用同步 sqlite3 + 一把锁：
SQLite 的写操作是微秒级的，阻塞事件循环的代价远小于引入线程池和
双份 API 的复杂度。WAL + busy_timeout 保证 CLI 与 HTTP 服务同时跑不打架。

⚠️ 已知风险：SQLite 3.7.0–3.51.2 存在 WAL-reset 竞态（修复于 3.51.3），
触发条件是 WAL + 多连接并发写/checkpoint。本机若是旧版本，可用
``CAPGATE_SQLITE_JOURNAL=delete`` 退回 DELETE 模式（写更慢、但绕开该 bug）。
``capgate doctor`` 会把这个判断显式打出来。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

WAL_SAFE_SINCE = (3, 51, 3)


def _journal_mode() -> str:
    return os.environ.get("CAPGATE_SQLITE_JOURNAL", "WAL").upper()


def _pragmas() -> tuple[str, ...]:
    return (
        f"PRAGMA journal_mode={_journal_mode()}",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA busy_timeout=5000",
        "PRAGMA foreign_keys=ON",
        "PRAGMA wal_autocheckpoint=1000",
    )


def wal_is_unsafe() -> bool:
    """本机 SQLite 是否落在 WAL-reset 竞态的影响区间。"""
    version = tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:3])
    return version < WAL_SAFE_SINCE


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=False, timeout=5.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    for pragma in _pragmas():
        conn.execute(pragma)
    return conn


class Locked:
    """给连接套一层可重入锁，让同步 API 在多线程（FastAPI 线程池）下安全。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self.conn

    def __exit__(self, *exc: object) -> None:
        self._lock.release()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
