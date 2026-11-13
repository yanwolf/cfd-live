"""設定：環境變數 + instances.json（要跑哪些策略）"""
import json
import os

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
EA_TOKEN = os.getenv("EA_TOKEN", "")                 # EA 連線用的密鑰（MT5 EA 參數要填一樣的）
LIVE_PASSWORD = os.getenv("LIVE_PASSWORD", "")       # 儀表板密碼
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
SERVER_TZ = os.getenv("SERVER_TZ", "nyclose")       # 券商伺服器時區：nyclose（美東 +7，Pepperstone 等）或 fixed:+2
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "60"))   # 算指標用的 1 分 K 天數
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "60"))   # 第一次連線時向 EA 要多少天歷史
HEARTBEAT_ALERT_SEC = int(os.getenv("HEARTBEAT_ALERT_SEC", "180"))
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
        if it["magic"] in seen_magic or it["id"] in seen_id:
            raise ValueError(f"instances.json 重複的 id 或 magic：{it['id']} / {it['magic']}")
        seen_magic.add(it["magic"]); seen_id.add(it["id"])
    return items
