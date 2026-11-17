# agent-autoloop 与 capgate

agent-autoloop 接收企业微信事件、安排执行和发起重跑。`command.mjs` 把每轮可信规则关联的事件提交给 capgate，并等到任务结束再返回结果。兼容 agent-autoloop `5eb4be89077461aa6a355757eed834667efe6749` 的事件和 `command` 接口。

## 配置中台及接单程序

1. 安装 Node.js 24 或更新版本。运行 `capgate serve`，保持回环监听。将 `CAPGATE_BRIDGE_URL` 设为 capgate 的本机地址，例如 `http://127.0.0.1:8787`。由运行 agent-autoloop 接单程序的环境提供该变量。
2. 把下面的配置合并到 agent-autoloop 的 `rules.json`。将 `本机工作器` 换成 `config.json` 的 `worker.name`，两个脚本位置和 `cwd` 使用实际绝对路径。`fit-env` 可用于只读检查。正式接收任务时，设置允许该入口使用的环境和标签。
3. 运行 `agent-autoloop check`，启动或重启 agent-autoloop。执行 `agent-autoloop key new source capgate-operator` 和 `agent-autoloop key new source capgate-wecom` 分别创建来源密钥，存入各自进程的环境变量。`CAPGATE_AUTOLOOP_URL` 指向本机中台，例如 `http://127.0.0.1:3350`。

```json
{
  "sources": {
    "capgate-operator": { "name": "capgate 操作者" },
    "capgate-wecom": { "name": "企业微信" }
  },
  "agents": {
    "capgate-rerun": {
      "name": "capgate 重跑", "worker": "本机工作器", "use": "command",
      "command": ["node", "C:/capgate/integrations/agent-autoloop/command.mjs", "rerun"]
    },
    "capgate-wecom": {
      "name": "capgate 企业微信", "worker": "本机工作器", "use": "command",
      "command": ["node", "C:/capgate/integrations/agent-autoloop/command.mjs", "wecom", "fit-env", "环境"]
    }
  },
  "rules": [
    {
      "name": "capgate 重跑", "when": { "source": "capgate-operator", "types": ["capgate.rerun"] },
      "to": "capgate-rerun", "cwd": "C:/capgate", "parallel": 1, "tell": "never",
      "prompt": "按保存的规范重跑 capgate 任务"
    },
    {
      "name": "capgate 企业微信", "when": { "source": "capgate-wecom", "types": ["capgate.wecom.message"] },
      "to": "capgate-wecom", "cwd": "C:/capgate", "parallel": 2, "tell": "never",
      "prompt": "把企业微信资料交给 capgate"
    }
  ]
}
```

## 重跑

向调用方提供 `CAPGATE_AUTOLOOP_OPERATOR_KEY` 及 `CAPGATE_AUTOLOOP_URL`，然后运行以下命令。命令使用原任务的目标、参数、预算和路由要求创建新任务，并在新任务的输入中记录来源。企微来源使用独立密钥，只能触发上面声明的企微规则。

```text
node integrations/agent-autoloop/rerun.mjs <原任务编号> [重跑请求编号]
```

命令输出事件编号和请求编号。查询超时后，以同一请求编号再次调用可查询原事件。重新发起一次执行时使用新的请求编号。capgate 的 `Idempotency-Key` 会给同一事件返回同一任务编号。

使用看板上的重跑按钮时，在 `capgate serve` 的进程环境中也提供 `CAPGATE_AUTOLOOP_URL` 和 `CAPGATE_AUTOLOOP_OPERATOR_KEY`。看板提交结束任务的编号，capgate 调用 agent-autoloop 的事件接口并返回事件编号。由 agent-autoloop 的规则和工作器发起新任务，来源密钥留在服务端。对相同任务重复提交同一个 `Idempotency-Key` 会返回同一事件。

## 企业微信消息

使用企业微信官方智能机器人长连接。进入本目录运行 `npm ci`，在一个本机进程中设置以下环境变量，再运行 `node wecom.mjs`。

| 变量 | 用途 |
| --- | --- |
| `CAPGATE_WECOM_ENABLED` | 设为 `1` 时启动监听 |
| `CAPGATE_WECOM_BOT_ID` / `CAPGATE_WECOM_BOT_SECRET` | 官方智能机器人的 ID 和密钥 |
| `CAPGATE_AUTOLOOP_WECOM_KEY` | `capgate-wecom` 来源密钥 |
| `CAPGATE_AUTOLOOP_URL` | 本机 agent-autoloop 地址 |
| `CAPGATE_WECOM_SENDERS` | 逗号分隔的企微用户 ID |
| `CAPGATE_WECOM_GROUPS` | 可选，逗号分隔的群聊 ID。群聊同时检查发送者 |
| `CAPGATE_WECOM_ATTACHMENT_DIR` | 接单程序和 capgate 均可访问的绝对目录，建议放在 capgate 的 `var` 下 |

消息内容、发送者、会话和附件引用保存在 capgate 任务的 `inputs.request` 中。文本也是 `inputs.question`，供问答环境使用。附件由官方 SDK 下载解密，文件保留在本机。企业微信消息 ID 用于 agent-autoloop 持久去重，重复执行同一事件会复用 capgate 任务。终态回复包含任务编号和执行状态，有结论、失败原因或产物时也会显示。回执最多 500 字，完整记录可在本机看板查询。官方 SDK 管理重连。断线期间未收到确认的消息需要核查运行记录。停止入口使用 `Ctrl+C`。
