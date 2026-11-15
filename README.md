# cfd-live — 指數 CFD 自動下單（MT5）

回測室（us-index-lab）挑出來的策略，在 MT5 上自動執行。
**策略邏輯只有一份**：`app/core/` 是從 us-index-lab 原封不動複製的引擎與策略，
這裡只負責「K 棒收盤 → 呼叫 on_bar() → 翻成 MT5 動作」。

```
Coolify（這個服務）  ⇄  HTTPS  ⇄  MT5 裡的 TzujenBridge EA  →  券商（Pepperstone）
  策略、對帳、儀表板、Telegram       只同步資料、執行動作
```

## 目前跑的策略（instances.json）
| id | 商品 | 策略 | 手數 | magic |
|---|---|---|---|---|
| jp_gy24 | JPN225 | GuYuan_2024（台指參數） | 0.1 | 225024 |
| jp_gy24_w9 | JPN225 | GuYuan_2024（LOSS 2、WIN 9、EBX 2，日經掃描的平台，並排比較用） | 0.1 | 225029 |
| nas_arx | NAS100 | AR_crossover（預設參數） | 0.1 | 100011 |

改策略、手數、新增商品：改 `instances.json` 推上去。magic 不能重複。
同一商品可以同時跑多支（例如上面兩支日經）：帳戶要是 **Hedge（對沖）** 模式，各支用自己的 magic 管理持倉、掛單、成交，
互不干擾（`tests/test_two_instances.py`）。Netting（淨額）帳戶不行，同商品的部位會合併。

## 部署（Coolify）
1. 新 GitHub repo（不要跟 us-index-lab 放一起，改回測室才不會重啟實盤）
2. Coolify 新增服務、選 Dockerfile，網域例如 `cfd.tzujenlab.com`
3. Persistent Storage 掛 volume 到 `/data`
4. 環境變數照 `.env.example`：`EA_TOKEN`（自己產生一串長亂碼）、`LIVE_PASSWORD`、`TG_BOT_TOKEN`、`TG_CHAT_ID`
5. 埠號 8080，健康檢查 `/healthz`

## MT5 端（Windows VPS）
1. 安裝 Pepperstone MT5，登入 Standard 模擬帳戶（勾選儲存密碼）
2. 雙擊 `windows/install_ea.cmd`：自動把 EA 複製進 MT5 並編譯
3. MT5：工具 → 選項 → EA交易 → 勾選「允許演算法交易」「允許 WebRequest」，加入服務網址
4. 把 TzujenBridge 拖到任一張圖表，參數填 `ServerURL`、`Token`
5. 工具列「演算法交易」按鈕打開；儀表板出現「EA 連線中」就完成
6. 第一次連線會回補 60 天 1 分 K（約一分鐘），補完才開始下單

EA 參數 `AllowLive` 預設 false：**在真實帳戶上不會開新倉**，要上真倉時才改 true。

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
