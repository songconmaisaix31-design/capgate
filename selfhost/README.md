# selfhost · 用 capgate 构建 capgate

这是一个独立的 capgate 工作区，只做一件事：**把本仓库自己的质量门，跑成一条正常的网关任务**。

This is a standalone capgate workspace whose only job is to run this repository's own
quality gates as ordinary gateway tasks.

```
selfhost/
  capabilities/mcp-repo.yaml   仓库操作 MCP（lint / 测试 / issue / 状态）
  envs/repo-lint.yaml          静态检查 env
  envs/repo-test.yaml          回归测试 env
  envs/issue-triage.yaml       开盘梳理 env
  tasks/T0*.md                 任务卡（front-matter: id / type / tags / goal）
  mcp_repo/__main__.py         那个 MCP 的实现
  loop.py                      驱动：读任务卡 → 交给网关 → 打印决策链
  var/                         自己的账本与产物（gitignored）
```

## 跑起来

```bash
python -m selfhost.loop --list        # 看有哪些任务
python -m selfhost.loop --only T03    # 只跑一条（开盘梳理，约 0.2s）
python -m selfhost.loop               # 全跑（约 20s，含一次完整测试）
```

退出码：全绿才是 0。可以直接当 CI 门禁。

## 它证明了什么

一次 `python -m selfhost.loop` 走的是**和线上任务完全相同**的路径：

1. 任务卡 → `TaskSpec`（tags 决定路由）
2. 第一层路由按 tag 召回 env（`repo-lint` / `repo-test` / `issue-triage`）
3. supervisor 拉起 `mcp-repo` 这个 stdio MCP，调 `run_lint` / `run_tests` / `list_issues`
4. 产物按 schema 校验后落盘，决策链写进 `selfhost/var/capgate.db`
5. `capgate chain <task_id>` 能复盘每一步为什么这么走

也就是说：**网关调度自己，账本记录自己，产物校验自己。** 没有为自举写任何特例代码。

## 一个踩过的坑（值得记住）

仓库 MCP 里跑子进程必须显式 `stdin=subprocess.DEVNULL`。

MCP 的 stdio 传输把 stdin/stdout 都占成了协议管道。子进程默认继承 stdin，
在 Windows 上会导致 `call_tool` 永久挂起——`list_tools` 正常、`ping` 正常，
一碰 `subprocess` 就卡死。切断 stdin 之后一切正常。

## 为什么不把 selfhost 放进主包

主包是网关本身；selfhost 是**一个使用它的工作区**。两者共用同一套代码，
但 selfhost 不该被打进 wheel，也不该出现在 `capgate` 的 CLI 里——
能自举是因为网关是通用的，不是因为网关里写了自举逻辑。
