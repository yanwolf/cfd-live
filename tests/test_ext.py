"""外部訊號策略（app/ext.py）：對方回報部位 → MT5 照著開平倉；重送不重複、太晚不追、斷訊不進場、
安全停損、緊急停止後不重開、模擬 vs MT5 對照（點差、滑價、延遲、報價差）。"""
import json, os, sys, tempfile
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DB_SYNCHRONOUS"] = "OFF"
os.environ["EXT_TOKEN"] = "ext-secret"
GOLD = {"id": "gold", "name": "黃金", "symbol": "XAUUSD", "broker_symbol": "XAUUSD", "strategy": "external",
        "source": "gold-scalper", "engine_id": "chan_profile_900", "lots": 0.01, "magic": 900015, "tick_size": 0.01}
NAS = {"id": "nas", "name": "那指", "symbol": "NAS100", "broker_symbol": "NAS100", "strategy": "AR_crossover",
       "lots": 0.1, "magic": 100011, "tick_size": 0.1, "enabled": False}
with open(os.path.join(tmp, "instances.json"), "w") as fh:
    json.dump([GOLD, NAS], fh)
os.environ["INSTANCES_FILE"] = os.path.join(tmp, "instances.json")
import importlib
from app import config; importlib.reload(config)
from app import db, alerts
db.reset_for_tests()
alerts._ensure_worker = lambda: None
from app import ext, sync, timeconv as T
from datetime import datetime, timezone

T0 = 1_800_000_000
iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


class Broker:
    def __init__(self):
        self.pos, self.deals, self.results, self.tk, self.bid, self.spread = [], [], [], 100, 4100.0, 0.2
        self.done = set()

    def payload(self, now):
        pl = {"login": 1, "server": "Pepperstone-Demo", "connected": True, "trade_mode": 0, "time": T.utc2srv(now),
              "positions": [dict(p) for p in self.pos], "orders": [], "deals": self.deals, "results": self.results,
              "ticks": {"XAUUSD": {"bid": self.bid, "ask": round(self.bid + self.spread, 2), "time": T.utc2srv(now)}}, "bars": {}}
        self.deals, self.results = [], []
        return pl

    def execute(self, resp, now, slip=0.0):
        acts = [ln.split("|") for ln in resp.strip().split("\n") if ln.startswith("A|")]
        for f in acts:
            aid, op, side, vol, sl, ticket, cmt = int(f[1]), f[2], f[5], float(f[6]), float(f[8]), int(f[10]), f[11]
            if aid in self.done:
                continue
            self.done.add(aid)
            if op == "MARKET":
                d = 1 if side == "BUY" else -1
                px = (self.bid + self.spread + slip) if d > 0 else (self.bid - slip)
                self.tk += 1
                self.pos.append({"ticket": self.tk, "symbol": "XAUUSD", "type": 0 if d > 0 else 1, "volume": vol, "price": px,
                                 "sl": sl, "tp": 0.0, "magic": int(f[4]), "time": T.utc2srv(now), "profit": 0.0, "comment": cmt})
                self.deals.append({"ticket": self.tk + 1000, "position": self.tk, "symbol": "XAUUSD", "entry": 0, "volume": vol,
                                   "price": px, "profit": 0, "swap": 0, "commission": -0.03, "magic": int(f[4]), "time": T.utc2srv(now)})
            elif op == "CLOSE":
                for p in list(self.pos):
                    if p["ticket"] == ticket:
                        self.close(p, now, slip)
            self.results.append({"id": aid, "ok": True, "retcode": 10009})
        return acts

    def close(self, p, now, slip=0.0):
        d = 1 if p["type"] == 0 else -1
        px = (self.bid - slip) if d > 0 else (self.bid + self.spread + slip)
        self.pos.remove(p)
        self.deals.append({"ticket": p["ticket"] + 2000, "position": p["ticket"], "symbol": "XAUUSD", "entry": 1, "volume": p["volume"],
                           "price": px, "profit": round(d * (px - p["price"]) * p["volume"] * 100, 2), "swap": -0.8,
                           "commission": -0.03, "magic": p["magic"], "time": T.utc2srv(now)})


def say(position, now, closed=(), seq=[0]):
    seq[0] += 1
    return ext.receive(GOLD_I, {"source": "gold-scalper", "engine_id": "chan_profile_900", "boot": "b1", "seq": seq[0],
                                "position": position, "price": 4100.0, "recent_closed": list(closed)}, now=now)


