---
title: "llm_fallback 只有钩子没有实现"
labels: [enhancement, p2]
---

## 现状

`Router` 留了 `judge: LLMJudge` 钩子，`router.yaml` 里有 `llm_fallback` 与 `llm_margin`
两个开关，但**没有任何地方实现 judge**。打开开关也不会发生什么。

## 期望

- 实现一个 judge：给定任务与候选表（含 tag、先验、样本数），返回一个 env id。
- 只在"头名与次名分差 < margin"或"完全没有 tag 命中"时调用，且每次调用都记进决策链。
- 有预算上限：单任务最多一次，超预算直接回退算法结果。

## 验收

- [ ] 打开开关后，`decisions.strategy` 能出现 `llm-fallback` 且 `reason` 写明分差
- [ ] judge 抛异常时自动回退，任务不受影响（已有 `try/except`，补测试）
- [ ] 有一个对照实验：同一批任务，开/关 fallback 的准确率与成本对比

## 备注

先有实验再决定要不要默认开。直觉上它会拖慢每一次决策，只对冷启动的新任务类型有收益。
