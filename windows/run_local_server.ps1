# 在這台電腦上跑策略服務（本機測試 EA 用），不需要 Coolify
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location -Path $repo
$Host.UI.RawUI.WindowTitle = 'cfd-live 本機測試服務（關掉這個視窗就停止）'

function Test-Py([string[]]$cmd) {
    try {
        $exe = $cmd[0]; $a = @(); if ($cmd.Count -gt 1) { $a = $cmd[1..($cmd.Count-1)] }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
        $v = & $exe @a -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 10) { return $v }
    } catch {}
    return $null
}
$cands = @(,@('py', '-3.12')) + @(,@('py', '-3'))
foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe")) {
    Get-ChildItem -Path $p -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $cands += ,@($_.FullName) }
}
$py = $null
foreach ($c in $cands) { if (Test-Py $c) { $py = $c; break } }
if (-not $py) { Write-Host '找不到 Python 3.10 以上。先執行之前的 mt5probe 腳本（會自動安裝 Python），再回來執行這個。' -ForegroundColor Red; exit 1 }
$pyExe = $py[0]; $pyArgs = @(); if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count-1)] }

Write-Host '[1/2] 安裝服務需要的套件 ...' -ForegroundColor Cyan
& $pyExe @pyArgs -m pip install --quiet --upgrade fastapi uvicorn pandas numpy
if ($LASTEXITCODE -ne 0) { Write-Host '套件安裝失敗，截圖給 Claude。' -ForegroundColor Red; exit 1 }

# MT5 的 WebRequest 只穩定支援 80／443 埠，優先用 80
$port = 80
if (Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue) { $port = 8080 }
$token = 'localtest-123456'
$env:DATA_DIR = Join-Path $repo 'data_local'
$env:EA_TOKEN = $token
$env:LIVE_PASSWORD = ''
$env:PORT = "$port"
$env:INSTANCES_FILE = Join-Path $repo 'instances.json'
$url = if ($port -eq 80) { 'http://127.0.0.1' } else { "http://127.0.0.1:$port" }

Write-Host ''
Write-Host '[2/2] 啟動本機服務' -ForegroundColor Cyan
Write-Host '--------------------------------------------' -ForegroundColor DarkGray
Write-Host ("儀表板：{0}" -f $url) -ForegroundColor Yellow
Write-Host 'MT5 裡要設定：'
Write-Host ("  工具 → 選項 → EA交易 → 允許 WebRequest 的網址加入：{0}" -f $url)
Write-Host ("  TzujenBridge 參數  ServerURL = {0}" -f $url)
Write-Host ("                    Token     = {0}" -f $token)
if ($port -ne 80) { Write-Host '  注意：80 埠被其他程式占用，改用 8080；若 EA 顯示連不上，請截圖給 Claude。' -ForegroundColor Yellow }
Write-Host '關掉這個視窗服務就會停止（MT5 上的持倉與停損仍在券商端）。'
Write-Host '--------------------------------------------' -ForegroundColor DarkGray
Start-Job -ScriptBlock { param($u) Start-Sleep 4; Start-Process $u } -ArgumentList $url | Out-Null
& $pyExe @pyArgs -m app.main
