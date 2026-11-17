---
title: "看板只能看不能操作：补人工验收、pin 与重跑"
labels: [enhancement, p2]
---

## 现状

`/api/tasks/{id}/outcome` 已经存在，看板也能展示决策链，但页面上没有任何操作入口：
人工验收只能回到 CLI 敲 `capgate rate`，pin 要手改 `router.yaml`，失败任务不能重跑。

## 期望

- 任务详情页加三个按钮：验收（好/差）、pin 当前 env、重跑。
- 验收后立即刷新聚合，让人看到"我这一下改变了什么"。
- pin 写入 `router.yaml` 并显示在决策链上（`strategy=pin`）。

## 验收

- [ ] 网页上打完差评，`/api/stats` 里该 env 的胜率立刻变化
- [ ] pin 之后同一 task_type 的决策链显示 `pin`
- [ ] 重跑产生新 task_id，旧任务保持不可变

## 备注

"我这一下改变了什么"是这套系统能不能被信任的关键。人看不到反馈闭环，就不会去验收，
没有验收标签，路由就退化成静态表。
