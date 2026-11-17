# 微信任务入口调研：从"文件传输助手"到 Agent 响应

> 调研日期：2026-11-18 ｜ 范围：Windows 桌面端为主
> 方法：读各项目 README / 官方文档 / 产品说明，**未在本机实测**。标注"项目自述"的数字都来自对方文档。

## TL;DR

1. "在微信里发一条 → Agent 收到 → 执行 → 回到微信"这个状态，**今天有现成实现**，但只在
   **企业微信智能机器人**这条官方通道上是干净的。
2. 个人微信侧同样能做，技术上有多个可收发、可只读的现成品，但 2026-01 起被微信官方点名治理，
   属"能跑但随时可能没"的灰色手段，只适合实验。
3. 对 capgate 而言，真正的缺口不是缺 MCP，而是**缺一种非 MCP 的入口适配器**：
   企微 aibot 是 WebSocket 长连接 + 流式回复，套不进现有 `kind: mcp` 的能力模型。

## 一、先划红线（决定选型的那条线）

微信安全中心 2026-01-29 的专项治理公告，把两类行为并列为违规：

- **违规获取用户敏感数据**：通过录音、录屏、截图、辅助服务读取、**破解数据库**等方式获取聊天记录、联系人信息。
- **外挂类**：利用**模拟按键、自动化脚本**操控微信，实现"**自动回复**""**管理聊天记录**""批量加好友"等。

后果是阶梯处罚：功能限制 → 设备封禁 → 永久封号。

