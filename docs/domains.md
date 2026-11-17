# 领域划分

env 按**业务域**切，不按技术切。技术粒度（"浏览器 env""HTTP env"）会让一个任务
要拉五六个环境，编排成本先压垮人。

当前共 8 个域、9 个环境、7 个能力。

| 域 | env | 能力 | 执行形态 | 被选中的入口 tag |
|---|---|---|---|---|
| 数据采集 | `crawler` | `mcp-fetch` | scripted | 爬虫 / 抓取 / 采集 |
| 内容分析 | `analyst` | `sdk-text` | scripted（消费 `crawl_batch`） | 汇总 / 分析 / 报告 |
| 链上数据 | `bca-chain` | `mcp-bca` + `sdk-text` | scripted | 链上 / 区块链 / 区块链工具 / 能力清单 |
| 链上数据 | `bca-evm` | `mcp-bca` | scripted（需 RPC 配置） | EVM / 地址 / 余额 |
| 开发环境 | `fit-env` | `mcp-fit` + `sdk-text` | scripted | 环境 / 工具链 / 依赖 / 版本 / 环境管理 |
| 知识问答 | `doc-qa` | `mcp-rag` | scripted | 文档 / 问答 / 检索 |
| 演示 | `chain-watch` | `sdk-chain` | scripted（伪数据） | 演示链 / 演示余额 |
| 代码修改 | `codex-dev` | —（cli 适配器） | cli | 代码修改 / 修复 / 重构 / 实现 |
| 判断上下文 | `swa-attention` | `mcp-swa` | scripted | 注意力 / 判断 / 上下文 / 焦点 |

## 能力池

| 能力 | 类型 | 传输 | 版本 | 特点 |
|---|---|---|---|---|
| `mcp-fetch` | MCP | stdio | 0.1.0 | 随包示例；抓 URL、抽链接/正文 |
| `mcp-rag` | MCP | stdio | 0.1.0 | 随包示例；本地语料检索 |
| `mcp-bca` | MCP | stdio | 1.0.0 | 真实 BCA，125 个工具，白名单 7 个 |
| `mcp-fit` | MCP | stdio | 0.2.0 | 真实 FIT，6 个注册表工具 |
| `sdk-text` | SDK | python | 0.1.0 | 文本/清单压缩，进程内 |
| `sdk-chain` | SDK | python | 0.1.0 | 演示桩，返回 `source: demo-deterministic` |
| `mcp-swa` | MCP | stdio | 0.1.0 | 真实 SWA / Astrocyte，只读放行 3 个工具 |

`mcp-bca` 被 `bca-chain` 与 `bca-evm` 同时引用且 `shared: true`，所以全池只有一个
Node 进程：第二个 env 复用它，省掉一次 3 秒级的冷启动。这是"能力可复用"最直接的收益。

## tag 是路由词表

第一层路由靠 tag 交集召回，所以 tag 是**接口**，不是备注：

- 一个 env 至少一个 tag；tag 决定它能不能被某个任务找到。
- 同一句话的多种说法（"爬虫"/"抓取"/"采集"）要显式都写上，不要指望语义匹配。
- 一个域里多个 env 时，tag 要**尽量不重叠**，否则一个任务会同时命中它们，变成随机竞争。
  `bca-chain`（清单）与 `bca-evm`（余额）就是按这个原则拆开的。
- 演示环境用"演示"前缀（`演示链`），避免和真实能力抢路由。

> 现状：tag 已纳入受控词表治理（issue #9），见下一节。

## 受控词表（taxonomy.yaml）

tag 是路由词表，也是账本里 `task_type` 的学习单位。自由文本会把"同一件事的
不同写法"劈成几份样本，Thompson 后验永远攒不够。所以根目录的 `taxonomy.yaml`
把 tag 管起来，`capgate/taxonomy.py` 提供规范化接口。

词表结构：

```yaml
version: 1            # 每次变更必须递增
terms:                # 标准词：id 是 task_type 的唯一合法取值
  - id: 爬虫
    aliases: [抓取, 采集, 爬取]
    desc: 抓取网页正文、抽取链接与文本
    owner: 数据采集
pins: {}              # 人工 pin：别名 -> 标准词，优先级最高，算法不得覆盖
```

三条规则：

1. **入库前规范化**：任务入库时调用 `Taxonomy.normalize_tags(tags)`，别名归并到
   标准词，账本只存标准词。"抓取" 与 "爬虫" 因此落到同一个 `task_type`。
2. **版本号追溯**：改词表必须递增 `version`；历史任务已固化的 `task_type` 不被
   追溯改写——规范化只在入库那一刻发生，旧实例保留旧映射。
3. **人工 pin**：`pins` 里的映射优先级高于 `aliases`，`normalize` 永不覆盖它；
   pin 目标必须是已登记的标准词，否则加载报错。

词表还保证 `task_type` 取值有限、可枚举：`Taxonomy.task_types()` 返回全部标准词，
`normalize_tags(tags, strict=True)` 会在遇到词表外 tag 时拒绝，防止碎片词悄悄溜进账本。
`capgate envs` 用 `overlapping_pairs(...)` 提示哪些 env 的 tag 高度重叠（会抢路由）。

## 新增一个域

1. 写 `capabilities/*.yaml`（能力不存在时）：MCP 填 `transport: stdio|http`，
   进程内函数填 `transport: python` + `entries`。路径用 `${VAR:default}`，不要写死。
2. `python -m capgate probe <能力>` 确认真能列出工具。
3. `python -m capgate call <能力> <工具> --args '{...}'` 核对一次真实返回形状。
4. 写 `envs/*.yaml`：tags、`capabilities`（带版本说明符）、`agent.recipe`、`artifacts` 契约。
5. 把 env 的新 tag 登记进根目录 `taxonomy.yaml`（新标准词或某条词的别名），并递增
   `version`；跑 `python -m pytest tests/test_taxonomy.py` 确认词表仍闭合。
6. 需要新产物类型就加 `schemas/<name>.schema.json`。
7. `python -m capgate solve "..." -t <tag>` 跑通，再看 `capgate chain <task>` 的决策链是否解释得通。
