# 贡献指南

短版：**ruff 说了算，测试护住行为，提交小步聚焦。** 这一页只写不显然的部分。

## 命令

```bash
python -m capgate doctor      # 自检：依赖 / SQLite / 能力与 env 能否载入
python -m ruff check --fix    # 静态检查（规则见 pyproject）
python -m ruff format         # 格式化（唯一格式标准，别手调空白）
python -m pytest -q           # 全量测试
python -m pytest -q -m "not integration"   # 跳过需要本机真实 MCP 的用例
python -m capgate demo -s all # 端到端自检
```

`pre-commit install` 之后，上面两条 ruff 会在提交前自动跑。

## 分层与依赖方向

```
编排 → 领域 env → 能力池 → 传输（MCP / SDK）
```

依赖只允许往右指。**能力层不许知道 env，env 不许知道路由，路由不许知道 MCP。**
新增功能时先问一句"这段代码属于哪一层"，答不上来通常是抽象错了。

## 代码风格

- 只认 `ruff`（lint + format）。规则在 `pyproject.toml` 的 `[tool.ruff]`，不要用 `# noqa` 绕过；
  确实要绕，就在 `per-file-ignores` 里写清楚理由。
- 公开函数要类型注解；内部函数能注就注。`from __future__ import annotations` 是默认。
- 数据交换用 pydantic 模型，不要裸 dict 穿透两层以上。
- **错误信息写给用户看，用中文**，并说清"缺什么、怎么补"。
  `raise AgentError("模板变量不存在：'page.html'（断在 'html'）")` 是对的；
  `raise ValueError("bad")` 不是。
- 不要新增第三方依赖，除非标准库明显不够用；新增要在 PR 里写一句为什么。
- 注释解释"为什么"，不解释"做了什么"。决策类改动同步更新 `docs/decisions.md`。

## 测试

- 改行为必须加或改测试。没有测试的行为等于没做。
- 单测不碰网络、不启子进程；需要真实 MCP 的用例放 `tests/test_integration_mcp.py`，
  打 `@pytest.mark.integration`，缺依赖时 `skipif` 跳过——**不许用 mock 假装通过**。
- 端到端用 `capgate demo`；断言产物内容，不只断言 `status == "ok"`。

## 提交

Conventional Commits，首行不超过 72 字符：

```
feat(router): 支持按任务类型 pin 模型
fix(mcp): 启动超时后回收子进程
docs(adr): 补充 ADR-014 产物 TTL
```

一个提交只做一件事。重构和改行为不要混在一起。

## 新增东西的最短路径

| 目标 | 改哪里 | 不用改 |
|---|---|---|
| 新领域 | `envs/*.yaml` | 任何 Python |
| 新能力 | `capabilities/*.yaml` | 网关 |
| 新 Agent 框架 | `capgate/agents/` 实现 `AgentAdapter` + `@register` | 路由、编排、账本 |
| 新产物类型 | `schemas/*.schema.json` + env 声明 | 产物库 |

新增 MCP 能力后，先跑 `python -m capgate probe <能力 id>` 确认真能列出工具，
再用 `python -m capgate call <能力 id> <工具>` 核对一次真实返回形状，最后才写 recipe。

## PR 检查清单

- [ ] `ruff check` 与 `ruff format --check` 通过
- [ ] `pytest` 通过（集成用例要么通过，要么明确 skip）
- [ ] 新能力/env 已用 `probe` / `call` 实测过
- [ ] 改了行为就改了测试；改了取舍就改了 ADR
- [ ] 没有新增未说明的依赖，没有硬编码绝对路径（用 `${VAR:default}`）