def step(br, now, slip=0.0):
    resp = sync.handle(br.payload(now), now=now)
    return br.execute(resp, now, slip)


sync._inst_cache["items"] = None
GOLD_I = next(i for i in sync.config_instances() if i["id"] == "gold")
assert GOLD_I["stale_sec"] == 180 and GOLD_I["max_entry_delay_sec"] == 300 and GOLD_I["safety_mult"] == 2.0, GOLD_I
assert ext.match_instance(sync.config_instances(), {"engine_id": "chan_profile_900", "source": "gold-scalper"})["id"] == "gold"
assert ext.match_instance(sync.config_instances(), {"engine_id": "chan_profile_300", "source": "gold-scalper"}) is None

br = Broker()
# 1) 對方開多：馬上市價進場，帶安全停損（報價 − 2 × 停損距離）
P1 = {"key": "chan_profile_900:" + iso(T0), "direction": "bullish", "entry_time": iso(T0), "entry_price": 4097.5,
      "sl_points": 6.0, "sl_price": 4091.5}
say(P1, T0 + 2)
acts = step(br, T0 + 3, slip=0.05)
assert [a[2] for a in acts] == ["MARKET"] and acts[0][5] == "BUY", acts
assert abs(float(acts[0][8]) - (4100.2 - 12.0)) < 1e-6, acts[0][8]
assert acts[0][11].startswith("G") and len(acts[0][11]) <= 31, acts[0][11]
# 2) 重送同樣的狀態好幾次：不會重複下單
for k in range(5):
    say(P1, T0 + 4 + k)
    assert step(br, T0 + 4 + k) == [], "同一個 key 不重複進場"
assert len(br.pos) == 1
tr = ext.trade_get(P1["key"])
assert tr["status"] == "open" and abs(tr["fill_price"] - 4100.25) < 1e-6, tr
assert abs(tr["entry_slip"] - 0.05) < 1e-6 and abs(tr["spread"] - 0.2) < 1e-6, tr
assert abs(tr["basis"] - (4100.1 - 4097.5)) < 1e-6, tr
# 3) 對方平倉：市價平倉；平倉後模擬單結果補進對照表
br.bid = 4110.0
say(None, T0 + 600)
acts = step(br, T0 + 601, slip=0.03)
assert [a[2] for a in acts] == ["CLOSE"], acts
step(br, T0 + 602)
tr = ext.trade_get(P1["key"])
assert tr["status"] == "closed" and tr["close_by"] == "訊號平倉", tr
assert abs(tr["mt5_points"] - (4109.97 - 4100.25)) < 1e-6 and abs(tr["exit_slip"] - 0.03) < 1e-6, tr
assert abs(tr["swap"] + 0.8) < 1e-9 and abs(tr["commission"] + 0.06) < 1e-9, tr
say(None, T0 + 610, closed=[{"key": P1["key"], "exit_price": 4112.0, "exit_time": iso(T0 + 598), "pnl_points": 14.5,
                             "exit_reason": "觸及移動停損"}, {"key": "never-traded", "pnl_points": 3}])
tr = ext.trade_get(P1["key"])
assert tr["sim_pnl_points"] == 14.5 and ext.trade_get("never-traded") is None, tr
# 4) 平倉後對方又回報舊的那筆（網路延遲）：不會再開
say(P1, T0 + 620)
assert step(br, T0 + 621) == [], "做過的 key 不再進場"
# 5) 太晚收到的訊號不追
P2 = {"key": "chan_profile_900:" + iso(T0 + 1000), "direction": "bearish", "entry_time": iso(T0 + 1000), "entry_price": 4100.0,
      "sl_points": 5.0}
say(P2, T0 + 1000 + 400)
assert step(br, T0 + 1401) == [] and ext.trade_get(P2["key"])["status"] == "skipped", ext.trade_get(P2["key"])
# 6) 空單＋對方換成另一筆（沒有先回報空手）：平掉舊的
P3 = {"key": "chan_profile_900:" + iso(T0 + 2000), "direction": "bearish", "entry_time": iso(T0 + 2000), "entry_price": 4100.0,
      "sl_points": 5.0}
