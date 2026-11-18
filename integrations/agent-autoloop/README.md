# agent-autoloop 与 capgate

agent-autoloop 接收飞书事件、安排执行和发起重跑。`command.mjs` 把每轮可信规则关联的事件提交给 capgate，并等到任务结束再返回结果。兼容 agent-autoloop `5eb4be89077461aa6a355757eed834667efe6749` 的事件和 `command` 接口。

## 配置中台及接单程序

1. 安装 Node.js 24 或更新版本。运行 `capgate serve`，保持回环监听。将 `CAPGATE_BRIDGE_URL` 设为 capgate 的本机地址，例如 `http://127.0.0.1:8787`。由运行 agent-autoloop 接单程序的环境提供该变量。
2. 把下面的配置合并到 agent-autoloop 的 `rules.json`。将 `本机工作器` 换成 `config.json` 的 `worker.name`，两个脚本位置和 `cwd` 使用实际绝对路径。`fit-env` 可用于只读检查。正式接收任务时，设置允许该入口使用的环境和标签。
3. 运行 `agent-autoloop check`，启动或重启 agent-autoloop。执行 `agent-autoloop key new source capgate-operator` 和 `agent-autoloop key new source capgate-lark` 分别创建来源密钥，存入各自进程的环境变量。`CAPGATE_AUTOLOOP_URL` 指向本机中台，例如 `http://127.0.0.1:3350`。

```json
{
  "sources": {
    "capgate-operator": { "name": "capgate 操作者" },
    "capgate-lark": { "name": "飞书" }
  },
  "agents": {
    "capgate-rerun": {
      "name": "capgate 重跑", "worker": "本机工作器", "use": "command",
      "command": ["node", "C:/capgate/integrations/agent-autoloop/command.mjs", "rerun"]
    },
    "capgate-lark": {
      "name": "capgate 飞书", "worker": "本机工作器", "use": "command",
      "command": ["node", "C:/capgate/integrations/agent-autoloop/command.mjs", "lark", "fit-env", "环境"]
    }
  },
  "rules": [
    {
      "name": "capgate 重跑", "when": { "source": "capgate-operator", "types": ["capgate.rerun"] },
      "to": "capgate-rerun", "cwd": "C:/capgate", "parallel": 1, "tell": "never",
      "prompt": "按保存的规范重跑 capgate 任务"
    },
    {
      "name": "capgate 飞书", "when": { "source": "capgate-lark", "types": ["capgate.lark.message"] },
      "to": "capgate-lark", "cwd": "C:/capgate", "parallel": 2, "tell": "never",
      "prompt": "把飞书资料交给 capgate"
    }
  ]
}
```

## 重跑

向调用方提供 `CAPGATE_AUTOLOOP_OPERATOR_KEY` 及 `CAPGATE_AUTOLOOP_URL`，然后运行以下命令。命令使用原任务的目标、参数、预算和路由要求创建新任务，并在新任务的输入中记录来源。飞书来源使用独立密钥，只能触发上面声明的飞书规则。

```text
node integrations/agent-autoloop/rerun.mjs <原任务编号> [重跑请求编号]
```

命令输出事件编号和请求编号。查询超时后，以同一请求编号再次调用可查询原事件。重新发起一次执行时使用新的请求编号。capgate 的 `Idempotency-Key` 会给同一事件返回同一任务编号。

使用看板上的重跑按钮时，在 `capgate serve` 的进程环境中也提供 `CAPGATE_AUTOLOOP_URL` 和 `CAPGATE_AUTOLOOP_OPERATOR_KEY`。看板提交结束任务的编号，capgate 调用 agent-autoloop 的事件接口并返回事件编号。由 agent-autoloop 的规则和工作器发起新任务，来源密钥留在服务端。对相同任务重复提交同一个 `Idempotency-Key` 会返回同一事件。

## 飞书单聊

