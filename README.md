# cfd-live — 指數 CFD 自動下單（MT5）

回測室（us-index-lab）挑出來的策略，在 MT5 上自動執行。
**策略邏輯只有一份**：`app/core/` 是從 us-index-lab 原封不動複製的引擎與策略，
這裡只負責「K 棒收盤 → 呼叫 on_bar() → 翻成 MT5 動作」。

```
Coolify（這個服務）  ⇄  HTTPS  ⇄  MT5 裡的 TzujenBridge EA  →  券商（Pepperstone）
  策略、對帳、儀表板、Telegram       只同步資料、執行動作
```

## 目前跑的策略（instances.json）
| id | 商品 | 策略 | K | 手數 | magic | 狀態 |
|---|---|---|---|---|---|---|
| gold_chan15 | XAUUSD | 外部訊號：gold-scalper lab 15 分K 纏論（窄中樞） | 15 分 | 0.01 | 900015 | 前瞻＋量點差滑價 |
| jp_gy25 | JPN225 | GuYuan_2025（預設參數） | 20 分 | 0.1 | 225025 | 主力 |
| nas_gy25_60 | NAS100 | GuYuan_2025（預設參數） | 60 分 | 0.1 | 100060 | 主力（2022 空頭仍獲利，和日經互補） |
| nas_gy24s | NAS100 | GuYuan_2024_S（WIN 2、MA2 100、EBX 2） | 60 分 | 0.1 | 100024 | 空方（2022 +19%），前瞻測試 |
| jp_gy24 | JPN225 | GuYuan_2024（台指參數） | 60 分 | 0.1 | 225024 | 對照組 |
| nas_arx | NAS100 | AR_crossover（預設參數） | 60 分 | 0.1 | 100011 | 對照組 |
| jp_gy24_w9 | JPN225 | GuYuan_2024（LOSS 2、WIN 9、EBX 2） | 60 分 | 0.1 | 225029 | 停用（和 jp_gy24 相關 0.88） |
| nas_gy25 | NAS100 | GuYuan_2025（預設參數） | 20 分 | 0.1 | 100025 | 停用（20 分滑價加倍 PF 掉到 1.04，改 60 分） |

改策略、手數、新增商品：改 `instances.json` 推上去。magic 不能重複。
停用一支：把 `enabled` 改成 `false`（不要直接刪掉）。系統會把它剩下的持倉平掉、掛單撤掉，之後不再進場；
magic 保留著不要給別支用。直接刪掉的話，它的持倉會變成「未知 magic」沒人管。
同一商品可以同時跑多支（例如上面兩支日經）：帳戶要是 **Hedge（對沖）** 模式，各支用自己的 magic 管理持倉、掛單、成交，
互不干擾（`tests/test_two_instances.py`）。Netting（淨額）帳戶不行，同商品的部位會合併。

## 外部訊號：黃金（gold-scalper）
`gold_chan15`（XAUUSD、0.01 手＝1 盎司、magic 900015）不是這裡的策略：訊號由 gold-scalper **lab** 的 15 分K 纏論引擎
（`chan_profile_900`，窄中樞前瞻測試那支）用幣安 XAUUSDT 算，這裡只照著在 MT5 下單，目的是**量出換到 MT5 之後的真實差異**。

- gold-scalper 每 20 秒、以及部位一有變化，POST「我現在的部位」到 `/ext/gold-scalper`（header `x-token` = `EXT_TOKEN`）。
- 這裡每次 EA 同步就對一次（`app/ext.py`）：對方有新部位 → 市價進場；對方空手或換了一筆 → 市價平倉。
  做過的 key 不會再做，所以重啟、重送、亂序都不會重複下單。
