# 架构

本文说明 capgate 的模块边界、一次任务的数据流、以及几个"为什么这么切"的关键决定。
设计取舍的来龙去脉在 [`docs/decisions.md`](docs/decisions.md)。

## 分层

```
任务 TaskSpec
   │
   ▼
┌──────────────── 控制面 ────────────────┐
│ Router            双层路由 + 探索 + pin │
│ Orchestrator      契约组流水线 / 波次   │
│ Ledger            决策链 + 结果 + 聚合  │
│ ArtifactStore     内容寻址 + schema     │
└───────────────┬───────────────────────┘
                │ 只经由产物与决策
                ▼
┌──────────────── 领域层 ────────────────┐
│ EnvManifest  能力引用 + Agent 画像 + 契约│
│ Supervisor   实例复用 / 限流 / 热池     │
└───────────────┬───────────────────────┘
                │ 版本解析 + 连接管理
                ▼
┌──────────────── 能力层 ────────────────┐
│ Pool(MCP / RAG / SDK / doc) 版本钉死    │
│ MCPHub  共享连接单例                    │
└───────────────────────────────────────┘
```

依赖方向是单向的：能力层不知道 env，env 不知道路由，路由不知道 MCP。
每一层都能单独替换。

## 一次任务的数据流

1. `Orchestrator.run(task)` → 账本登记任务，发 `task.start` 事件。
2. **第一层路由**：`Router.route_domains(task)` 按 tag 召回 env，按账本里
   `(env, 任务类型)` 的 Beta 后验采样打分，产出 `Decision(stage="domain")` 并落账。
3. **编排计划**：`_plan_pipeline` 组流水线——
   - 起点必须是能独立启动的 env（`consumes` 为空或由任务输入直接提供）；
   - 第一轮只收"不依赖别人产物"的 env，它们构成并行波次；
   - 第二轮按产物契约接力，`consumes ⊆ produced` 才准入；
   - 每个被排除的候选都记下原因。
4. **波次执行**：`_execute` 按依赖切波，波内 `asyncio.gather` 并行，
   每个 env 先过第二层路由（选 Agent 的哪个模型），再交 `Supervisor.execute`。
5. **产物落地**：`ArtifactStore.put` 做 schema 校验 → 内容寻址落盘 → 账本登记。
   下游 env 只拿到它 `consumes` 声明的那几个产物，拿不到别的。
6. **收尾**：失败按 env 的 `failure_policy` 处理；任务状态与耗时落账；
   `chain()` 能把整条链路原样吐出来。

## 关键模块

### `router.py` — 两层，不是一张大表

| 层 | 决策 | 候选 | 数据来源 |
|---|---|---|---|
| 第一层 | 进哪几个领域 | tag 命中的 env | `ledger.perf(task_type=…)` 按 env 聚合 |
| 第二层 | 用哪个模型 | env 声明的 `models` | `ledger.perf(env_id=…, adapter=…)` 按 model 聚合 |

打分 = `Beta(1 + prior + wins, 1 + fails)` 采样 × 成本惩罚
`1 / (1 + w · avg_ms / ref_ms)`。

- 先验 `prior` 是 env manifest 里的"人工先验成功当量"，不是拍脑袋的权重；
  样本进来后自然被数据覆盖。
- Thompson 的探索是**内建**的：样本少的候选方差大，会被抽中。
- `epsilon` 是备选策略；`static` 按先验排序，用于测试与基线。
- `pins` 优先级最高，用于线上救火。
- `judge` 钩子留给 LLM 兜底裁决，默认不启用。

### `orchestrator.py` — 契约驱动的编排

编排只看一件事：**这个 env 需要的产物现在有没有**。由此得到三个性质：

- 没有契约关系的 env 自动并行（`crawler ∥ chain-watch`）；
- 有契约关系的自动排队（`crawler → analyst`）；
- 契约无法满足的候选被排除，并在决策链里写明缺什么。

`_pick_head` 单独处理"起点"：头名可能是个只能接力的 env，直接开端会注定失败。
调整起点时会写进 `pipeline` 决策的理由里，不偷偷改。

### `supervisor.py` — 实例生命周期

- 每个 env 一个信号量限制并发（`resources.max_parallel`）。
- 实例按 env 缓存复用（`MAX_WARM_PER_ENV`），带 TTL 回收。
- `shared: true` 的能力连接由 `MCPHub` 全池单例持有；`shared: false` 的按实例前缀隔离，
  实例销毁时 `drop_prefix` 一起关掉。
- "表现好的留下"落成三条独立机制：热实例（本模块）、默认路由意图（账本驱动 router）、
  可复现快照（版本钉死 + 账本记录 exact version）。三者互不替代。

