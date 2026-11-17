from __future__ import annotations

import asyncio
import shutil
import socket
import sys
from pathlib import Path

import pytest

from capgate.agents.base import AgentError
from capgate.agents.cli import _resolve_argv, run_command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "win32" or shutil.which("codex.cmd") is None,
        reason="需要 Windows 与已经安装的 Codex npm 启动器",
    ),
]


async def _listening_pid(port: int, command: asyncio.Task[str]) -> int:
    async with asyncio.timeout(18):
        while True:
            if command.done():
                await command
                pytest.fail("真实 Codex 未启动本地监听")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            await writer.wait_closed()
            break

        probe = await asyncio.create_subprocess_exec(
            "netstat.exe", "-ano", "-p", "tcp", stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await probe.communicate()
        assert probe.returncode == 0
        for line in stdout.decode("ascii", errors="ignore").splitlines():
            fields = line.split()
            if len(fields) == 5 and fields[1:3] == [f"127.0.0.1:{port}", "0.0.0.0:0"]:
                return int(fields[-1])
        pytest.fail("没有找到 Codex 监听端口对应的进程")


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
async def test_codex_npm_process_exits_before_return(tmp_path: Path, cancel: bool) -> None:
    import win32api
    import win32con
    import win32event

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    argv = _resolve_argv(
        ["codex.cmd", "app-server", "--listen", f"ws://127.0.0.1:{port}", "-c", "mcp_servers={}"]
    )
    assert argv[0] == "cmd.exe"
    command = asyncio.create_task(run_command(argv, "", 20, tmp_path))
    try:
        pid = await _listening_pid(port, command)
        native = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        try:
            if cancel:
                command.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await command
            else:
                with pytest.raises(AgentError, match="超时"):
                    await command

            assert win32event.WaitForSingleObject(native, 0) == win32event.WAIT_OBJECT_0
            with socket.socket() as connection:
                connection.settimeout(1)
                assert connection.connect_ex(("127.0.0.1", port)) != 0
        finally:
            native.Close()
    finally:
        if not command.done():
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)
