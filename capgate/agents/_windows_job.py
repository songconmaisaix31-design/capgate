from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

_START = b"\x01"
_LAUNCHER = Path(__file__).resolve()


async def run_windows_job(
    argv: list[str], input_bytes: bytes, timeout_s: float, cwd: str | Path | None
) -> tuple[int | None, bytes, bytes]:
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        # Windows 的 venv 重定向器会在加入 Job 前启动解释器，直接使用基础解释器。
        proc = await asyncio.create_subprocess_exec(
            sys._base_executable,
            "-I",
            "-S",
            str(_LAUNCHER),
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid
            )
            try:
                win32job.AssignProcessToJobObject(job, handle)
            finally:
                handle.Close()
        except Exception:
            # 进程尚未收到启动字节，只有启动器需要回收。
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()
            raise

        communication = asyncio.create_task(proc.communicate(_START + input_bytes))
        try:
            async with asyncio.timeout(timeout_s):
                stdout, stderr = await asyncio.shield(communication)
        finally:
            await _wait_for_job_exit(job, communication)
        return proc.returncode, stdout, stderr
    finally:
        job.Close()


async def _wait_for_job_exit(job: Any, communication: asyncio.Task) -> None:
    from .base import AgentError

    async def cleanup() -> None:
        await asyncio.to_thread(_terminate_and_wait, job)
        try:
            async with asyncio.timeout(10):
                await communication
        except TimeoutError as exc:
            raise AgentError("Windows CLI 进程树退出后未能读完输出") from exc

    # 重复取消也要等到退出确认，Job handle 的生命周期覆盖整个清理阶段。
    cleaning = asyncio.create_task(cleanup())
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleaning)
            break
        except asyncio.CancelledError:
            if cleaning.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _terminate_and_wait(job: Any) -> None:
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32job
    import winerror

    from .base import AgentError

    if not win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList):
        return
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    basic = limits["BasicLimitInformation"]
    # 关闭新进程准入，枚举与终止之间不会新增能运行的进程。
    basic["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    basic["ActiveProcessLimit"] = 0
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    pids = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList)
    rights = win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION
    with ExitStack() as opened:
        handles = []
        for pid in pids:
            try:
                handle = win32api.OpenProcess(rights, False, pid)
            except pywintypes.error as exc:
                if exc.winerror == winerror.ERROR_INVALID_PARAMETER:
                    continue
                raise
            opened.callback(handle.Close)
            if win32job.IsProcessInJob(handle, job):
                handles.append(handle)

        win32job.TerminateJobObject(job, 1)
        deadline = time.monotonic() + 10
        # Job 成员列表可能先于进程对象的退出信号清空。
        for handle in handles:
            remaining_ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
            if win32event.WaitForSingleObject(handle, remaining_ms) != win32event.WAIT_OBJECT_0:
                raise AgentError("无法确认 Windows CLI 进程树已经退出")


def _launch() -> int:
    # 先加入 Job 再启动实际命令，避免批处理启动器抢先创建子进程。
    if os.read(0, 1) != _START:
        return 1
    return subprocess.run(sys.argv[1:], stdin=0, stdout=1, stderr=2, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(_launch())