在飞书开放平台为企业自建应用开启机器人能力。将应用可用范围限制为获准的单聊用户。开启应用身份的 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot`。附件需 `im:resource`，远端历史需 `im:message:readonly`。在事件配置中选择长连接，添加 `im.message.receive_v1`，保存并发布应用版本。进入本目录运行 `npm ci`。 [飞书接收消息事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive) · [官方 Node.js SDK](https://github.com/larksuite/node-sdk)

| 变量 | 用途 |
| --- | --- |
| `CAPGATE_LARK_ENABLED` | 设为 `1` 时启动监听 |
| `CAPGATE_LARK_APP_ID` / `CAPGATE_LARK_APP_SECRET` | 企业自建应用的 App ID 和 App Secret，留在入口进程环境 |
| `CAPGATE_AUTOLOOP_LARK_KEY` | `capgate-lark` 来源密钥 |
| `CAPGATE_AUTOLOOP_URL` | 本机 agent-autoloop 地址 |
| `CAPGATE_LARK_SENDERS` | 获准的飞书用户 `ou_` ID，多个用逗号分隔 |
| `CAPGATE_LARK_CHATS` | 获准的机器人单聊 `oc_` ID，多个用逗号分隔 |
| `CAPGATE_LARK_ATTACHMENT_DIR` | 入口、接单程序和 capgate 均可访问的本机绝对目录，建议放在 capgate 的 `var` 下 |

后台要求先建立长连接时，在入口终端仅提供应用 ID 和密钥，运行 `node lark.mjs --bootstrap`。看到“飞书机器人长连接已建立”后，可在平台保存长连接配置；完成后用 `Ctrl+C` 关闭。

采集单聊 ID 时，在发布版本后设置一次性、随机且至少 32 字符的 `CAPGATE_LARK_IDENTIFY_TEXT`，运行 `node lark.mjs --bootstrap --identify`。获准用户本人私聊发送该短语，终端仅输出一次匹配消息的 `sender` 和 `chat` ID。这个模式不派发任务、下载或回复。结束进程，把 ID 配入正式入口的白名单。单个应用只启动一个 `node lark.mjs` 处理进程。

获准的文本、富文本、图片、文件、音频和视频会写入本机 SQLite 收件记录，再确认飞书事件。入口使用官方 SDK 从原消息下载二进制附件，每条消息附件累计最多 25 MiB。capgate 任务的 `inputs.request` 保存消息、发送者、会话、发送时刻和本机文件引用。消息文本也提供为 `inputs.question`，供问答环境使用。确认 agent-autoloop 已接受事件后，机器人回复事件编号。终态回复包含任务编号和结果，有结论、失败原因或产物时也会显示。正常终态回复最多 500 字，完整记录可在本机看板查询。发送成功后收件记录仅保留去重标识，原任务及附件按本机任务数据保存。

去重使用应用 ID 和原消息 ID。agent-autoloop 与 capgate 各自保存事件和任务。附件下载、提交、状态查询或回复发生临时故障时，入口保留收件进度，下次继续处理。已经确认提交的事件使用本机保存的事件编号查询，已生成的回复直接重发。官方 SDK 处理断线与重连。断线期间未落盘的消息可以用飞书单聊历史核对，需要执行时重新发送。停止入口使用 `Ctrl+C`。独立验证自动重连可运行 `node lark.mjs --bootstrap --verify-reconnect`。该命令会关闭测试进程自己建立的 TLS 连接，收到 SDK 的重连事件后退出。

### 整理已接收的消息

本机历史索引默认关闭。启用时，为 `lark.mjs`、agent-autoloop 工作器和运行查询的本机终端设置同一个 `CAPGATE_LARK_HISTORY_DB` 绝对路径，建议放在 capgate 的 `var` 下。入口启动时建立索引，工作器在 capgate 确认任务编号后记录来源、会话、发送者、收件时间和任务编号。原文保留在对应的 capgate 任务中。

查询时使用当前 `CAPGATE_LARK_SENDERS` 和 `CAPGATE_LARK_CHATS` 白名单，显式指定一个获准的单聊和含时区的开始、结束时间。查询范围包含开始时刻，结束时刻作为上界。窗口最多 7 天、250 条记录。默认使用本机索引的收件时间，逐条读取 capgate 任务并核对来源。

```powershell
node integrations/agent-autoloop/history.mjs --kind single --chat <飞书单聊ID> --since 2026-11-18T09:00:00+08:00 --until 2026-11-18T18:00:00+08:00
```

如需核对断线区间，提供应用凭证、设置 `CAPGATE_LARK_REMOTE_HISTORY=1`，并为同一条命令加 `--remote`。它只调用分配给该机器人且在白名单中的单聊，以飞书消息创建时间过滤，只整理白名单发送者的内容。远端历史查询只读，不下载历史附件、不重新派发任务。输出为 JSON，含消息 ID 和时间。摘要使用现有任务结论或消息摘录，待办保留原文明确提出的行动项。整理不调用模型。 [飞书会话历史 API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)
