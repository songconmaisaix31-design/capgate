---
id: T04
title: 文档校验
type: docs
tags: [文档, README, 校验, 双语]
goal: 校验 README 的双语覆盖、相对链接、图片存在与 demo.svg 是否最新
---

这条任务把"文档质量没有门禁"补上：跑 `run_readme_check`，任一问题（某节缺英文、
某链接 404、demo.svg 过期）都会让任务失败，并给出具体到小节与链接的定位。
