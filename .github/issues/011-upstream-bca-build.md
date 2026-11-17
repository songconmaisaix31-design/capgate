---
title: "[上游] BCA 仓库无法构建：dist 缺失、node_modules 不全"
labels: [upstream, p1]
---

## 现状

`C:\Users\DW\repos\blockchain-anything`（BCA 源码仓库）当前**无法产出 `dist/`**：

- `npm run build` 失败：`tsc` 不在 `node_modules/.bin`，直接跑
  `node node_modules/typescript/bin/tsc` 报大量类型错误。
- `node_modules` 缺运行时依赖：`viem`、`@aptos-labs/ts-sdk` 等缺失，导致
  `node --import tsx src/mcp/main.ts` 在 `src/chains/viem.ts` 处 `ERR_MODULE_NOT_FOUND`。
- 因此网关的 `mcp-bca` 能力目前指向另一份**已构建**的副本
  `C:\Users\DW\orca\blockchain-anything`（有 `dist/mcp/main.js` 且 `viem` 齐全），
  用 `${CAPGATE_BCA_HOME:...}` 切路径。

## 期望

- 源码仓库能 `npm ci && npm run build` 成功，`dist/mcp/main.js` 可用。
- 网关默认路径改回源码仓库，`orca` 副本只作为临时兜底。

## 验收

- [ ] `npm ci && npm run build` 在干净环境通过
- [ ] `node dist/mcp/main.js` 能被 `capgate probe mcp-bca` 列出 125 个工具
- [ ] `CAPGATE_BCA_HOME` 默认值切到源码仓库

## 备注

类型错误看起来是依赖版本漂移（`@mysten/sui` 缺 d.ts、`viem` 完全缺失）。
先跑一次 `npm ci` 再看是不是 lockfile 与 package.json 不一致。
