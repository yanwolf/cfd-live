"""週末休市／券商斷線時的重試（2026-11-28 搬 VPS 後的事故重現）：
外部訊號在休市時要平倉 → 每 2 分鐘再要一次、每筆失敗又排一筆重試 → 週末堆出上百筆、每秒狂送。
修正後：同一張單的平倉只會有一筆在排隊、重試間隔遞增（最多 15 分鐘）、10031 斷線也重試、
進場單超過 15 分鐘沒送成就作廢（不在週一開盤跳空後追進），開盤第一次就平掉。"""
import json, os, sys, tempfile
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DB_SYNCHRONOUS"] = "OFF"
os.environ["EXT_TOKEN"] = "ext-secret"
GOLD = {"id": "gold", "name": "黃金", "symbol": "XAUUSD", "broker_symbol": "XAUUSD", "strategy": "external",
        "source": "gold-scalper", "engine_id": "chan_profile_900", "lots": 0.01, "magic": 900015, "tick_size": 0.01,
        "stale_sec": 10 ** 9}
with open(os.path.join(tmp, "instances.json"), "w") as fh:
    json.dump([GOLD], fh)
os.environ["INSTANCES_FILE"] = os.path.join(tmp, "instances.json")
import importlib
from app import config; importlib.reload(config)
from app import db, alerts
db.reset_for_tests()
alerts._ensure_worker = lambda: None
from app import ext, sync, live, timeconv as T

T0 = 1_800_000_000
POS = {"ticket": 435201800, "symbol": "XAUUSD", "type": 0, "volume": 0.01, "price": 3647.05, "sl": 3600.0, "tp": 0.0,
       "magic": 900015, "time": T.utc2srv(T0 - 3600), "profit": 0.0, "comment": "GOLD1"}


TICK_T = {"t": None}         # None = 報價跟著時間走（盤中）；數字 = 報價停在那個時間（休市）


def payload(now, positions, results):
    return {"login": 1, "server": "Pepperstone-Demo", "connected": True, "trade_mode": 0, "time": T.utc2srv(now),
            "positions": positions, "orders": [], "deals": [], "results": results,
            "ticks": {"XAUUSD": {"bid": 3647.0, "ask": 3647.2, "time": T.utc2srv(TICK_T["t"] or now)}}, "bars": {}}


def acts(resp):
    return [ln.split("|") for ln in resp.strip().split("\n") if ln.startswith("A|")]


# 系統原本就持有這筆（open_ticket 對得上），對方在休市期間已經空手
st = {"first_seen": T0 - 7200, "open_ticket": POS["ticket"], "open_key": "k1", "done_keys": ["k1"]}
db.kv_set("st:gold", st)
db.kv_set("ext:gold", {"received_at": T0, "position": None, "price": 3640.0})
orig_desired = ext.desired
ext.desired = lambda inst_id: {"received_at": now, "position": None, "price": 3640.0}

# ---- 1) 券商連線中斷 2 小時（報價還在跳）：每筆回 10031 → 間隔 1、2、4、8、15 分鐘遞增重試
now, sent, results = T0, [], []
for step in range(2 * 3600 // 2):
    now = T0 + step * 2
    a = acts(sync.handle(payload(now, [dict(POS)], results), now))
    sent += a
    results = [{"id": int(f[1]), "ok": False, "retcode": 10031, "msg": "no connection"} for f in a]
closes = [f for f in sent if f[2] == "CLOSE"]
queued = db.q("SELECT COUNT(*) FROM actions WHERE status='queued' AND op='CLOSE'")[0][0]
print("斷線 2 小時送出的平倉次數", len(closes), "排隊中", queued)
assert queued <= 1 and len(closes) <= 14, (len(closes), queued)
errs = db.q("SELECT COUNT(*) FROM events WHERE msg LIKE '%10031%'")[0][0]
assert errs <= 4, errs                                  # 每 30 分鐘最多提醒一次

# ---- 2) 週末休市 48 小時（報價停住）：排隊的平倉留著不送，外部訊號也不再排新的
TICK_T["t"] = now
n = 0
for k in range(1, 48 * 3600 // 2):
    t = now + k * 2
    a = acts(sync.handle(payload(t, [dict(POS)], results), t))
    results = [{"id": int(f[1]), "ok": False, "retcode": 10018, "msg": "Market closed"} for f in a]
    n += len([f for f in a if f[2] == "CLOSE"])
print("週末送出的平倉次數", n)
assert n <= 6, n                      # 報價停住的前 5 分鐘還看不出休市，最多每分鐘試一次
assert db.q("SELECT COUNT(*) FROM actions WHERE status='queued' AND op='CLOSE'")[0][0] == 1
now += 48 * 3600
TICK_T["t"] = None

# ---- 週一開盤：下一次送出就成交，之後不再送
first = acts(sync.handle(payload(now + 2, [dict(POS)], []), now + 2))
assert [f[2] for f in first] == ["CLOSE"], first        # 開盤第一筆報價進來就送（不用等重試間隔）
results = [{"id": int(f[1]), "ok": True, "retcode": 10009} for f in first]
for k in range(1, 40):
    a = acts(sync.handle(payload(now + 2 + k * 60, [], results), now + 2 + k * 60))
    results = []
    assert not [f for f in a if f[2] == "CLOSE"], a
print("開盤後平倉一次就結束")

# ---- 進場單：休市回 10018，超過 15 分鐘就作廢
ext.desired = orig_desired
t = now + 10000
aid = live.new_action("gold", "MARKET", {"symbol": "NAS100", "magic": 100060, "side": "BUY", "volume": 0.1, "sl": 26126.0,
                                         "reason": "GY25 進場"})
db.ex("UPDATE actions SET created=? WHERE id=?", (t, aid))
results, n_sent = [], 0
for step in range(0, 3600, 2):
    a = [f for f in acts(sync.handle(payload(t + step, [], results), t + step)) if f[2] == "MARKET"]
    n_sent += len(a)
    results = [{"id": int(f[1]), "ok": False, "retcode": 10018} for f in a]
left = db.q("SELECT COUNT(*) FROM actions WHERE status='queued' AND op='MARKET'")[0][0]
print("進場單送出次數", n_sent, "還在排隊", left)
assert left == 0 and n_sent <= 16, (n_sent, left)        # 每分鐘試一次，15 分鐘後作廢

# ---- 舊版堆出來的重複排隊（部署前就在 DB 裡）：部署後第一次同步就清掉
for _ in range(50):
    db.ex("INSERT INTO actions(inst, op, payload, status, created, not_before) VALUES (?,?,?,?,?,?)",
          ("gold", "CLOSE", json.dumps({"ticket": 777, "symbol": "XAUUSD", "magic": 900015, "comment": "X1"}), "queued", t, 0))
assert sync.tidy_queue(t + 4000) == 49
print("OK")
