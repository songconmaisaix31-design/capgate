# 产物库与存储

本文说明产物怎么落盘、怎么回收、怎么走流式、以及"本机单写者"这个并发前提。
设计取舍的来龙去脉在 [`docs/decisions.md`](decisions.md)，对应 issue #5 / #6 / #7。

## 布局与内容寻址

```
var/artifacts/<task_id>/<name>@<version>-<sha8>.json
```

- sha256 是完整摘要，文件名用前 8 位便于人读；`name@version` 让人一眼看出契约版本。
- 同一份内容 → 同一 sha → 同一文件名。**同一 task 内相同内容天然去重**。
- 账本 `artifacts` 表登记每一份产物（sha256 / path / bytes / producer），文件内容在磁盘上，
  账本记录是审计来源。**删文件不动账本记录**——历史任务的 `chain` 依然能看到当时产了
  什么，只是 `load` 时路径 404（可接受，见 issue #5 验收）。

### 为什么目录按 `task_id` 而不是 `sha256`

内容寻址的"跨任务复用"需要按 sha256 建索引；现在按 `task_id` 分目录，同一内容会在
不同任务下各存一份。当前没有跨任务复用，`task_id` 目录更直观、清理更省事（删一个
任务 = 删一个目录）。**将来若做跨任务复用，先把索引换成 sha256 再动 GC**，否则
会误删仍被别处引用的产物——这是 issue #5 备注里"顺手想清楚"的结论。

## 产物回收（GC，issue #5）

`capgate gc` 按任务 TTL（默认 30 天）清理过期产物，两条保护规则：

1. **未完成任务**（`tasks.status='running'`）的产物不删；
2. **被未完成任务消费**的产物不删（引用计数，见 `artifact_refs` 表）。

引用计数的语义：`ledger.add_artifact_ref` 登记"哪个 env 的哪次 run 消费了哪个 sha256"。
GC 只护住"被**未完成**任务引用的产物"——任务一结束，它消费的上游产物就重新进入
可清理状态，否则流水线里最常被消费的 `crawl_batch` 永远删不掉。

```bash
capgate gc --dry-run   # 先看：将删除 N 个、可回收 M MB（受保护的另列）
capgate gc --yes       # 确认执行；每次删除记一条 artifact.gc 事件，可审计
```

## 流式产物（issue #6）

ADR-006 规定 env 之间只走产物库，代价是下游必须等上游**整块产出**。流式通道是这条
规矩上的受控例外：`EnvManifest.stream: true` 的 env 按 chunk 写、按 chunk 读，仍经产物
库中转，默认关闭。

**机制**：producer 每写一条 chunk 立即落盘到 `var/artifacts/<task>/<name>@<ver>.stream`
（JSON-lines，二进制 fd 直写、无用户态缓冲），并用内存里的 `asyncio.Event` 通知；
consumer 增量读同一个 `.stream` 文件。**chunk 数据走磁盘、通知走内存**，编排层把
producer 与它的流式 consumer 用 `asyncio.gather` 并发跑——所以下游能在上游写出最终快照
**之前**就消费到先到的 chunk。`seal` 把 chunk 组装成最终快照（dict chunk 浅合并成对象，
其余拼成列表）并原子落盘。

```python
writer = store.open_stream(task_id, name, env_id=env.id, run_id=run_id)
writer.write_chunk({"url": "u"})          # 逐条落盘，下游此刻已能读到
writer.write_chunk({"links": ["a"]})
artifact = writer.seal()                  # 组装最终快照（sha = 最终快照哈希）
```

recipe 里两个新步骤：producer 用 `stream_emit`（写一条 chunk），consumer 用
`stream_collect`（增量读直到 `until` 的 key 齐了，可挂多个 `until` 做分阶段消费）。

关键取舍：

- **sha256 取最终快照的哈希，不是 chunk 序列的 Merkle 根。** 重放语义是"拿到最终
  快照"，地址必须和重放时读到的东西一致；Merkle 根只负责传输完整性校验，v1 不做。
- **断流是显式的**：producer 失败时编排层 `mark_truncated()` 后仍 `seal`，落盘的快照带
  `Artifact.truncated=True`，下游读到 `truncated` 标记，而不是静默吞半截或永远等下去。
- **快照对应完整实际落盘字节**：`seal` 写出的 `.json` 是最终快照，sha 即其哈希；
  `.stream` 是传输通道，seal 后删除。
- 决策链在 `runs.meta.mode` 与 `env.finish` 事件里记录 `stream | block`；consumer 的
  `agent.stream_collect` 事件带 `before_seal` / `truncated`，可审计"是否真在快照前消费"。

