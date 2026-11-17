---
title: "retry-alt 目前只降级，不换候选重跑"
labels: [bug, p1]
---

## 现状

`FailurePolicy` 声明了 `abort / degrade / retry-alt` 三种策略，但
`Orchestrator._apply_failure_policy` 对 `retry-alt` 只做两件事：把任务标成
`degraded`、写一条 `env.retry_alt` 事件。**它没有真的换一个候选 env 重跑。**

配置里写 `retry-alt` 的人会以为有重试，实际上没有——这比不写更糟。

## 期望

- 从第一层路由的候选里挑下一个未尝试、且产物契约可满足的 env 重跑同一段工作。
- 最多重试 `N` 次（建议 1~2），重试也要记进决策链，不能偷偷发生。
- 全部候选都失败时，按 `degrade` 处理并在任务结果里说明"试过哪些、各自怎么失败"。

## 验收

- [ ] 单测：构造一个必失败的 env + 一个会成功的同契约候选，断言第二次真正执行
- [ ] 决策链里能看到 `retry` 阶段与"为什么换到这个 env"
- [ ] `capgate demo` 行为不变
- [ ] 文档（README 已知限制、ADR-010）同步更新

## 备注

重试会放大成本，所以"换候选"必须比"原地重试"更划算才做。原地重试暂时不支持，
因为 scripted 路径是确定性的，重复跑没有意义。