这不是纸面规则。最流行的开源聊天记录工具 [chatlog](https://github.com/sjzar/chatlog) 在 2025-10
收到微信官方函件后，把**全部代码与提交历史**删除，仓库只剩一份移除说明。同类项目因此都改成了
小仓库、个人维护、随时准备消失的状态。

推论：**个人微信侧的方案（读库、读窗口、模拟键鼠）只能当实验通道，不能当产品主干。**

## 二、四类方案

| 方案 | 能读记录 | 能"发完就响应" | 合规性 | 适合 |
|---|---|---|---|---|
| **A 企业微信智能机器人** | 会话内消息（含回调体） | ✅ 官方回调 + 流式回复 | 官方支持 | 主干 |
| **B 个人微信 · 本机常驻助手** | 仅运行期收到的消息 | ✅ 消息到达即处理 | ❌ 违反协议 | 实验 / 过渡 |
| **C 个人微信 · 只读归档** | ✅ 完整本地库 | ❌ 只能读 | ❌ "破解数据库" | 整理历史记录 |
| **D 微信客服 / 公众号** | 客服会话 | ✅ 需公网回调 | 官方支持 | 对外客服 |

## 三、方案细节

### A. 企业微信智能机器人（推荐主干）

官方在"智能机器人"里给了 AI 场景的入口：**WebSocket 长连接模式**，不需要公网 IP、不需要域名备案，
群里 @机器人 或单聊发消息即回调；回复走 `aibot_respond_msg` 的**流式**通道——同一 `stream.id`
首推创建、再推刷新，`finish=true` 结束。

关键限制（官方文档）：

- 首次推送起 **10 分钟内必须 finish**，`content` ≤ 20480 字节。
- 同一机器人**同时只能一条长连接**，新连接订阅会踢掉旧连接 → 高可用得主备切换，不能多连。
- 心跳建议 30 秒。

现成的对接件：

| 项目 | 形态 | 说明 |
|---|---|---|
| [dsh-wecom-plugin](https://github.com/zhengmz/dsh-wecom-plugin) | DSH 插件（Node） | 企微 ↔ DSH 双向：一条企微消息 → 一个持久 DSH agent 会话，回复流式回推；`/cd` 切工作目录、`/reset`、`/status`；**权限审批能在企微里回"同意 3F9A"直接批**；入站图片/文件/语音、出站图片都通。作者自述：43 项协议测试 + 真 LLM 端到端通过，基准 DSH `0.1.5-rc.2`，状态是"可行性验证 demo" |
| [actflare](https://pypi.org/project/actflare/) | Python 服务 | 企微回调 + Claude Agent SDK，AES 解密验签、SQLite 队列、5 分钟会话窗口、记忆与反馈评分 |
| [@wwlocal/aibot-connector](https://www.npmjs.com/package/@wwlocal/aibot-connector) | Node 连接器 | OpenAI / DeepSeek / Dify / webhook 多适配，配置化接后端模型 |

**体验等价于"文件传输助手"**：把入口换成"和机器人的单聊"或"一个只有自己的企微群"，发一段话就开工。
区别是这个不会被封号。

成本：需要一个企业微信（可自建一个团队），并在管理后台创建智能机器人拿 `bot_id` + `secret`。

### B. 个人微信 · 本机常驻助手（能做到，但灰）

| 项目 | 形态 | 能力 | 限制 |
|---|---|---|---|
| [LingYu 灵语](https://github.com/CheserEri/LingYu) | Windows exe，MCP + 桌面 GUI | `get_chat_history` / `get_recent_messages`（游标增量）/ `send_message` / `search_contact` / `set_monitored_chats`；`--gui` 模式**消息到达即处理，不需要 Agent 轮询**；仓库内自带 DSH 插件包 | 项目自述：`get_chat_history` 返回的是**运行期间被动收到的消息**，不是完整历史；微信窗口必须可见，最小化/遮挡即失败；底层 UI 自动化随微信改版失效 |
| wechat-skill / Wechat Mcp | Windows MCP | pyautogui 截图 + 键鼠，能发 | 读不稳，几乎只能发 |
| wcferry / itchat / wxauto 原版 | Python 库 | — | **基本已死**：wcferry 只到微信 3.9；wxauto 在 4.x 上 UIA 只剩空壳（Qt 自绘未开无障碍门） |

补充一点实测信息：LingYu README 里写 `dsh plugin --profile web add dsh-lingyu`，但
`registry.npmjs.org/dsh-lingyu` 返回 **404**，说明插件包没发到 npm——要用得按源码/`file:` 安装。

### C. 个人微信 · 只读归档（专门整理聊天记录）

只读不构成"自动回复"，但"破解数据库"这一档仍被公告点名。

| 项目 | 形态 | 关键点 |
|---|---|---|
| [im-mcp](https://github.com/stkoecn/im-mcp) | Python stdio MCP | 项目自述实测微信 **4.1.15.13（Windows 最新版）**；29 个只读工具：全文检索、按日期/时段拉取、增量游标、群成员、图片解密、合并转发展开、群日报聚合；本地 FTS5 索引，毫秒级；初始化需**管理员终端**扫 `Weixin.exe` 内存取密钥，明文库落在 `~/.wechat_mcp/` |
| [@powercess/wxchat-mcp](https://www.npmjs.com/package/@powercess/wxchat-mcp) | Node stdio MCP | `extract_key` / `decrypt` / `get_messages` / `search` / `stats`；还能读朋友圈、转账红包、撤回消息；富媒体（silk 语音、图片 .dat）自动解码 |
| [wxq](https://github.com/vpcoderli/wxq) | Python CLI + MCP | 跨平台，自动扫进程内存取密钥，11 个子命令含 `export` |
| 微信助手 MCP（wxarchive） | 商业软件 + MCP | 只读归档查询（列会话 / 搜消息 / 读时段 / 取上下文）；产品说明**以 Apple Silicon Mac 为准** → Windows 不可用 |

共同代价：要提权读微信进程内存；会把解密后的明文库磁盘上落一份，用完必须清
（im-mcp 自带 `im-clean`）。

**"文件传输助手"在这类工具里没有特殊性**——它就是一个自己发自己的普通会话，能列会话就能读它。
但它不是联系人，机器人无法"回复"它，所以"响应"必须由 B 类（本机自动化）来完成。

### D. 微信客服 / 公众号

- 微信客服 API：可收发，需要**企业认证 + 公网回调域名**，且有客服账号体系。
- 公众号客服消息：认证服务号，用户 48 小时内可回复。

只在做对外客服时值得考虑；自己给自己派活用不上。

## 四、对 capgate 的接入点

现有能力池是 MCP 形状（`capabilities/*.yaml`，`kind: mcp` + `versions[].transport/command/args/tools`），
所以 C、B 都能"登记成能力"，A 不行（WebSocket 长连接 + 流式语义，不是 MCP）。

按现有 schema，只读归档可以直接落成一条能力，并用 `tools` 白名单收窄上下文：

```yaml
id: mcp-wechat-archive
kind: mcp
title: 微信本地归档只读查询（会话 / 消息 / 时段）
versions:
  - version: "1.x"
    transport: stdio
    command: uvx
    args:
      - "--from"
      - "git+https://github.com/stkoecn/im-mcp.git[all]"
      - "wechat-mcp"
    env:
      PYTHONIOENCODING: "utf-8"
      PYTHONUTF8: "1"
    tools:
      - list_wechat_sessions
      - get_session_history
      - get_messages_by_range
      - search_wechat_messages
    startup_timeout: 120
```

同理可登记 LingYu（`kind: mcp`，`command` 指向 `lingyu.exe`），但要注意：

- `send_message` 是**有副作用**的工具，应按现有的确认策略走，不该和只读工具放同一档；
- `set_monitored_chats` 建议留在 env 层配置，**不暴露给模型**——否则模型可以自己扩大监听范围。

A 方案需要的是另一种东西：**入口适配器**（收消息 → 建任务；任务结束 → 回消息），与
`AgentAdapter`（`capgate/agents/`）是同一类扩展点但不是一回事——一个在任务输入端，一个在执行端。
两种做法：把 dsh-wecom-plugin 那种桥挂在网关外（capgate 只暴露一个 HTTP 入口），
或在网关内新增 `kind: im-bot`。前者改动小、可先验证；后者才谈得上"能力池统一"。

## 五、选型建议

| 目标 | 选 |
|---|---|
| 只想把聊天记录整理出来 | **C**（im-mcp），配定时任务扫增量 |
| 一定要"在微信里发一句就开工" | **B**（LingYu），接受封号风险，用小号 |
| 长期稳定、还要给别人用 | **A**（企微机器人），外挂 dsh-wecom-plugin 或自写桥 |
| 过渡 | 文件传输助手 + B 先跑通手感，主干迁 A |

建议的落地顺序：

1. 先用 C 把"整理聊天记录"跑通（不碰发送，风险最低，立刻有产出）。
2. 再用 A 打通"发消息 → 建任务 → 回结论"，把入口做正经。
3. B 只在需要"就在微信里、不想装企微"的场景做实验，默认关闭。

## 六、未验证项

- 企微智能机器人对**未认证企业**的功能限制（能否创建机器人、能发多少条）未查实。
- dsh-wecom-plugin 与本仓库当前 DSH 版本（高于其基准 `0.1.5-rc.2`）是否兼容，未实测。
- im-mcp / LingYu 在本机的真实成功率、取密钥是否稳定，未实测。
- LingYu 的 DSH 插件包是否只在仓库内分发，未确认。

## 参考

- 微信安全中心专项治理公告（2026-01-29）：[快科技](https://m.mydrivers.com/newsview/1101318.html)、[PChome](https://article.pchome.net/info/10402.html)
- 企业微信 · 智能机器人接收消息：[官方文档](https://developer.work.weixin.qq.com/document/path/100719)
- [dsh-wecom-plugin](https://github.com/zhengmz/dsh-wecom-plugin) ｜ [actflare](https://pypi.org/project/actflare/) ｜ [@wwlocal/aibot-connector](https://www.npmjs.com/package/@wwlocal/aibot-connector)
- [LingYu](https://github.com/CheserEri/LingYu) ｜ [im-mcp](https://github.com/stkoecn/im-mcp) ｜ [@powercess/wxchat-mcp](https://www.npmjs.com/package/@powercess/wxchat-mcp) ｜ [wxq](https://github.com/vpcoderli/wxq)
- chatlog 移除说明：[sjzar/chatlog](https://github.com/sjzar/chatlog)
