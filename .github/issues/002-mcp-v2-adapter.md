---
title: "MCP 2.x 兼容层：上游已在用 2.3.1，网关仍锁 1.x"
labels: [tech-debt, p1]
---

## 现状

- 网关依赖 `mcp>=1.2,<2`，用的是 1.x 的 `ClientSession` / `StdioServerParameters` /
  `streamablehttp_client`。
- 但 BCA 的 `package.json` 依赖 `@modelcontextprotocol/server@2.3.1`（2.x 是重写版），
  目前能连通是因为 2.x 服务端向下兼容旧协议握手。
- 一旦 BCA 或别的上游改用 2.x 独有特性（如结构化 `structured_content`、新的取消语义），
  网关侧会先崩。

## 期望

- 抽一层 `MCPTransport` 接口，把客户端 SDK 的差异关在里面。
- 2.x 可用时走新客户端，否则回退 1.x；两者对上层暴露同一组 `list_tools / call / close`。

## 验收

- [ ] `capgate probe` 在 1.x 与 2.x 两种安装下都能列出 BCA 的 125 个工具
- [ ] `tests/test_integration_mcp.py` 覆盖两种后端（用环境变量切换）
- [ ] 依赖声明拆成 `mcp` / `mcp-v2` 两个 extra，而不是硬钉死

## 备注

先别急着升级：2.x 的 `Client` 是一等公民、API 全改，迁移面比看起来大。
在没确认上游服务端需要 2.x 特性之前，加适配层比换 SDK 便宜。
