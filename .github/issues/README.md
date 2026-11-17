# Issues

待办事项的完整描述（背景 + 期望 + 验收标准），按 GitHub issue 的格式写在这里，
可以用 `gh` 一键建到仓库上：

```powershell
gh auth status
.\scripts\file-issues.ps1 -DryRun     # 先看会建哪些
.\scripts\file-issues.ps1             # 真建
```

| # | 标题 | 标签 |
|---|---|---|
| 001 | retry-alt 目前只降级，不换候选重跑 | bug, p1 |
| 002 | MCP 2.x 兼容层 | tech-debt, p1 |
| 003 | openai 适配器联调 + 成本记账 | enhancement, p1 |
| 004 | cli 适配器接真实命令行 Agent | enhancement, p2 |
| 005 | 产物库 TTL 与引用计数 | enhancement, p2 |
| 006 | 流式产物通道 | enhancement, p2 |
| 007 | 多进程并发假设 | tech-debt, p2 |
| 008 | llm_fallback 实现与评估 | enhancement, p2 |
| 009 | tag 受控词表与任务类型规范化 | tech-debt, p1 |
| 010 | 看板补人工验收 / pin / 重跑 | enhancement, p2 |
| 011 | [上游] BCA 仓库无法构建 | upstream, p1 |
| 012 | [上游] FIT 日志写 stdout | upstream, p2 |
| 013 | selfhost 还没把 README 动效与文档校验纳入门禁 | enhancement, p2 |
| 014 | selfhost 只能检查不能修：缺受限写入能力 | enhancement, p3 |

> 原 `#010 开 CI` 已直接实现（`.github/workflows/ci.yml`），不再占一个 issue。

## 优先级含义

- `p1`：影响正确性或阻断后续工作，先做。
- `p2`：明确要做，但不挡路。
- `p3`：想做，先记着。

## 建议顺序

1. **001 retry-alt** —— 现在是个会误导人的"假功能"。
2. **011 上游 BCA 构建** —— 让能力指向源码仓库而不是临时副本。
3. **009 tag 词表** —— 样本攒得越久越难改，越早越好。
4. 其余按 `p2` 排。
