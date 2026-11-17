from __future__ import annotations

import os
from pathlib import Path

from capgate.agents.cli import _resolve_argv


def test_wraps_cmd_shim_on_windows(tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(os, "name", "nt")

    argv = _resolve_argv([str(shim), "exec", "-"])

    assert argv == ["cmd.exe", "/c", str(shim), "exec", "-"]


def test_keeps_path_on_posix(tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(os, "name", "posix")

    argv = _resolve_argv([str(shim), "exec"])

    assert argv[0] == str(shim)


def test_resolves_bare_command_from_path(tmp_path, monkeypatch):
    fake = tmp_path / "capgate-fake-cli"
    fake.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    argv = _resolve_argv(["capgate-fake-cli", "x"])

    assert Path(argv[0]).name == "capgate-fake-cli"
