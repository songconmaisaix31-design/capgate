---
title: "多进程并发假设：账本与产物库都默认单写者"
labels: [tech-debt, p2]
---

## 现状

- SQLite 用 WAL + `busy_timeout=5000`，代码里假设"单进程、可重入锁"。
- `CLI` 和 `serve` 同时跑是常见用法，两个进程都会写账本。
- 产物库用 `task_id/name@ver-sha.json`，并发写同名产物会互相覆盖（内容相同则无害，
  内容不同则是静默丢数据）。

## 期望

- 明确并发模型并写进 ADR：要么"同一时刻只允许一个写进程"（加文件锁），
  要么把写路径收敛到一个常驻写队列。
- 产物写入改为 `O_EXCL` 或先写临时文件再原子 rename，杜绝半截文件。
- `capgate doctor` 检测是否存在多个进程同时持有账本。

## 验收

- [ ] 并发跑两个 `capgate solve` 时，账本不丢事件、不报 `database is locked`
- [ ] 产物写入是原子的（进程被 kill 不会留下半截 JSON）
- [ ] ADR 里写清"本机单写者"这个前提什么时候必须被打破

## 备注

同时注意本机 SQLite 3.50.4 落在 WAL-reset 竞态区间（修复于 3.51.3）。若真要多写者，
先升级 SQLite 或退回 `CAPGATE_SQLITE_JOURNAL=delete`。
