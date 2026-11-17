# selfhost · 用 capgate 构建 capgate

这是一个独立的 capgate 工作区，只做一件事：**把本仓库自己的质量门，跑成一条正常的网关任务**。

This is a standalone capgate workspace whose only job is to run this repository's own
quality gates as ordinary gateway tasks.

```
selfhost/
  capabilities/mcp-repo.yaml   仓库操作 MCP（lint / 测试 / issue / 状态 / 文档 / 受限写入）
  envs/repo-lint.yaml          静态检查 env
  envs/repo-test.yaml          回归测试 env
  envs/issue-triage.yaml       开盘梳理 env
  envs/repo-docs.yaml          文档校验 env（issue #013）
  envs/repo-fix.yaml           受限写入 env（issue #014，无任务卡，手动触发）
  tasks/T0*.md                 任务卡（front-matter: id / type / tags / goal）
  schemas/*.schema.json        产物契约（lint / test / triage / readme_check / patch）
  mcp_repo/__main__.py         那个 MCP 的实现
  loop.py                      驱动：读任务卡 → 交给网关 → 打印决策链
  var/                         自己的账本与产物（gitignored）
```

## 跑起来

```bash
python -m selfhost.loop --list        # 看有哪些任务
python -m selfhost.loop --only T03    # 只跑一条（开盘梳理，约 0.2s）
python -m selfhost.loop --only T04    # 只跑文档校验（含 demo.svg 最新性，约 16s）
python -m selfhost.loop               # 全跑（约 40s，含一次完整测试 + 文档校验）
```

退出码：全绿才是 0。可以直接当 CI 门禁。

## 它证明了什么

一次 `python -m selfhost.loop` 走的是**和线上任务完全相同**的路径：

1. 任务卡 → `TaskSpec`（tags 决定路由）
2. 第一层路由按 tag 召回 env（`repo-lint` / `repo-test` / `issue-triage` / `repo-docs`）
3. supervisor 拉起 `mcp-repo` 这个 stdio MCP，调 `run_lint` / `run_tests` / `list_issues` / `run_readme_check`
4. 产物按 schema 校验后落盘，决策链写进 `selfhost/var/capgate.db`
5. `capgate chain <task_id>` 能复盘每一步为什么这么走

也就是说：**网关调度自己，账本记录自己，产物校验自己。** 没有为自举写任何特例代码。

## 受限写入（issue #014）

`mcp-repo` 除了只读工具，还有 `write_file` 与 `apply_patch`，让网关能"改自己的代码"。
因为风险高，写入是**双保险**，缺一个都落不了盘：

- **工具侧**：默认 dry-run；路径只许仓库内的 `capgate/ tests/ docs/ envs/ capabilities/`，
  `../`、绝对路径、`selfhost/`、`README.md`、符号链接一律拒绝；
- **操作者批准侧**：进程只在启动时通过 `CAPGATE_SELFHOST_APPROVED_PATCH` 收到一个
  **已审 patch 产物路径**。正式写入要求 `allow_write=true`，且当前动作的 `path`、`diff`、
  前置状态（`old_sha256`）都与该产物一致——模型自己点 `allow_write=true` 没用，
  进程没收到批准就拒绝落盘。

`apply_patch` 的 diff 用 `unidiff` 校验只改所声明的一个文件，再用 `git apply` 在隔离
目录生成新内容（不手写 parser）；真实仓库只做同目录临时文件 + 原子替换，过期 / 上下文
对不上 / 多文件 diff 一律拒绝。原始执行理由（`reason`）随 `patch` 产物与账本保留。

目标根目录可用 `CAPGATE_SELFHOST_ROOT` 覆盖（默认取本仓库根），这是测试和换机复用的
正常入口。**谁 review、怎么回滚**：`repo-fix` 没有任务卡，不会进默认循环，只能人工
显式触发；patch 产物是统一 diff，`git apply -R` 就是回滚；review 走正常 PR 流程——
先跑一次 dry-run 产出 diff，人看 diff、批准、再跑一次才真正写。没有这条批准链，
它就是个会自动搞坏仓库的脚本。

## 一个踩过的坑（值得记住）

仓库 MCP 里跑子进程必须显式 `stdin=subprocess.DEVNULL`。

MCP 的 stdio 传输把 stdin/stdout 都占成了协议管道。子进程默认继承 stdin，
在 Windows 上会导致 `call_tool` 永久挂起——`list_tools` 正常、`ping` 正常，
一碰 `subprocess` 就卡死。切断 stdin 之后一切正常。

## 为什么不把 selfhost 放进主包

主包是网关本身；selfhost 是**一个使用它的工作区**。两者共用同一套代码，
但 selfhost 不该被打进 wheel，也不该出现在 `capgate` 的 CLI 里——
能自举是因为网关是通用的，不是因为网关里写了自举逻辑。
