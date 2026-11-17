# 真实模型用量和候选裁决

## 通过模型检索授权语料

把兼容 Chat Completions 的基址放入 `CAPGATE_LLM_BASE_URL`，将 API 密钥配置到进程的 `CAPGATE_LLM_API_KEY`。`doc-qa-model` 使用 `deepseek-flash`，在环境清单中可替换为端点提供的模型。以该环境读取检索结果时，任务问题和命中的文档片段会发送到配置的模型端点。`CAPGATE_CORPUS` 可指定本次授权的语料目录。

```powershell
python -m capgate solve "按文档解释路由和账本的关系" --hint doc-qa-model --json
```

`model_doc_answer` 产物保存回答和支持回答的相对文件路径。模型只能请求本次环境公开的工具，公开名映射到实际能力 ID 和工具名。模型返回的工具名或参数无效时，环境运行立即失败。该环境只响应专用标签、人工指定或任务指定，不进入无标签的自动候选。

## 用量和估价

每次返回的模型请求写入 `llm.usage` 事件。运行的 `meta.usage` 按回合累加已知的 `prompt_tokens`、`completion_tokens`、`total_tokens` 与 `cached_prompt_tokens`，并记录返回了响应的 `requests` 数。只在所有回合都提供某项数据时累加该项，因此缺失读数在汇总中保持未知。

环境可以在 `agent.config.prices_usd_per_million` 中按模型配置 `input`、`output` 和可选的 `cached_input`。单位为每百万 token 的美元价。配置区分缓存价时，需要供应商同时返回缓存命中量才能计算 `cost_usd`。该值由配置价和已知用量计算，实际扣费以供应商账单为准。供应商若按时段定价，比较不同实验时应固定同一套价目。DeepSeek 的用量及现行费率见 [Chat Completions 响应](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)。

`Ledger.perf()` 统计已完成运行的 `avg_tokens` 和 `avg_cost`，单位分别为 token 和美元，每项分母只包含该项已知的运行。路由打分用 `1 / (1 + latency_weight × avg_ms / latency_ref_ms + token_weight × avg_tokens / token_ref)` 乘以采样分数。未知 token 用量不参与惩罚。`static` 领域基线仍按先验排序。

## 异步模型裁决

在 `router.yaml` 中设置 `llm_fallback: true` 后，无标签命中或前两名分差小于 `llm_margin` 的任务可请求一次裁决。任务指定环境、人工指定环境或只剩一个候选时直接保留原决定。

```yaml
llm_fallback: true
llm_model: deepseek-flash
llm_margin: 0.12
llm_timeout_s: 10
llm_max_tokens: 128
llm_max_request_bytes: 16384
llm_reasoning_effort: none
```

`llm_model` 也可通过 `CAPGATE_LLM_JUDGE_MODEL` 提供。裁决使用同一组端点和密钥，可通过 `llm_price_usd_per_million` 配置 `input`、`output` 和 `cached_input`。路由提交任务目标、标签、任务类型和候选环境的名称、领域、标签、先验、分数与样本数。调用时间受配置和任务预算共同限制，请求字节数或生成量有独立上限。超时、服务错误或未知候选 ID 时，任务沿原算法排序执行。裁决结果、失败原因和已知用量在 `routing.llm_judge` 事件中可查。

## 真实端点验证

将 `CAPGATE_TEST_PUBLIC_STATS_ROOT` 指向 [公开统计项目](https://github.com/songconmaisaix31-design/astrocyte-s3-public-stats) 的 Git 工作区。测试只取固定提交 `dbce190be32f52eb93a6fd3909fe4d05213c9ba7` 的 `stats.go`。真实知识问答检查引用、工具调用和逐次用量，路由对照用同一批公开任务比较正确数和按固定价目计算的成本。

首次使用 DeepSeek 真实端点时，知识问答使用了 4 次 API 调用和 3,789 个 token。三条公开任务的静态路由选对 1 条，启用裁决后选对 3 条，裁决消耗 1,488 个 token。此结果只覆盖这批任务。

```powershell
New-Item -ItemType Directory -Force .test-state | Out-Null
python -m pytest --basetemp=.test-state/model-live tests/test_openai_integration.py tests/test_llm_judge_integration.py -q -s
```
