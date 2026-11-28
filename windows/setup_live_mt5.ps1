# 在這台電腦再開一個「實盤用」的 MT5（複製現有的 Pepperstone MT5 到 C:\MT5-Live），並裝好 TzujenBridge EA
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
$Host.UI.RawUI.WindowTitle = '建立實盤 MT5'
function Pause-Exit($code) { Write-Host ''; Read-Host '按 Enter 關閉視窗' | Out-Null; exit $code }
function Read-Origin($dir) {
    $bytes = [IO.File]::ReadAllBytes((Join-Path $dir 'origin.txt'))
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { $s = [Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2) }
    else { $s = [Text.Encoding]::Default.GetString($bytes) }
    return $s.Trim([char]0, ' ', "`r", "`n")
}

$dst = 'C:\MT5-Live'
$root = Join-Path $env:APPDATA 'MetaQuotes\Terminal'

# 1) 找到現有的 Pepperstone MT5 安裝位置
$src = $null
foreach ($d in (Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'origin.txt') })) {
    $p = Read-Origin $d.FullName
    if ($p -ne $dst -and $p -match 'Pepperstone' -and (Test-Path (Join-Path $p 'terminal64.exe'))) { $src = $p; break }
}
if (-not $src) {
    foreach ($p in @('C:\Program Files\Pepperstone MetaTrader 5', 'C:\Program Files\MetaTrader 5')) { if (Test-Path (Join-Path $p 'terminal64.exe')) { $src = $p; break } }
}
if (-not $src) { Write-Host '找不到 Pepperstone MT5 的安裝資料夾。請先確認模擬倉那個 MT5 能正常開啟。' -ForegroundColor Red; Pause-Exit 1 }
Write-Host "現有的 MT5：$src" -ForegroundColor Yellow

# 2) 複製一份到 C:\MT5-Live（已經有就跳過）
if (-not (Test-Path (Join-Path $dst 'terminal64.exe'))) {
    Write-Host "複製到 $dst …"
    robocopy $src $dst /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if (-not (Test-Path (Join-Path $dst 'terminal64.exe'))) { Write-Host '複製失敗' -ForegroundColor Red; Pause-Exit 1 }
    Write-Host '  完成' -ForegroundColor Green
} else { Write-Host "$dst 已經存在，略過複製" }

# 3) 桌面捷徑
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'MT5 實盤.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk); $sc.TargetPath = Join-Path $dst 'terminal64.exe'; $sc.WorkingDirectory = $dst; $sc.Save()
Write-Host "桌面捷徑：$lnk" -ForegroundColor Green

# 4) 開一次，讓 MT5 建立自己的資料夾，再把 EA 裝進去並編譯
$data = $null
foreach ($d in (Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'origin.txt') })) {
    if ((Read-Origin $d.FullName) -eq $dst) { $data = $d.FullName }
}
if (-not $data) {
    Write-Host '第一次開啟實盤 MT5（等它開好再繼續）…'
    Start-Process -FilePath (Join-Path $dst 'terminal64.exe')
    for ($i = 0; $i -lt 60 -and -not $data; $i++) {
        Start-Sleep -Seconds 2
        foreach ($d in (Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'origin.txt') })) {
            if ((Read-Origin $d.FullName) -eq $dst) { $data = $d.FullName }
        }
    }
}
if (-not $data) { Write-Host '等不到 MT5 建立資料夾。請手動開「MT5 實盤」捷徑一次，再執行 install_ea.cmd。' -ForegroundColor Red; Pause-Exit 1 }

$ea = Join-Path $PSScriptRoot 'TzujenBridge.mq5'
if (-not (Test-Path $ea)) { $ea = Join-Path (Split-Path $PSScriptRoot -Parent) 'ea\TzujenBridge.mq5' }
$exp = Join-Path $data 'MQL5\Experts'
New-Item -ItemType Directory -Force -Path $exp | Out-Null
Copy-Item $ea (Join-Path $exp 'TzujenBridge.mq5') -Force
$log = Join-Path $env:TEMP 'TzujenBridge_live_compile.log'
Start-Process -FilePath (Join-Path $dst 'metaeditor64.exe') -ArgumentList "/compile:`"$(Join-Path $exp 'TzujenBridge.mq5')`"", "/log:`"$log`"" -Wait
if (Test-Path (Join-Path $exp 'TzujenBridge.ex5')) { Write-Host 'EA 已裝進實盤 MT5 並編譯完成' -ForegroundColor Green }
else { Write-Host 'EA 編譯失敗，請截圖給 Claude' -ForegroundColor Red }

Write-Host ''
Write-Host '接下來在「MT5 實盤」裡做：' -ForegroundColor Cyan
Write-Host '  1. 檔案 → 登入交易帳戶：填實盤帳號、密碼，伺服器選 Pepperstone 的實盤伺服器（Live）'
Write-Host '  2. 工具 → 選項 → EA交易：勾「允許演算法交易」、「允許 WebRequest 的網址」，加入實盤服務網址（例如 https://cfd-real.tzujenlab.com）'
Write-Host '  3. 開一張任意圖表，把 TzujenBridge 拖上去，參數：'
Write-Host '       ServerURL = 實盤服務網址　Token = 實盤服務的 EA_TOKEN　AllowLive = true'
Write-Host '  4. 上方「演算法交易」按鈕要是綠色'
Write-Host '模擬倉那個 MT5 照常開著，兩個可以同時跑。' -ForegroundColor Yellow
Pause-Exit 0
