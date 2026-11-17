---
title: "selfhost 只能检查不能修：缺受限写入能力"
labels: [enhancement, p3]
---

## 现状

`mcp-repo` 的四个工具全是只读。自举能告诉你"lint 挂了、测试红了、README 过期了"，
但修不了——最后一步仍然是人手动改。

## 期望

- 增加 `write_file(path, content)` 与 `apply_patch(path, diff)`，**默认 dry-run**，
  要显式 `allow_write=true` 才落盘。
- 路径白名单：只允许仓库内的 `capgate/ tests/ docs/ envs/ capabilities/`，
  越界直接拒绝并记事件。
- 每次写入产出 `patch` 产物（统一 diff 格式），进账本，可审可回滚。

## 验收

- [ ] dry-run 时不改任何文件，但产物里有完整 diff
- [ ] 越界路径（例如 `../` 或仓库外绝对路径）被拒绝且不留半截文件
- [ ] 一次 `allow_write` 运行能在决策链里追到"改了哪几个文件、为什么"

## 备注

这是把"网关自动改自己的代码"摆上台面，风险比前 13 条都高。开之前先想清楚两件事：
谁 review、怎么回滚。没有这两条就只是个会自动搞坏仓库的脚本。
