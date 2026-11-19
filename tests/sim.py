"""模擬一個 MT5 券商 + EA，逐分鐘呼叫 sync.handle，檢查實盤邏輯跟回測引擎的結果一致。

模擬券商：
  · 報價 = 1 分 K（bid），ask = bid + spread
  · 每分鐘開頭 EA 同步一次（只送已完成的 K 棒），收到的動作立刻在該分鐘開盤執行
  · 掛單、SL、TP 用該分鐘的高低價觸發；同一分鐘 SL 和 TP 都碰到算 SL
"""
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.dirname(os.path.abspath(__file__))]


class FakeBroker:
    def __init__(self, sym, m, spread, bid_trigger=False):
        self.sym, self.m, self.spread = sym, m, spread
        self.bid_trigger = bid_trigger           # True：買單也用 bid 觸發（跟回測一致，用來驗證邏輯）；False：照 MT5 用 ask
        self.pos, self.ords, self.deals = [], [], []
        self.tk = 1000
        self.results = []
        self.done_ids = set()
        self.closed = []                          # (dir, entry, exit, entry_ts, exit_ts, reason)

    def _tk(self):
        self.tk += 1
        return self.tk

    def _deal(self, ts, magic, entry, d, price, vol, position, profit=0.0, comment=""):
        from app import timeconv as T
        self.deals.append({"ticket": self._tk(), "order": 0, "position": position, "symbol": self.sym,
                           "type": 0 if d > 0 else 1, "entry": entry, "volume": vol, "price": price, "profit": profit,
                           "swap": 0.0, "commission": 0.0, "magic": magic, "time": T.utc2srv(ts), "comment": comment})

    def open(self, ts, d, price, vol, sl, tp, magic, comment):
        from app import timeconv as T
        t = self._tk()
        self.pos.append({"ticket": t, "symbol": self.sym, "type": 0 if d > 0 else 1, "volume": vol, "price": price,
                         "sl": sl, "tp": tp, "magic": magic, "time": T.utc2srv(ts), "profit": 0.0, "swap": 0.0,
                         "comment": comment, "_ts": ts})
        self._deal(ts, magic, 0, d, price, vol, t, comment=comment)

    def close(self, ts, p, price, reason):
        d = 1 if p["type"] == 0 else -1
        self.pos.remove(p)
        self._deal(ts, p["magic"], 1, -d, price, p["volume"], p["ticket"], profit=(price - p["price"]) * d * p["volume"])
        self.closed.append((d, p["price"], price, p["_ts"], ts, reason, p["magic"]))

    def execute(self, lines, ts, k):
        o_bid = self.m["o"][k]
        for ln in lines:
            if not ln.startswith("A|"):
                continue
            f = ln.split("|")
            aid, op, sym, magic, side = int(f[1]), f[2], f[3], int(f[4]), f[5]
            vol, price, sl, tp, ticket, comment = float(f[6]), float(f[7]), float(f[8]), float(f[9]), int(f[10]), f[11]
            if aid in self.done_ids:
                continue
            self.done_ids.add(aid)
            ok, code = True, 10009
            if op == "MARKET":
                d = 1 if side == "BUY" else -1
                self.open(ts, d, o_bid + (self.spread if d > 0 else 0), vol, sl, tp, magic, comment)
            elif op == "PENDING":
                self.ords.append({"ticket": self._tk(), "symbol": sym, "type": side, "volume": vol, "price": price,
                                  "sl": sl, "tp": tp, "magic": magic, "comment": comment}); code = 10008
            elif op == "CANCEL":
                before = len(self.ords)
                self.ords = [o for o in self.ords if o["ticket"] != ticket]
                if len(self.ords) == before:
                    ok, code = False, 10013
            elif op == "MODIFY":
                for p in self.pos:
                    if p["ticket"] == ticket:
                        p["sl"], p["tp"] = sl, tp
            elif op == "CLOSE":
                for p in list(self.pos):
                    if p["ticket"] == ticket:
                        self.close(ts, p, o_bid + (self.spread if p["type"] == 1 else 0), "CLOSE")
            self.results.append({"id": aid, "ok": ok, "retcode": code})

    def run_minute(self, ts, k):
        o, h, l = self.m["o"][k], self.m["h"][k], self.m["l"][k]
        sp = self.spread
        tsp = 0.0 if self.bid_trigger else sp
        for od in list(self.ords):
            t = od["type"]
            trig = (t == "BUY_STOP" and h + tsp >= od["price"]) or (t == "SELL_STOP" and l <= od["price"]) or \
                   (t == "BUY_LIMIT" and l + tsp <= od["price"]) or (t == "SELL_LIMIT" and h >= od["price"])
            if trig:
                d = 1 if t.startswith("BUY") else -1
                if self.bid_trigger:
                    px = (max(od["price"], o) if t.endswith("STOP") else min(od["price"], o)) + sp if d > 0 \
                        else (min(od["price"], o) if t.endswith("STOP") else max(od["price"], o))
                elif t.endswith("STOP"):
                    px = max(od["price"], o + sp) if d > 0 else min(od["price"], o)
                else:
                    px = min(od["price"], o + sp) if d > 0 else max(od["price"], o)
                self.ords.remove(od)
                self.open(ts, d, px, od["volume"], od["sl"], od["tp"], od["magic"], od["comment"])
        for p in list(self.pos):
            d = 1 if p["type"] == 0 else -1
            if d > 0:
                if p["sl"] and l <= p["sl"]:
                    self.close(ts, p, min(p["sl"], o), "SL"); continue
                if p["tp"] and h >= p["tp"]:
                    self.close(ts, p, max(p["tp"], o), "TP"); continue
            elif self.bid_trigger:            # 跟回測一致：空單也用 bid 觸發，成交價再加點差
                if p["sl"] and h >= p["sl"]:
                    self.close(ts, p, max(p["sl"], o) + sp, "SL"); continue
                if p["tp"] and l <= p["tp"]:
                    self.close(ts, p, min(p["tp"], o) + sp, "TP"); continue
            else:
                if p["sl"] and h + sp >= p["sl"]:
                    self.close(ts, p, max(p["sl"], o + sp), "SL"); continue
                if p["tp"] and l + sp <= p["tp"]:
                    self.close(ts, p, min(p["tp"], o + sp), "TP"); continue

    def payload(self, ts, k, since):
        from app import timeconv as T
        bars = []
        if since is not None:
            lo = np.searchsorted(self.m["ts"], since + 60)
            hi = k
            sel = range(lo, min(hi, lo + 5000))
            bars = [[T.utc2srv(int(self.m["ts"][j])), self.m["o"][j], self.m["h"][j], self.m["l"][j], self.m["c"][j], 1] for j in sel]
        pl = {"login": 1, "server": "Fake-Demo", "connected": True, "balance": 10000, "equity": 10000, "time": T.utc2srv(ts),
              "positions": [{k2: v for k2, v in p.items() if not k2.startswith("_")} for p in self.pos],
              "orders": list(self.ords), "deals": self.deals, "results": self.results,
              "ticks": {self.sym: {"bid": self.m["o"][k], "ask": self.m["o"][k] + self.spread, "time": T.utc2srv(ts),
                                   "last_m1": T.utc2srv(int(self.m["ts"][k - 1])) if k > 0 else 0}},
              "bars": {self.sym: bars}}
        self.deals, self.results = [], []
        return pl


