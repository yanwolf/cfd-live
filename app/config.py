"""設定：環境變數 + instances.json（要跑哪些策略）"""
import json
import os

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
EA_TOKEN = os.getenv("EA_TOKEN", "")                 # EA 連線用的密鑰（MT5 EA 參數要填一樣的）
EXT_TOKEN = os.getenv("EXT_TOKEN", "")               # 外部訊號（例如 gold-scalper）回報部位用的密鑰；沒設就不收
LIVE_PASSWORD = os.getenv("LIVE_PASSWORD", "")       # 儀表板密碼
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
SERVER_TZ = os.getenv("SERVER_TZ", "nyclose")       # 券商伺服器時區：nyclose（美東 +7，Pepperstone 等）或 fixed:+2
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "60"))   # 算指標用的 1 分 K 天數
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "60"))   # 第一次連線時向 EA 要多少天歷史
HEARTBEAT_ALERT_SEC = int(os.getenv("HEARTBEAT_ALERT_SEC", "180"))
TG_PREFIX = os.getenv("TG_PREFIX", "")                # Telegram 前綴，例如「【實盤】」，同一個群組收兩套服務時分得出來
ACCOUNT_MAX_DD_USD = float(os.getenv("ACCOUNT_MAX_DD_USD", "0") or 0)   # 帳戶合計已實現回撤（美元）檢討門檻，0 = 不檢查（網頁可改）
INSTANCES_FILE = os.getenv("INSTANCES_FILE", os.path.join(os.path.dirname(os.path.dirname(__file__)), "instances.json"))


def load_instances():
    with open(INSTANCES_FILE, encoding="utf-8") as fh:
        items = json.load(fh)
    seen_magic, seen_id = set(), set()
    for it in items:
        it.setdefault("params", {})
        it.setdefault("minutes", None)
        it.setdefault("align", "session")
        it.setdefault("safety_units", 3.0)
        it.setdefault("enabled", True)
        if it.get("strategy") == "external":            # 外部訊號：策略在別的服務跑（app/ext.py）
            if not it.get("engine_id"):
                raise ValueError(f"instances.json 外部訊號策略 {it['id']} 要填 engine_id")
            it.setdefault("stale_sec", 180)
            it.setdefault("max_entry_delay_sec", 300)
            it.setdefault("safety_mult", 2.0)
        if it["magic"] in seen_magic or it["id"] in seen_id:
            raise ValueError(f"instances.json 重複的 id 或 magic：{it['id']} / {it['magic']}")
        seen_magic.add(it["magic"]); seen_id.add(it["id"])
    return items
