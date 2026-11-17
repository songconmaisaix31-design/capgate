# 产物库与存储

本文说明产物怎么落盘、怎么回收、怎么走流式、以及"本机单写者"这个并发前提。
设计取舍的来龙去脉在 [`docs/decisions.md`](decisions.md)，对应 issue #5 / #6 / #7。

## 布局与内容寻址

```
var/artifacts/<task_id>/<name>@<version>-<sha256>.json
```

- 新文件名使用完整 SHA-256。旧账本中的短摘要路径仍按保存的路径读取。
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
writer.write_chunk({"url": "u"})  # 逐条落盘，下游此刻已能读到
writer.write_chunk({"links": ["a"]})
artifact = writer.seal()  # 组装最终快照（sha = 最终快照哈希）
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

`capgate demo -s stream` 运行流式链路。`demo.run_stream_comparison` 用同一示例输入比较
整块与流式执行。两组实测均在本机 Windows、Python 3.13、预热 MCP 后交替跑六对任务。

| 模式 | 中位时延 |
|---|---|
| 整块 `crawler → analyst` | 44–47 ms |
| 流式 `crawler-stream → analyst-stream` | 64–66 ms |

流式任务的 `before_seal=True` 事件记录了下游在最终快照前的消费。两种模式的汇总结果一致，
上游的 `fetched_at` 记录各自运行时间。这个工作负载的端到端时延目标尚未达成。

## 并发模型：本机单写者（issue #7）

同一时刻只有一个进程能访问账本。`Ledger` 先获取进程内 `RLock`，再获取 `filelock`
提供的 `<db>.lock`。连接关闭也持有这两把锁，覆盖 SQLite 的隐式 checkpoint。
`capgate doctor` 用非阻塞探测查看账本锁是否正被占用。

产物先完整写入同目录的唯一临时文件，再通过硬链接将文件原子安装到最终路径。若路径已存在，
只有磁盘内容与新内容字节一致时才复用。使用产物库的本地文件系统需支持硬链接。

这些锁协调同一台机器上共享账本路径的 capgate 进程。跨主机或由外部进程直接写 SQLite 时，
需要单独选择并发模型。使用 SQLite 3.51.3 之前的版本时，`doctor` 会报告 WAL-reset 风险，
可设 `CAPGATE_SQLITE_JOURNAL=delete` 使用 DELETE 日志模式。

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