say(P3, T0 + 2001)
acts = step(br, T0 + 2002)
assert acts[0][5] == "SELL" and abs(float(acts[0][8]) - (4110.0 + 10.0)) < 1e-6, acts
P4 = dict(P3, key="chan_profile_900:" + iso(T0 + 2100), entry_time=iso(T0 + 2100), direction="bullish")
say(P4, T0 + 2101)
acts = step(br, T0 + 2102)
assert [a[2] for a in acts] == ["CLOSE"], acts
acts = step(br, T0 + 2103)
assert [a[2] for a in acts] == ["MARKET"] and acts[0][5] == "BUY", "換倉：舊的平掉後新的照常進場（還在 300 秒內）"
assert ext.trade_get(P3["key"])["close_by"] == "訊號換倉"
# 7) 對方斷訊：不進新倉、不主動平倉、只警示一次
say(None, T0 + 2200)
step(br, T0 + 2201); step(br, T0 + 2202)
assert not br.pos
P5 = {"key": "chan_profile_900:" + iso(T0 + 3000), "direction": "bullish", "entry_time": iso(T0 + 3000), "entry_price": 4100.0,
      "sl_points": 5.0}
say(P5, T0 + 3000)
step(br, T0 + 3001)
assert len(br.pos) == 1
n_warn = len(db.q("SELECT 1 FROM events WHERE msg LIKE '%沒有回報部位%'"))
assert step(br, T0 + 3000 + 400) == [] and len(br.pos) == 1, "斷訊不主動平倉"
step(br, T0 + 3000 + 401)
assert len(db.q("SELECT 1 FROM events WHERE msg LIKE '%沒有回報部位%'")) == n_warn + 1, "只警示一次"
# 8) 券商端安全停損打掉（不是訊號平倉）：標記出來，對方還說有部位也不重開
br.close(br.pos[0], T0 + 3500)
say(P5, T0 + 3500)
assert step(br, T0 + 3501) == [] and step(br, T0 + 3502) == []
assert ext.trade_get(P5["key"])["close_by"].startswith("券商端"), ext.trade_get(P5["key"])
# 9) 緊急停止：平倉；解除後同一筆不重開
P6 = {"key": "chan_profile_900:" + iso(T0 + 4000), "direction": "bullish", "entry_time": iso(T0 + 4000), "entry_price": 4100.0,
      "sl_points": 5.0}
say(P6, T0 + 4000); step(br, T0 + 4001)
assert len(br.pos) == 1
db.kv_set("kill", True); db.kv_set("kill_sent", [])
acts = step(br, T0 + 4002)
assert [a[2] for a in acts] == ["CLOSE"] and not br.pos, acts
db.kv_set("kill", False)
say(P6, T0 + 4003)
assert step(br, T0 + 4004) == [] and step(br, T0 + 4005) == []
# 10) 對照表
rep = ext.report(GOLD_I)
s = rep["summary"]
assert s["trades"] == 4 and s["matched"] == 1 and s["skipped"] == 1, s
assert abs(s["avg_diff_points"] - (9.72 - 14.5)) < 1e-6 and abs(s["avg_spread"] - 0.2) < 1e-6, s
# 11) 只有外部訊號用的商品：第一次只要 2 天報價，不回補 60 天
resp = sync.handle(br.payload(T0 + 5000), now=T0 + 5000)
since = [ln for ln in resp.split("\n") if ln.startswith("S|XAUUSD|")]
assert since, resp
db.ex("DELETE FROM bars")
resp = sync.handle(br.payload(T0 + 5001), now=T0 + 5001)
since = int([ln for ln in resp.split("\n") if ln.startswith("S|XAUUSD|")][0].split("|")[2])
assert abs(T.srv2utc(since) - (T0 + 5001 - 2 * 86400)) < 5, since
# 12) /ext 端點：沒有密鑰 401、對不到 engine_id 404、正確 200
from fastapi.testclient import TestClient
from app import server
c = TestClient(server.app)
assert c.post("/ext/gold-scalper", json={"engine_id": "chan_profile_900"}).status_code == 401
assert c.post("/ext/gold-scalper", json={"engine_id": "x"}, headers={"x-token": "ext-secret"}).status_code == 404
r = c.post("/ext/gold-scalper", json={"engine_id": "chan_profile_900", "seq": 999, "boot": "b2", "position": None},
           headers={"x-token": "ext-secret"})
assert r.status_code == 200 and ext.desired("gold")["boot"] == "b2", r.text
assert c.get("/api/ext").status_code == 200 and c.get("/api/ext").json()[0]["summary"]["trades"] == 4
# 13) 對帳(回測)不處理外部訊號
from app import recon
assert all(x["id"] != "gold" for x in recon.reconcile_all(sync.config_instances(), now=T0 + 6000))
print("OK")
