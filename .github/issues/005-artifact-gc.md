---
title: "产物库只增不删：需要 TTL 与引用计数"
labels: [enhancement, p2]
---

## 现状

`var/artifacts/<task_id>/` 只写不清理。跑几个月的本地网关会攒出几十万个小 JSON，
既占盘也让"最近产物"类查询变慢。

## 期望

- 按任务 TTL 清理（默认 30 天），但**被未完成任务引用的产物不能删**。
- 提供 `capgate gc --dry-run` 先看会删什么，再 `--yes` 执行。
- 删除动作记入账本（`artifact.gc` 事件），可审计。

## 验收

- [ ] `capgate gc --dry-run` 输出"将删除 N 个、可回收 M MB"
- [ ] 有引用计数单测：被 consume 的产物即使超 TTL 也保留
- [ ] 清理不破坏 `capgate chain <task>`（历史任务的产物路径允许 404，但账本记录仍在）

## 备注

内容寻址意味着相同内容天然去重；如果将来要做跨任务复用，索引要按 `sha256` 而不是
`task_id`，现在的目录布局会挡路——做 GC 时顺手想清楚。
