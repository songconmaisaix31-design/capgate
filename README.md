# capgate · 本地能力网关

把"一个 Agent 拖着几个 MCP"翻过来：**网关是主体，Agent 是它调用的能力**。

网关常驻本地，管着一池共享能力（MCP / RAG / SDK / 文档）、一组按业务域划分的
**领域环境（env）**、一份记录每次决策与结果的**账本**。一个任务进来，网关决定拉哪几个
环境、用什么顺序、每个环境用哪个 Agent 的哪个模型，然后把决策链摊开给你看。

```
┌ 编排层   任务 DAG —— 一个任务拉多个 env，它们之间只经产物库交换
├ 领域层   env: crawler / analyst / chain-watch / doc-qa …（能力组合 + 自带 Agent + 自带画像）
├ 能力池   MCP / RAG / SDK / 文档（共享、可复用、版本钉死）
└ 控制面   双层路由 + 决策链账本 + 产物契约
```

## 快速开始

```bash
cd capgate
python -m capgate doctor          # 自检：依赖 / SQLite / 能力与 env 能否载入
python -m capgate envs            # 看领域环境
python -m capgate caps            # 看能力池
python -m capgate demo -s pipeline   # 采集 → 汇总（离线，不需要外网）
python -m capgate demo -s branch     # 采集 ∥ 链上 → 汇总（真并行）
python -m capgate serve --port 8787  # 看板 http://127.0.0.1:8787
```

要求 Python ≥ 3.11，依赖见 `pyproject.toml`。演示用 `file://` 夹具抓页面，
不依赖任何网络；真实抓取把 `--input url=https://...` 传进去即可。

## 核心概念

| 概念 | 含义 | 存在形式 |
|---|---|---|
| 能力 capability | 可被复用的零件：MCP server、RAG、进程内 SDK 函数 | `capabilities/*.yaml`，带版本 |
| 领域环境 env | 封装单位：能力组合 + Agent 画像 + 产物契约 + 资源画像 | `envs/*.yaml` |
| 产物 artifact | env 之间唯一的通道，内容寻址 + schema 校验 | `var/artifacts/<task>/` |
| 任务 task | 一次请求，可拉多个 env | 账本 `tasks` 表 |
| 决策链 | 每一步为什么这么选、候选分数多少、被谁按什么规则排除 | 账本 `decisions` 表 + 看板 |

## 三个设计判断

**1. 环境是封装单位，不是沙箱。**
env 同时是依赖单位、凭据单位、版本单位、隔离单位、失败单位。异构 Agent 的适配器
因此可以下沉到 env 里：网关只认一份 manifest，新增一个框架 = 发一个新 env，
网关零改动。这是"异构"不变成 N×M 适配矩阵的唯一走法。

**2. 路由拆成两层，探索本地化。**
第一层网关选领域，第二层环境自己选 Agent/模型。全局去学 `(任务 × env × agent × model)`
的样本永远不够，域内的 `(任务类型 × agent/model)` 能学，因为域内任务同质。

打分用 Thompson sampling（Beta-Bernoulli 后验采样），**它自带探索**：样本少的候选
方差大，自然会被抽中去试。没有探索的路由会锁死在"第一次还行的组合"上——"GPT 干文档
还行，于是文档永远给 GPT"就是这么来的。同时保留人工 pin 通道，线上选错时能立刻钉死。

**3. 先有账本，再有算法。**
路由表必须由数据长出来。没有 `(env × 任务类型 × agent/model)` 的样本和成败标签，
任何打分都只是"我觉得"。所以第一件产出是可读的决策链和可聚合的账本，不是调度器。
人的验收标签（`capgate rate`）优先于自动判定。

## 三条硬规矩

1. **env 之间禁止直连。** 产物只经产物库中转，编排层负责读取和分发。这样可追溯、可重放、
   可计费；直接通信会退化成不可追溯的点对点网络。
2. **产物契约先于编排。** 一个 env 能不能进流水线，取决于它的 `consumes` 能不能被满足。
   多环境任务失败，八成不是 Agent 不行，是两边对"数据长什么样"理解不一致。
3. **失败逐节点声明。** `abort` / `degrade` / `retry-alt`，按 env 配置，不一刀切。

## 复用清单

原则：能用官方 SDK 和成熟框架就不自己写。

