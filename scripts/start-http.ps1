# awam-knowledge HTTP 服务启动脚本（供 AFM 搜索面板调用，ADR-0016）
#
# 机密（KM_SERVICE_TOKEN / KM_SECRET_AGENT_TOKEN 等）一律放在仓库外：
#   默认 %USERPROFILE%\.awam-knowledge-http\env.ps1（可用 -EnvFile 覆盖）
# 该文件只负责装载机密并前台运行服务；常驻/开机自启由外部包裹（如计划任务）负责。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-http.ps1
#   powershell ... -File scripts\start-http.ps1 -EnvFile D:\elsewhere\env.ps1
param(
  [string]$EnvFile = (Join-Path $env:USERPROFILE '.awam-knowledge-http\env.ps1')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $EnvFile)) {
  Write-Error "找不到机密环境文件：$EnvFile（先创建它，参考 README「HTTP 服务」一节）"
  exit 1
}

. $EnvFile

if (-not $env:KM_SERVICE_TOKEN) {
  Write-Error 'env.ps1 缺少 KM_SERVICE_TOKEN'
  exit 1
}

Write-Host "[start-http] index root: $env:AWAM_INDEX_ROOT"
Write-Host '[start-http] launching http-server.js (127.0.0.1:18790 by default)...'

node (Join-Path $PSScriptRoot '..\code\http-server.js')
exit $LASTEXITCODE
