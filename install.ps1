<#
一键部署：装 Python 依赖 → 确认 Claude Code CLI → 注册 MCP server → 环境体检。

    powershell -ExecutionPolicy Bypass -File install.ps1

AutoCAD 完整版和天正建筑 T30 要先自己装好，这个脚本不装它们，只检查。
可重复运行：依赖已装就跳过，MCP 注册会覆盖成当前仓库的位置。

    -McpName <名字>   MCP server 的注册名，默认 autocad
    -SkipClaude       不装也不注册 Claude Code，只装依赖和体检
#>
param(
    [string]$McpName = "autocad",
    [switch]$SkipClaude
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$repo = $PSScriptRoot
$server = Join-Path $repo "mcp\server.py"

function Step($n, $text) { Write-Host "`n[$n/4] $text" -ForegroundColor Cyan }

Step 1 "Python 依赖"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
# 应用商店的占位 python.exe 一运行就弹商店，不算装了
if (-not $python -or $python -like "*\WindowsApps\*") {
    Write-Host "没找到 Python。先装 3.10 以上再重跑本脚本：winget install Python.Python.3.12" -ForegroundColor Red
    exit 1
}
Write-Host "Python: $python"
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $repo "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "pip 安装依赖失败，看上面的报错" -ForegroundColor Red; exit 1 }
Write-Host "依赖已装好"

Step 2 "Claude Code CLI"
if ($SkipClaude) {
    Write-Host "按 -SkipClaude 跳过"
} else {
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if (-not $claude) {
        Write-Host "没找到 claude 命令，用官方安装脚本装（https://claude.ai/install.ps1）"
        Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    }
    if (-not $claude) {
        Write-Host "claude 还是找不到。新开一个终端重跑本脚本；仍不行就手动装：npm install -g @anthropic-ai/claude-code" -ForegroundColor Red
        exit 1
    }
    Write-Host "Claude Code: $(& $claude --version)"
}

Step 3 "注册 MCP server「$McpName」"
if ($SkipClaude) {
    Write-Host "按 -SkipClaude 跳过"
} else {
    $ErrorActionPreference = "Continue"
    & $claude mcp remove --scope user $McpName *> $null
    & $claude mcp add --scope user $McpName -- $python $server
    $added = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($added -ne 0) { Write-Host "注册失败，看上面的报错" -ForegroundColor Red; exit 1 }
    Write-Host "已注册：$python $server"
    Write-Host "数据根目录是你启动 claude 时所在的目录；要固定到某个目录就设环境变量 ACADMCP_ROOT"
}

Step 4 "环境体检"
& $python (Join-Path $repo "mcp\doctor.py") --mcp-name $McpName
$code = $LASTEXITCODE
Write-Host ""
if ($code -eq 0) {
    Write-Host "部署完成。到图纸所在目录里运行 claude，就能让它读图、识别天正图、画图。" -ForegroundColor Green
} else {
    Write-Host "体检有必需项没过，按上面的提示补上后重跑本脚本。" -ForegroundColor Yellow
}
exit $code
