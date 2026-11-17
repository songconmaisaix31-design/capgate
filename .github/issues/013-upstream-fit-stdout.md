---
title: "[上游] FIT 的 MCP server 把日志写到 stdout，污染协议流"
labels: [upstream, p2]
---

## 现状

`fit serve mcp` 启动时会往 **stdout** 打两行非 JSON-RPC 内容：

```
[fit] MCP server on stdio · root=D:\env · 139 endpoints
[fit] ready 2026-11-17T17:11:08.849Z
```

MCP stdio 传输要求 stdout 上**只有**协议消息。当前 Python 客户端容忍了这两行
（解析失败会跳过），但换一个严格的客户端就会直接失败。

## 期望

- FIT 侧把这些日志改写到 stderr（`console.error` 或 `process.stderr.write`）。
- 或提供 `--quiet` 开关，供 MCP 宿主使用。

## 验收

- [ ] `node bin/fit.mjs serve mcp` 的 stdout 只含 JSON-RPC 行
- [ ] `capgate probe mcp-fit` 仍能列出 6 个工具

## 备注

同类问题也值得在网关侧加一道防御：启动时读到的第一行不是 JSON-RPC 就记一条
`mcp.stdout.noise` 事件，把"能跑"和"干净"区分开。