- 進場附**安全停損**＝進場報價 ∓ 對方停損距離 × 2，只防斷線；停損、移動停損都由對方決定（對方平倉 → 這裡平倉）。
- 對方 3 分鐘沒回報 → 不進新倉、不主動平倉、警示一次。訊號收到時已過 5 分鐘 → 不追，記「略過」。
- 儀表板「外部訊號：模擬 vs MT5」：每筆的模擬（幣安）與 MT5 成交、點差、進出場滑價（成交價比下單當下報價差多少）、
  訊號到成交的延遲、券商與幣安的報價差；上面是合計與平均。`/api/ext` 是同樣的資料。
- 0.01 手 = 1 盎司，1 點 = 1 美元，跟 gold-scalper 的點數直接對得起來。
- **模擬帳戶的成交通常比真實帳戶理想**：這裡量得到點差、報價差、延遲和整條流程，量不到真實的滑價。
- 只有外部訊號用的商品（XAUUSD），第一次連線只要 2 天 K 棒，不回補 60 天。
- 測試：`tests/test_ext.py`（開倉帶安全停損、重送不重複、平倉與模擬單補進對照、做過的 key 不重開、太晚不追、
  換倉、斷訊不進場只警示一次、券商端停損、緊急停止後不重開、對照表、只要 2 天報價、/ext 密鑰與 404、對帳略過外部訊號）。

## 部署（Coolify）
1. 新 GitHub repo（不要跟 us-index-lab 放一起，改回測室才不會重啟實盤）
2. Coolify 新增服務、選 Dockerfile，網域例如 `cfd.tzujenlab.com`
3. Persistent Storage 掛 volume 到 `/data`
4. 環境變數照 `.env.example`：`EA_TOKEN`（自己產生一串長亂碼）、`LIVE_PASSWORD`、`TG_BOT_TOKEN`、`TG_CHAT_ID`、
   `EXT_TOKEN`（外部訊號用，另一串長亂碼）
5. 埠號 8080，健康檢查 `/healthz`

## MT5 端（Windows VPS）
1. 安裝 Pepperstone MT5，登入 Standard 模擬帳戶（勾選儲存密碼）
2. 雙擊 `windows/install_ea.cmd`：自動把 EA 複製進 MT5 並編譯
3. MT5：工具 → 選項 → EA交易 → 勾選「允許演算法交易」「允許 WebRequest」，加入服務網址
4. 把 TzujenBridge 拖到任一張圖表，參數填 `ServerURL`、`Token`
5. 工具列「演算法交易」按鈕打開；儀表板出現「EA 連線中」就完成
6. 第一次連線會回補 60 天 1 分 K（約一分鐘），補完才開始下單

EA 參數 `AllowLive` 預設 false：**在真實帳戶上不會開新倉**，要上真倉時才改 true。

## 實盤（第二套服務，2026-11-28）
真倉跟模擬倉分開兩套服務、兩個 MT5，同一個 repo：模擬倉照跑全部策略，實盤只跑 `instances.live.json` 裡的。
同一支策略兩邊都在跑，可以直接比同一個訊號在模擬和真實帳戶的成交差異。

**Coolify**：新增第二個服務（同一個 repo、Dockerfile），網域例如 `cfd-real.tzujenlab.com`，**另外掛一個 volume 到 `/data`**（不能跟模擬倉共用），環境變數：
| 變數 | 值 |
|---|---|
| `INSTANCES_FILE` | `/app/instances.live.json` |
| `EA_TOKEN` | 新的一串長亂碼（不要跟模擬倉一樣，接錯服務才不會下錯帳戶） |
| `LIVE_PASSWORD` | 一定要設：儀表板的緊急停止、暫停、檢討條件都能改東西 |
| `TG_BOT_TOKEN`、`TG_CHAT_ID` | 可以跟模擬倉同一個群組 |
| `TG_PREFIX` | `【實盤】`（同一個群組才分得出是哪一套） |
| `ACCOUNT_MAX_DD_USD` | `500`（帳戶 2000 美元的 25%；所有策略已實現回撤超過就全部暫停進場，網頁可改） |
| `EXT_TOKEN` | 不用設（實盤沒有外部訊號） |

