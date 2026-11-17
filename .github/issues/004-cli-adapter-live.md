---
title: "cli 适配器还没接任何一个真实命令行 Agent"
labels: [enhancement, p2]
---

## 现状

`capgate/agents/cli.py` 实现了"子进程 + stdin 提示词 + stdout JSON"的通用契约，
`_parse` 能容忍前后日志噪声，但这套壳**没有接过任何真实 CLI**（codex / dsh / claude）。

## 期望

- 至少接一个：给出可直接用的 `agent.config.command` 示例并跑通一次任务。
- 处理真实 CLI 的常见差异：进度输出走 stderr、需要 `--json` 开关、需要写文件而非 stdout。

## 验收

- [ ] `envs/` 里有一个使用 `adapter: cli` 的示例 env（可以是 example 分支）
- [ ] 失败时错误信息包含子进程退出码与 stderr 摘要
- [ ] 超时能杀净子进程（现在只 `proc.kill()`，Windows 上要验证进程树）

## 备注

Windows 上 `proc.kill()` 不一定杀掉孙进程。如果目标 CLI 会再起子进程，需要
`CREATE_NEW_PROCESS_GROUP` + `taskkill /T`，或改用 Job Object。