def simulate(strategy, symbol, m, spread, start_day=20, inst_extra=None, bid_trigger=False, instances=None, after=None):
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    inst = {"id": "t1", "name": "測試", "symbol": symbol, "broker_symbol": symbol, "strategy": strategy, "lots": 1.0,
            "magic": 777, "safety_units": 3.0, "enabled": True, "params": {}, "tick_size": 0.000001}
    inst.update(inst_extra or {})
    with open(os.path.join(tmp, "instances.json"), "w") as fh:
        json.dump(instances or [inst], fh)
    os.environ["INSTANCES_FILE"] = os.path.join(tmp, "instances.json")
    import importlib
    from app import config, db
    importlib.reload(config)
    db.reset_for_tests()
    from app import sync, timeconv as T
    importlib.reload(sync)
    sync._inst_cache["items"] = None
    fb = FakeBroker(symbol, m, spread, bid_trigger)
    ts_all = m["ts"]
    k0 = int(np.searchsorted(ts_all, ts_all[0] + start_day * 86400))
    since = None
    t0 = time.time()
    for k in range(k0, len(ts_all)):
        ts = int(ts_all[k])
        pl = fb.payload(ts, k, since)
        resp = sync.handle(pl, now=ts + 2)
        lines = resp.strip().split("\n")
        for ln in lines:
            if ln.startswith("S|"):
                since = T.srv2utc(int(ln.split("|")[2]))
        fb.execute(lines, ts, k)
        fb.run_minute(ts, k)
    if after:
        after(int(ts_all[-1]) + 60)
    shutil.rmtree(tmp, ignore_errors=True)
    return fb, k0, time.time() - t0