**MT5**：在 VPS 上雙擊 `windows/setup_live_mt5.cmd` → 複製一份 Pepperstone MT5 到 `C:\MT5-Live`、建桌面捷徑「MT5 LIVE」、裝好 EA。
VPS 重開機自動啟動：雙擊 `windows/autostart_mt5.cmd`（兩個 MT5 都加進啟動資料夾，已經有的略過）。
登入實盤帳戶後，EA 參數 `ServerURL` = 實盤網址、`Token` = 實盤的 `EA_TOKEN`、**`AllowLive = true`**。模擬倉的 MT5 照常開著。

**instances.live.json**：那指 GY25 60 分、那指 GY24_S、黃金 GY25 90 分（`start_paused`：第一次上線先暫停進場，要跑在網頁按「恢復進場」）。
手數跟模擬倉一樣（0.1／0.1／0.01）。各策略的檢討門檻寫在 `review`，換算成美元和佔帳戶比例寫在 `basis`。

## 在自己的 Windows 電腦先測（不用 Coolify、不用 VPS）
1. `windows/install_ea.cmd`：安裝並編譯 EA
2. `windows/run_local_server.cmd`：在本機啟動服務（視窗會顯示網址與 Token），瀏覽器自動開儀表板
3. MT5 允許 WebRequest 加入 `http://127.0.0.1`，EA 參數 ServerURL=`http://127.0.0.1`、Token=`localtest-123456`
4. 這樣就是完整流程，會在模擬帳戶上真的下單；關掉服務視窗就停止

## 和回測的對應
- 每根 60 分 K 收盤，用券商的 1 分 K 組 K 棒（切法與回測相同）呼叫 on_bar()
- 進場 stop／limit → 券商掛單，下一根 K 收盤時撤掉重掛（MC 慣例）；掛單價已被穿過 → 市價
- 出場 stop → 持倉 SL、出場 limit → 持倉 TP、出場市價 → 平倉
- 進場時附「安全停損」（`safety_units` 個停損單位），只防斷線與極端行情，下一根 K 收盤就換成策略停損
- 週末：依券商每週最後一筆報價時間（從歷史資料算出）提前 WKX 分鐘出場
- 持倉狀態（進場價、停損單位、最大浮盈、當日進場次數）以券商實際持倉與成交紀錄為準

`tests/test_live_vs_backtest.py` 用模擬券商逐分鐘跑完整流程，跟回測引擎逐筆比對：
NAS100、JP225 測試資料上，進場時間與每筆損益都一致（只差資料結尾那一筆）。

## 安全機制
- EA 斷線：伺服器 3 分鐘沒收到同步就發 Telegram；持倉的停損在券商端，不受影響
- 對帳：同一 magic 多筆持倉（自動暫停進場）、手數不符、持倉超過 3 分鐘沒停損 → 警示
- 緊急停止：儀表板右上角，平掉所有策略持倉、撤所有掛單、停止下單
- 單一策略可以「暫停進場」（既有持倉仍照策略出場）
- 動作失敗（例如休市中下單）：休市類錯誤一分鐘後重試，其他錯誤發警示

## 改策略時
1. 在 us-index-lab 修改、回測
2. 把 `engine.py`、`bars.py`、`strategies.py`、`instruments.py` 四個檔案覆蓋到 `app/core/`
3. 跑 `tests/test_live_vs_backtest.py` 確認一致，再推上去

## EBX（進場 K 災難停損）
策略參數有 `EBX`（例如 GuYuan_2024_S 預設 1）時，進場附帶的安全停損改為 AvgRange(200)×EBX，
跟回測的進場 K 災難停損一致；下一根 K 收盤時換成策略自己的停損。EBX=0 時維持原本的 `safety_units`。

## 限制（第一版）
- 不支援持倉中反手（TMFF 這類多空策略要再加），目前兩支都只做多
- 日內出場 DAYX 以美東 17:00 轉倉為準