### `pool.py` — 版本、白名单、路径

- `CapabilityVersion.tools` 是白名单：BCA 有 125 个工具，`mcp-bca` 只放行 7 个。
  工具描述会占上下文，也会让模型有机会调用根本不该自动调的东西（签名、写链）。
- `command / args / cwd / url / env` 支持 `${VAR:default}`，在 `Pool.resolve` 时展开，
  所以能力声明既能换机器只改一处，又不强迫使用者先配环境变量。
- 解析结果是 `Resolved`，`exact` 版本会写进 `runs.meta.resolved`。

### `mcp/client.py` — 为什么要有 connection host

MCP 的 stdio 客户端上下文必须在**同一个 task** 里进出。所以每个连接开一个常驻 host task
进上下文，业务侧只拿 session 调工具。这带来两个好处：

- 一个 MCP 进程可以被多个 env 共享（`shared: true`）；
- 关闭时机可控，不会因为调用方任务的取消而泄露子进程。

子进程的 `PYTHONPATH` 会注入 capgate 包根，所以示例 MCP server 无论从哪个目录启动都能
`python -m capgate.examples.mcp_fetch`。

### `ledger.py` — 决策链即接口

表：`tasks / events / decisions / runs / artifacts / outcomes`。

`perf()` 的成败判定是"运行成功 **且** 没有人工差评"，且只取每个任务的**最新**一条
人工标签（子查询取 `MAX(id)`），否则多次验收会把 run 重复计数。

`chain(task_id)` 返回一个可直接渲染的字典——决策链不是日志，是产品的一部分。

### `artifacts.py` — 内容是地址

`var/artifacts/<task_id>/<name>@<version>-<sha8>.json`。sha256 是完整摘要，
文件名用前 8 位便于人读。有 `schemas/<name>.schema.json` 就严格校验，没有就要求顶层是
对象或数组。

## 数据模型要点

- `EnvManifest.capabilities[].version` 用 PEP 440 说明符；解析到确切版本后写进
  `runs.meta.resolved`，保证半年后能复现"当时用的哪个版本"。
- `AgentSpec` 只有 `adapter` + `models` + `recipe` + `config`。网关不知道 agent 是什么框架。
- `Decision` 带 `candidates`（每个候选的分数、样本数、说明）和 `reason`。
  这是可解释性的最小单位。

## 扩展点

| 想加什么 | 改哪里 | 不需要改什么 |
|---|---|---|
| 新领域 | `envs/*.yaml` | 任何 python 代码 |
| 新 MCP / RAG / SDK 能力 | `capabilities/*.yaml` | 网关代码 |
| 新 Agent 框架 | `agents/` 下实现 `AgentAdapter` 并 `@register` | 路由、编排、账本 |
| 新路由算法 | `router.py` 的 `_rank` / `route_agent` | 编排与能力层 |
| 新产物类型 | `schemas/*.schema.json` + env 声明 | 产物库 |

## 失败模式

| 现象 | 根因 | 现在的处理 |
|---|---|---|
| 任务一开局就失败 | 排了个需要上游产物的 env 当起点 | `_pick_head` 保证起点可独立启动 |
| 下游拿到空数据 | 契约没声明 | `consumes` 强制显式，缺失即排除并记原因 |
| 结果看着成功但没用 | 只看运行状态 | `capgate rate --bad` 人工标签压过自动判定 |
| 路由锁死在一个组合 | 无探索 | Thompson 后验采样内建探索 |
| 决策说不清 | 只记结果不记候选 | 每次决策落 candidates + reason |
| MCP 子进程泄漏 | 连接生命周期错位 | connection host 单独持有上下文 |

## 性能与规模假设

- 单进程、单机、单人。SQLite 同步写，写路径微秒级，不阻塞事件循环。
- 一次任务最多 `MAX_ENVS_PER_TASK = 8` 个 env。
- 实测：示例 MCP 链路的 `crawler` 约 2.3s（三次 MCP 往返 + Python 子进程启动）；
  `fit-env` 约 0.8s；`bca-chain` 约 3.3s（Node + 大量 SDK 加载）。
  同一能力被第二个 env 复用时降到 10ms 级——这是 `shared: true` 的主要收益。
- 每个共享 MCP 常驻一个子进程；冷启动慢的在能力声明里调 `startup_timeout`（BCA 给了 180s）。

超出以上假设（多用户、多机、高并发）需要换的部件：账本 → Postgres、产物库 → 对象存储、
`MCPHub` → 独立服务。分层设计就是为了让这些替换局限在一层里。
