# 领域划分

env 按**业务域**切，不按技术切。技术粒度（"浏览器 env""HTTP env"）会让一个任务
要拉五六个环境，编排成本先压垮人。

当前共 6 个域、7 个环境、6 个能力。

| 域 | env | 能力 | 执行形态 | 被选中的入口 tag |
|---|---|---|---|---|
| 数据采集 | `crawler` | `mcp-fetch` | scripted | 爬虫 / 抓取 / 采集 |
| 内容分析 | `analyst` | `sdk-text` | scripted（消费 `crawl_batch`） | 汇总 / 分析 / 报告 |
| 链上数据 | `bca-chain` | `mcp-bca` + `sdk-text` | scripted | 链上 / 区块链 / 区块链工具 / 能力清单 |
| 链上数据 | `bca-evm` | `mcp-bca` | scripted（需 RPC 配置） | EVM / 地址 / 余额 |
| 开发环境 | `fit-env` | `mcp-fit` + `sdk-text` | scripted | 环境 / 工具链 / 依赖 / 版本 / 环境管理 |
| 知识问答 | `doc-qa` | `mcp-rag` | scripted | 文档 / 问答 / 检索 |
| 演示 | `chain-watch` | `sdk-chain` | scripted（伪数据） | 演示链 / 演示余额 |

## 能力池

| 能力 | 类型 | 传输 | 版本 | 特点 |
|---|---|---|---|---|
| `mcp-fetch` | MCP | stdio | 0.1.0 | 随包示例；抓 URL、抽链接/正文 |
| `mcp-rag` | MCP | stdio | 0.1.0 | 随包示例；本地语料检索 |
| `mcp-bca` | MCP | stdio | 1.0.0 | 真实 BCA，125 个工具，白名单 7 个 |
| `mcp-fit` | MCP | stdio | 0.2.0 | 真实 FIT，6 个注册表工具 |
| `sdk-text` | SDK | python | 0.1.0 | 文本/清单压缩，进程内 |
| `sdk-chain` | SDK | python | 0.1.0 | 演示桩，返回 `source: demo-deterministic` |

`mcp-bca` 被 `bca-chain` 与 `bca-evm` 同时引用且 `shared: true`，所以全池只有一个
Node 进程：第二个 env 复用它，省掉一次 3 秒级的冷启动。这是"能力可复用"最直接的收益。

## tag 是路由词表

第一层路由靠 tag 交集召回，所以 tag 是**接口**，不是备注：

- 一个 env 至少一个 tag；tag 决定它能不能被某个任务找到。
- 同一句话的多种说法（"爬虫"/"抓取"/"采集"）要显式都写上，不要指望语义匹配。
- 一个域里多个 env 时，tag 要**尽量不重叠**，否则一个任务会同时命中它们，变成随机竞争。
  `bca-chain`（清单）与 `bca-evm`（余额）就是按这个原则拆开的。
- 演示环境用"演示"前缀（`演示链`），避免和真实能力抢路由。

> 现状：tag 是自由文本，没有词表治理，长期会碎片化。这是 issue #9 要解决的。

## 新增一个域

1. 写 `capabilities/*.yaml`（能力不存在时）：MCP 填 `transport: stdio|http`，
   进程内函数填 `transport: python` + `entries`。路径用 `${VAR:default}`，不要写死。
2. `python -m capgate probe <能力>` 确认真能列出工具。
3. `python -m capgate call <能力> <工具> --args '{...}'` 核对一次真实返回形状。
4. 写 `envs/*.yaml`：tags、`capabilities`（带版本说明符）、`agent.recipe`、`artifacts` 契约。
5. 需要新产物类型就加 `schemas/<name>.schema.json`。
6. `python -m capgate solve "..." -t <tag>` 跑通，再看 `capgate chain <task>` 的决策链是否解释得通。
