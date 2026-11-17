---
title: "openai 适配器没有对真实端点联调过，成本也没记账"
labels: [enhancement, p1]
---

## 现状

`capgate/agents/openai_compat.py` 按 `/v1/chat/completions` 写完了，支持工具调用循环与
JSON 产物解析，但：

- 没有任何一次真实端点调用；解析逻辑只有单测（`tests/test_agent_parsing.py`）。
- `usage` 字段拿到了却没进账本，导致路由的"成本"维度只有时延，没有 token/金额。

## 期望

- 用一个真实端点（DeepSeek / 通义 / vLLM 任一）跑通 crawler 之外的至少一个 env。
- `runs.meta.usage` 落账，`perf()` 增加 `avg_tokens` / `avg_cost` 聚合。
- 路由的成本惩罚能同时吃时延与 token。

## 验收

- [ ] `CAPGATE_LLM_BASE_URL` + key 配好后，`capgate solve` 能走 openai adapter 出产物
- [ ] 账本里能看到每次 run 的 prompt/completion tokens
- [ ] 成本惩罚公式有单测（重模型 + 高 token 时分数被压低）

## 备注

不要在测试里造假 token；联调用例打 `integration` 标记，缺 key 就 skip。
