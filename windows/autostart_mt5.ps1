# 把「模擬倉 MT5」和「實盤 MT5（C:\MT5-Live）」都加進開機自動啟動（啟動資料夾），已經有的就略過
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = 'MT5 開機自動啟動'
function Pause-Exit($code) { Write-Host ''; Read-Host '按 Enter 關閉視窗' | Out-Null; exit $code }

$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell

# 啟動資料夾裡已經指向哪些 terminal64.exe
$have = @{}
foreach ($f in Get-ChildItem -Path $startup -Filter *.lnk -ErrorAction SilentlyContinue) {
    try { $t = $ws.CreateShortcut($f.FullName).TargetPath; if ($t) { $have[$t.ToLower()] = $f.Name } } catch { }
}

$targets = @(
    @{ name = 'MT5 DEMO'; exe = 'C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe' },
    @{ name = 'MT5 LIVE'; exe = 'C:\MT5-Live\terminal64.exe' }
)
foreach ($t in $targets) {
    if (-not (Test-Path $t.exe)) { Write-Host "找不到 $($t.exe)，略過" -ForegroundColor Yellow; continue }
    if ($have.ContainsKey($t.exe.ToLower())) { Write-Host "已經會自動啟動：$($t.exe)（$($have[$t.exe.ToLower()])）" -ForegroundColor Green; continue }
    $lnk = Join-Path $startup "$($t.name).lnk"
    try {
        $sc = $ws.CreateShortcut($lnk); $sc.TargetPath = $t.exe; $sc.WorkingDirectory = Split-Path $t.exe -Parent; $sc.Save()
    } catch { }
    if (Test-Path $lnk) { Write-Host "已加入開機自動啟動：$($t.exe)" -ForegroundColor Green }
    else { Write-Host "加入失敗：$($t.exe)" -ForegroundColor Red }
}

Write-Host ''
Write-Host '說明：' -ForegroundColor Cyan
Write-Host '  - 啟動資料夾是「登入 Windows 後」才執行。VPS 重開機後要能自動登入（模擬倉原本能自動起來，就代表已經設好）'
Write-Host '  - MT5 會記得上次開著的圖表和 EA；關 MT5 時請用「檔案 → 結束」，不要直接關 VPS，圖表和 EA 設定才會存下來'
Write-Host '  - 兩個 MT5 各自上方的「演算法交易」按鈕要是綠色，重開後會維持上次的狀態'
Write-Host "  - 啟動資料夾位置：$startup"
Pause-Exit 0
