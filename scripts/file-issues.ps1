#Requires -Version 7.0
<#
.SYNOPSIS
    把 .github/issues/*.md 批量建成 GitHub issue。

.DESCRIPTION
    每个文件的 YAML front-matter 提供 title 与 labels，正文作为 issue body。
    先 -DryRun 看一眼，再真建。

.EXAMPLE
    .\scripts\file-issues.ps1 -DryRun
    .\scripts\file-issues.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$issuesDir = Join-Path $RepoRoot '.github/issues'

if (-not (Test-Path $issuesDir)) { throw "找不到 issue 目录：$issuesDir" }

$files = Get-ChildItem $issuesDir -Filter '*.md' |
    Where-Object { $_.Name -ne 'README.md' } |
    Sort-Object Name

if (-not $files) { throw "没有 issue 文件：$issuesDir" }

function Read-IssueDraft {
    param([string]$Path)

    $lines = Get-Content $Path -Encoding utf8
    if ($lines.Count -lt 3 -or $lines[0].Trim() -ne '---') {
        throw "缺少 front-matter：$Path"
    }
    $end = -1
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq '---') { $end = $i; break }
    }
    if ($end -lt 0) { throw "front-matter 未闭合：$Path" }

    $meta = @{ Labels = @() }
    for ($i = 1; $i -lt $end; $i++) {
        $line = $lines[$i]
        if ($line -match '^\s*title:\s*(.+?)\s*$') {
            $meta.Title = $Matches[1].Trim('"', "'")
        }
        elseif ($line -match '^\s*labels:\s*\[(.*?)\]\s*$') {
            $meta.Labels = $Matches[1].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        }
    }
    if (-not $meta.Title) { throw "front-matter 缺 title：$Path" }

    $body = if ($end + 1 -le $lines.Count - 1) {
        ($lines[($end + 1)..($lines.Count - 1)] -join "`n").Trim()
    }
    else { '' }

    [pscustomobject]@{ Path = $Path; Title = $meta.Title; Labels = @($meta.Labels); Body = $body }
}

$drafts = $files | ForEach-Object { Read-IssueDraft -Path $_.FullName }

if ($DryRun) {
    foreach ($d in $drafts) {
        Write-Host ("[dry] {0} :: {1} :: {2}" -f (Split-Path -Leaf $d.Path), $d.Title, ($d.Labels -join ','))
    }
    Write-Host ("共 {0} 个，未创建。" -f $drafts.Count)
    return
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw '未安装 GitHub CLI（gh）' }

gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'gh 未登录：先跑 gh auth login' }

$remote = git -C $RepoRoot remote get-url origin 2>$null
if (-not $remote) { throw '仓库没有 origin：先 git remote add origin <url>' }

$created = 0
foreach ($d in $drafts) {
    foreach ($label in $d.Labels) {
        gh label create $label --force 2>&1 | Out-Null
    }
    $tmp = New-TemporaryFile
    try {
        Set-Content -Path $tmp -Value $d.Body -Encoding utf8
        $ghArgs = @('issue', 'create', '--title', $d.Title, '--body-file', $tmp.FullName)
        foreach ($label in $d.Labels) { $ghArgs += @('--label', $label) }
        & gh @ghArgs
        if ($LASTEXITCODE -ne 0) { Write-Warning "创建失败：$($d.Title)"; continue }
        $created++
    }
    finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ("已创建 {0}/{1} 个 issue。" -f $created, $drafts.Count)