### 演示与实测（crawler-stream → analyst-stream）

`capgate demo -s stream` 跑流式链路；`demo.run_stream_comparison` 用同一个 fixture 对比
整块与流式的结果和耗时（结果一致，`fetched_at` 除外）。本机（Windows，Python 3.13，
暖 MCP）六对交替、固定样本，取中位数：

| 模式 | 中位时延 |
|---|---|
| 整块 `crawler → analyst` | ~44 ms |
| 流式 `crawler-stream → analyst-stream` | ~64 ms |

**结论**：这个微工作负载（3 次 MCP 往返 + 进程内汇总）下流式**没有更快**——重叠省下的
只有一次 `extract_text` 往返（~5ms），而增量通道的协作开销（事件通知 + 增量文件读写 +
并发调度，~17ms）更大。优化前 per-chunk 反复 `open`/`close` 文件句柄时是 ~96ms，改成
持久 fd 后降到 ~64ms，已把关键路径上最大的开销修掉。流式的价值在"边爬边喂"的大负载
（每 chunk 本身耗时可观）才体现；对 44ms 级的小任务，整块更省。此结果已如实记录，
`before_seal=True` 的证据说明**增量交付机制本身是正确**的（下游在快照前已消费，
汇总与整块相等）。未用人造延时、未调整任务隐藏开销，也不宣称时延已通过。

## 并发模型：本机单写者（issue #7）

**决定**：同一时刻只允许一个进程写账本，用一把跨进程文件锁（`FileLock`）串行化每次
账本访问；产物写入走"临时文件 + 原子 rename"。

- `Ledger` 每次进出临界区先拿进程内 `RLock`（FastAPI 线程池安全），再拿进程间
  `FileLock`（`<db>.lock`）。`FileLock` 直接包装成熟库 `filelock`：它统一平台差异
  （Windows `msvcrt` / Unix `flock`），阻塞 `acquire` 带真实超时（轮询 `is_locked` 而非
  裸 `flock` 无限阻塞），`is_locked` 精确区分是否被持有。CLI 与 `serve` 同时跑，写操作
  排队而不会 `database is locked`，也不会在 WAL checkpoint 时丢事件。
- 锁在进程退出时由操作系统自动释放，不会像目录锁那样留下僵尸锁。
- 产物 `put` 先写唯一命名的临时文件再 `os.replace`；进程被 kill 不会留下半截 JSON。
  并发写同名产物（内容相同、sha 相同）时 Windows 的 replace 可能报 access denied，
  代码检测到目标已存在且内容相同则跳过——内容寻址保证这无害。
- `capgate doctor` 用非阻塞探测判断是否有其他进程正在持锁。

**什么时候必须打破这个前提**：多用户、多机、高并发（见 ARCHITECTURE 的规模假设）。
届时账本换 Postgres、产物库换对象存储、`MCPHub` 换独立服务；文件锁只保本机正确性，
不是跨机的分布式锁。另注意本机 SQLite 若 < 3.51.3，WAL-reset 竞态区间仍在，退回
`CAPGATE_SQLITE_JOURNAL=delete` 可彻底绕开（`doctor` 会提示）。

## 相关接口

| 模块 | 职责 | 新增 |
|---|---|---|
| `capgate/artifacts.py` | 落盘 / 读取 / 校验 / 流式 | 原子写、`open_stream`/`read_stream`/`StreamWriter`/`StreamReader`、`delete` |
| `capgate/ledger.py` | 账本 | `artifact_refs` 表、`gc_candidates`、`record_gc`、`reserve_task`、引用计数查询 |
| `capgate/system.py` | 装配 | `System.gc(ttl_days, dry_run)` |
| `capgate/db.py` | 连接 | `FileLock`（包装 `filelock`）、`concurrent_writer_detected` |
| `capgate/cli.py` | 命令 | `capgate gc`、`doctor` 多进程探测 |
| `capgate/orchestrator.py` | 编排 | `_run_streaming_link`（producer/consumer 并发）、登记消费引用、stream/block 模式 |
| `capgate/supervisor.py` | 执行 | 经 contextvar 把流式会话传给 adapter |
| `capgate/agents/scripted.py` | 适配器 | `stream_emit` / `stream_collect` recipe 步骤 |
| `capgate/demo.py` | 演示 | `stream` 场景 + `run_stream_comparison` |
| `envs/crawler-stream.yaml` | 声明 | 流式 producer（`stream: true`） |
| `envs/analyst-stream.yaml` | 声明 | 流式 consumer（`stream: true`） |
