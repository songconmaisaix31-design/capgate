"""SQLite 连接与 PRAGMA。

本地单用户网关，读写都在一个进程里，因此用同步 sqlite3 + 一把锁：
SQLite 的写操作是微秒级的，阻塞事件循环的代价远小于引入线程池和
双份 API 的复杂度。WAL + busy_timeout 挡短暂写竞争，进程间 ``FileLock``
（基于成熟库 ``filelock``）把"本机单写者"变成显式约束，杜绝多进程并发写时的
WAL-reset 竞态。

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

import filelock as _filelock

WAL_SAFE_SINCE = (3, 51, 3)
_LOCK_TIMEOUT_S = 10.0


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


class FileLock:
    """跨进程文件锁：把"本机单写者"这个前提变成显式约束。

    CLI 与 ``serve`` 同时跑是常见用法，两者都写账本。SQLite WAL 的
    ``busy_timeout`` 挡得住短暂的写竞争，却挡不住 3.7.0–3.51.2 的
    WAL-reset 竞态。用一把进程间文件锁把每次账本访问串行化，就能
    既不发``database is locked``，也不会在 checkpoint 时丢事件。

    实现直接包装成熟库 ``filelock``：它统一了平台差异（Windows ``msvcrt`` /
    Unix ``flock``+``fcntl``），阻塞 ``acquire`` 带真实超时（轮询 ``is_locked``
    而非裸 ``flock`` 无限阻塞），``is_locked`` 精确区分是否被持有。锁在进程退出时
    由操作系统自动释放，不会像目录锁那样留下僵尸锁。
    """

    def __init__(self, path: Path | str, timeout: float = _LOCK_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._lock = _filelock.FileLock(str(self.path), timeout=timeout)

    def acquire(self) -> None:
        self._lock.acquire(timeout=self.timeout, poll_interval=0.02)

    def release(self) -> None:
        self._lock.release()

    @property
    def is_locked(self) -> bool:
        return self._lock.is_locked

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def lock_path_for(db_path: Path | str) -> Path:
    return Path(f"{db_path}.lock")


def concurrent_writer_detected(path: Path | str) -> bool:
    """非阻塞探测：账本写锁此刻是否正被其他进程持有。

    供 ``capgate doctor`` 使用——它自己还没开账本，所以拿到锁就说明没人写，
    拿不到就说明有别的进程正在写。
    """
    lock = FileLock(lock_path_for(path), timeout=0.0)
    try:
        lock.acquire()
    except TimeoutError:
        return True
    lock.release()
    return False


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _pragmas():
        conn.execute(pragma)
    return conn


class Locked:
    """给连接套一层可重入锁，让同步 API 在多线程（FastAPI 线程池）下安全。

    同时持有一把跨进程文件锁：进临界区先拿进程内 RLock，再拿进程间文件锁，
    保证同一时刻只有一个进程在碰账本（本机单写者）。
    """

    def __init__(self, conn: sqlite3.Connection, flock: FileLock | None = None) -> None:
        self.conn = conn
        self._lock = threading.RLock()
        self._flock = flock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        if self._flock is not None:
            self._flock.acquire()
        return self.conn

    def __exit__(self, *exc: object) -> None:
        if self._flock is not None:
            self._flock.release()
        self._lock.release()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
