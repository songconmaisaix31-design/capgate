# BCA 与 FIT 上游验证

## BCA 从源码构建

在 BCA `ff807f434f5132b0bc044bb0460b07de5b091500` 的干净工作树上使用 Node.js `24.16.0`、npm `11.13.0` 安装锁定的依赖并完成构建。Windows 的默认 `cmd.exe` 无法执行 `@foundry-rs/forge` 的 POSIX 安装脚本。本次使用 Git for Windows 提供的 Bash 执行 npm 脚本。

在 BCA 源码目录运行以下命令。若 Git for Windows 安装在其他位置，使用本机 `bash.exe` 的实际路径。

```powershell
npm --script-shell 'C:/Program Files/Git/bin/bash.exe' ci
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm --script-shell 'C:/Program Files/Git/bin/bash.exe' run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Test-Path dist/mcp/main.js
```

capgate 的 BCA 默认源码目录为 `C:/Users/DW/repos/blockchain-anything`。使用其他源码目录时，在启动 capgate 的环境中设置 `CAPGATE_BCA_HOME`。默认源码目录完成相同的依赖安装和构建后，以下命令分别返回 125 个注册工具和 125 条能力。将环境变量设为干净工作树的路径也得到相同结果。

```powershell
python -m capgate probe mcp-bca
python -m capgate call mcp-bca capability_list
```

## FIT 标准输出

FIT 上游提交 `5e4ccd3c32c1d63274429e1c426e10722b6147e6` 把启动和就绪日志写入 stderr。本次验证使用 `2713777b32e4442b7f982f5decb508a04275ee17`。直接启动标准输入输出服务并发送 `initialize` 与 `tools/list` 后，stdout 返回两条 JSON-RPC 2.0 消息。stderr 返回启动和就绪两行日志。capgate 的默认路径 `D:/repos/fit-protocol` 探测到 6 个工具。

```powershell
python -m capgate probe mcp-fit --json
```

如需在 FIT 源码目录直接查看两个输出通道，可在 PowerShell 中运行以下命令。`initialize` 和 `tools/list` 的响应均应可由 `ConvertFrom-Json` 解析。

```powershell
@(
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"capgate-stdio-check","version":"0.1.0"}}}'
    '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
) | node bin/fit.mjs serve mcp | ConvertFrom-Json
```
