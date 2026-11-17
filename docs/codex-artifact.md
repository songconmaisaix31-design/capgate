# Codex 文本修改产物

`codex-dev` 使用 `inputs.files` 中的相对路径和原始文本。模型返回有修改的完整文件文本及理由。网关生成统一 diff，`codex_change` 产物保存 `diff` 和 `reason`。原文件由调用方提供，Codex 的原生文件、命令和网络工具保持关闭。

HTTP 请求的 `inputs.files` 接受 JSON 对象。命令行的 `--input files=…` 接受同一对象的 JSON 字符串。从 capgate 工作目录执行下面的命令，将 `$publicRepo` 设为公开统计项目的检出目录。[公开统计项目](https://github.com/songconmaisaix31-design/astrocyte-s3-public-stats)

```powershell
$source = git -C $publicRepo show 'dbce190be32f52eb93a6fd3909fe4d05213c9ba7:stats.go' | Out-String
$source = $source.Replace("`r`n", "`n")
$files = @{ 'stats.go' = $source } | ConvertTo-Json -Compress
python -m capgate solve "为 Go 统计包添加 Median 函数" --hint codex-dev --input "files=$files"
```

路径使用 `/` 分隔，只含英文字母、数字、点、下划线和连字符。文件采用 UTF-8 与 LF 换行，非空文件以 LF 结尾。重复替换、未知路径和没有实际修改的替换会被拒绝。已有文件内容可以替换为空文本。补丁应用前可用 `git apply --check` 核对，并使用与任务原文换行一致的检出。
