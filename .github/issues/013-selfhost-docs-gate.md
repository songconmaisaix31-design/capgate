---
title: "selfhost 还没把 README 动效与文档校验纳入门禁"
labels: [enhancement, p2]
---

## 现状

`tools/make-demo.mjs` 生成演示动效，双语 README 的覆盖率检查靠外部脚本，两者都只在
CI 或手工时跑。selfhost 的三个任务只覆盖 `lint / test / triage`。

也就是说：代码质量有门禁，**文档质量没有**。README 里的链接失效、中英某一节缺失、
`demo.svg` 过期，都要靠人发现。

## 期望

- `mcp-repo` 增加 `run_readme_check` 工具：校验双语覆盖、相对链接、图片存在、`demo.svg` 是否最新。
- 新增 `repo-docs` env 与 `T04-docs` 任务卡，纳入 `python -m selfhost.loop` 的默认集合。
- 失败信息要具体到"哪一节缺英文""哪个链接 404"，不是一句"README 有问题"。

## 验收

- [ ] `python -m selfhost.loop --only T04` 能定位到具体小节与具体链接
- [ ] 手动改坏一处链接后，该任务返回非 0 并说明改哪里
- [ ] `demo.svg` 过期能被检出（`node tools/make-demo.mjs --check`）

## 备注

双语检查脚本目前是 skill 目录里的第三方脚本。要 vendored 进仓库的话，
先确认它的许可并在文件头注明来源，不要直接复制粘贴当自己的。