| 依赖 | 版本 | 用途 | 结论 |
|---|---|---|---|
| `mcp`（官方 Python SDK） | 1.28.1 | MCP 客户端（stdio / streamable HTTP）+ 示例 server | 直接用 |
| `fastapi` + `uvicorn` | 0.138 / 0.49 | 本地 HTTP 服务与看板 | 直接用 |
| `pydantic` | 2.x | manifest / 契约 / 决策模型 | 直接用 |
| `typer` + `rich` | 0.25 / 13+ | CLI 与终端渲染 | 直接用 |
| `packaging` | 26.x | 能力版本说明符（PEP 440）解析 | 直接用 |
| `pytest` + `pytest-asyncio` | 9.x | 测试 | 直接用 |

**只借鉴设计、不引入依赖：**

- [IBM ContextForge](https://github.com/IBM/mcp-context-forge)（Apache-2.0）：`tool catalog` ≈ 能力池、
  `virtual server` 打包 ≈ 领域环境。但它没有内容寻址的版本钉死，且依赖 Postgres/Redis 的重部署，
  所以只抄数据模型。
- Thompson sampling：Chapelle & Li (2011) 的 Beta-Bernoulli 形式，30 行自实现即可，
  不引入 `mabwiser`（2024 年后停更、依赖 numpy、context-free 场景属过度设计）。
- Agent 调用契约：字段形状参考 OpenHands / AgentScope 的执行接口（status 四态、trace id 透传、
  usage 由执行方上报）。

**刻意不用：** `mcp-proxy` / `mcpo`（方向相反，十几行 SDK 就能替代）、MetaMCP（TS）、
`SQLModel`（append-only 账本不需要 ORM）、`anyio` task group（本项目跑在单一 asyncio 循环里，
`asyncio.gather` 已满足；若将来要跨后端再换）。

## 目录

```
capgate/
  capgate/
    models.py       契约与数据模型
    ledger.py       SQLite 账本（决策链 + 聚合打分）
    artifacts.py    内容寻址产物库 + schema 校验
    pool.py         能力池（版本钉死）
    envs.py         env 注册表
    router.py       双层路由（Thompson + pin + LLM 兜底钩子）
    supervisor.py   环境监管：实例复用、限流、TTL、热池
    orchestrator.py 任务编排：契约组流水线、并行波次、失败策略
    mcp/client.py   MCP 长连接宿主（共享能力单例）
    agents/         适配器：scripted（确定性快路径）/ openai 兼容 / CLI
    server.py       FastAPI + 看板
    cli.py          命令行
    examples/       随包两个示例 MCP server（抓取、语料检索）
  envs/             领域环境声明
  capabilities/     能力声明
  schemas/          产物 JSON Schema
  fixtures/         离线夹具（页面 + 语料）
  tests/            46 个测试
```

## 扩展

**加一个环境**：写 `envs/xxx.yaml`，声明 tags、能力引用（带版本说明符）、Agent 与产物契约。
`capgate doctor` 会校验能力能否解析。

**加一个能力**：写 `capabilities/xxx.yaml`。MCP 填 `transport: stdio|http`；
进程内函数填 `transport: python` + `entries`。

**接一个 Agent 框架**：实现 `AgentAdapter.run(ctx) -> AgentOutput` 并 `@register("名字")`，
在 env 里写 `agent.adapter: 名字`。网关不感知框架差异。

**改路由策略**：`router.yaml` 切 `thompson | epsilon | static`；要换打分函数改
`capgate/router.py` 的 `_rank` 与 `route_agent`，两处都只依赖账本的 `perf()` 聚合。

## 已知限制（诚实清单）

- `retry-alt` 目前只降级并记事件，**没有真正换一个候选 env 重跑**。
- `openai` 适配器按 `/v1/chat/completions` 写好了，但**没有对真实 LLM 端点联调过**；
  `cli` 适配器同理，只做了接口与解析，未接具体 CLI。
- 没有流式输出、没有产物 GC、没有多进程/多用户隔离。定位是**单人本地常驻**。
- 单链路由（`shared: true`）尚不支持按剩余配额选择性重试；新任务可能先失败一小段时间。
- SQLite 3.50.4 落在 WAL-reset 竞态区间（修复于 3.51.3）。单进程单连接风险低；
  多进程并发写请设 `CAPGATE_SQLITE_JOURNAL=delete`，`doctor` 会提示。
- 演示用的链上余额是**确定性伪数据**，不是真实链上查询。

## 下一步

1. 把 `retry-alt` 补成真正的候选替换，并给"失败但可降级"补一个产物完整性检查。
2. 接一个真实 LLM 的 `openai` 端点，跑同一批任务，让账本里出现真实的 `(env × model)` 对比。
3. 给产物库加 TTL 与引用计数清理。
4. 把 `llm_fallback` 的兜底裁决接起来（`Router` 已留 `judge` 钩子，缺一个实现）。

## 许可

MIT。
