"""實盤 vs 回測對帳：拿券商的成交紀錄，和「同一段時間、同一份 1 分 K、同一支策略」的回測逐筆比對。

回測價格是 bid 報價（K 棒資料就是 bid）。實盤多單買在 ask、賣在 bid；空單賣在 bid、買回在 ask。
所以把點差補回去之後，剩下的差就是「滑價＋時間差」：
  多單  進場滑價 = 實盤進場 −（回測進場 + 點差）      出場滑價 = 回測出場 − 實盤出場
  空單  進場滑價 = 回測進場 − 實盤進場                 出場滑價 = 實盤出場 −（回測出場 + 點差）
正數 = 比回測差。點差用商品預設值（實際點差會浮動，所以單筆會有幾點誤差，看平均就好）。
"""
import json
import time

import numpy as np

from . import config, db, live
from .core import engine, instruments
from .core.strategies import REGISTRY


def live_trades(magic):
    """券商成交 → 一筆一筆的交易（用 position id 把進場、出場配對）"""
    rows = db.q("SELECT data FROM deals WHERE magic=? ORDER BY ts, ticket", (int(magic),))
    pos = {}
    for (raw,) in rows:
        d = json.loads(raw)
        pid = d.get("position") or d.get("ticket")
        t = pos.setdefault(pid, {"position": pid, "dir": None, "vol": 0.0, "entry": None, "entry_ts": None,
                                 "exit": None, "exit_ts": None, "profit": 0.0, "swap": 0.0, "commission": 0.0, "exits": 0})
        t["profit"] += float(d.get("profit") or 0)
        t["swap"] += float(d.get("swap") or 0)
        t["commission"] += float(d.get("commission") or 0)
        if int(d.get("entry", -1)) == 0:                    # 進場
            t["dir"] = 1 if int(d.get("type", 0)) == 0 else -1
            t["entry"] = float(d["price"]); t["entry_ts"] = int(d["time_utc"]); t["vol"] = float(d.get("volume") or 0)
        else:                                               # 出場（可能分批，取量加權）
            v = float(d.get("volume") or 0)
            prev = t["exit"] * t["exits"] if t["exit"] is not None else 0.0
            t["exits"] += v
            t["exit"] = (prev + float(d["price"]) * v) / t["exits"] if t["exits"] else float(d["price"])
            t["exit_ts"] = int(d["time_utc"])
    out = [t for t in pos.values() if t["entry_ts"]]
    out.sort(key=lambda t: t["entry_ts"])
    return out


def backtest(inst, start_ts, now):
    cls = REGISTRY[inst["strategy"]]
    minutes = int(inst.get("minutes") or cls.minutes)
    m = live.load_m1(inst["broker_symbol"], start_ts - config.LOOKBACK_DAYS * 86400)
    if m is None or len(m["ts"]) < live.MIN_M1:
        return [], minutes
    c = instruments.cost_defaults(inst["symbol"]) if hasattr(instruments, "cost_defaults") else {}
    costs = {"spread": float(c.get("spread", 0)), "slippage": 0.0, "commission": 0.0,
             "swap_long_pct": float(c.get("swap_long_pct", 0)), "swap_short_pct": float(c.get("swap_short_pct", 0))}
    r = engine.run(cls, inst.get("params") or {}, m, costs, minutes=minutes, align=inst.get("align", "session"),
                   start_ts=start_ts, symbol=inst["symbol"], cache_key=("recon", inst["id"], int(now // 60)))
    return r["trades"], minutes


def reconcile(inst, now=None):
    now = int(now or time.time())
    lt = live_trades(inst["magic"])
    st = live.get_state(inst["id"])
    start = min([st.get("first_seen") or now] + [t["entry_ts"] for t in lt])
    bt, minutes = backtest(inst, start, now)
    spread = float((instruments.cost_defaults(inst["symbol"]) if hasattr(instruments, "cost_defaults") else {}).get("spread", 0))
    tol = minutes * 60
    used, rows = set(), []
    for t in lt:
        best = None
        for k, b in enumerate(bt):
            if k in used or b["dir"] != t["dir"]:
                continue
            dt = abs(b["entry_ts"] - t["entry_ts"])
            if dt <= tol and (best is None or dt < best[1]):
                best = (k, dt)
        b = None
        if best:
            used.add(best[0]); b = bt[best[0]]
        rows.append(_row(t, b, spread))
    for k, b in enumerate(bt):
        if k in used:
            continue
        rows.append(_row(None, b, spread))
    rows.sort(key=lambda r: r["entry_ts"])
    m = [r for r in rows if r["status"] == "相符"]
    closed = [r for r in m if r["exit_slip"] is not None]
    es = [r["entry_slip"] for r in m]
    xs = [r["exit_slip"] for r in closed]
    summ = {
        "since": start, "minutes": minutes, "spread": spread,
        "live_n": len(lt), "bt_n": sum(1 for r in rows if r["bt_entry_ts"]),
        "matched": len(m), "missed": sum(1 for r in rows if r["status"] == "實盤沒有"),
        "extra": sum(1 for r in rows if r["status"] == "回測沒有"),
        "entry_slip_avg": _avg(es), "exit_slip_avg": _avg(xs),
        "slip_per_side": _avg(es + xs),
        "live_pts": round(sum(r["live_pts"] for r in closed), 2) if closed else 0.0,
        "bt_pts": round(sum(r["bt_pts_after_spread"] for r in closed), 2) if closed else 0.0,
        "live_money": round(sum(t["profit"] + t["swap"] + t["commission"] for t in lt if t["exit_ts"]), 2),
        "slip_assumed": float((instruments.cost_defaults(inst["symbol"]) if hasattr(instruments, "cost_defaults") else {}).get("slippage", 0)),
    }
    return {"id": inst["id"], "name": inst["name"], "strategy": inst["strategy"], "symbol": inst["broker_symbol"],
            "magic": inst["magic"], "enabled": inst.get("enabled", True), "summary": summ, "rows": rows}


def _avg(xs):
    return round(float(np.mean(xs)), 2) if xs else None


def _row(t, b, spread):
    r = {"status": "相符" if (t and b) else ("回測沒有" if t else "實盤沒有"),
         "dir": (t or b)["dir"], "entry_ts": (t or b)["entry_ts"],
         "live_entry_ts": t["entry_ts"] if t else None, "live_exit_ts": t["exit_ts"] if t else None,
         "live_entry": t["entry"] if t else None, "live_exit": t["exit"] if t else None,
         "bt_entry_ts": b["entry_ts"] if b else None, "bt_exit_ts": b["exit_ts"] if b else None,
         "bt_entry": b["entry"] if b else None, "bt_exit": b["exit"] if b else None,
         "bt_in": b["in"] if b else None, "bt_out": b["out"] if b else None,
         "entry_slip": None, "exit_slip": None, "entry_dt": None, "exit_dt": None,
         "live_pts": None, "bt_pts_after_spread": None, "live_money": None}
    if b:
        r["bt_pts_after_spread"] = round(b["gross"] - spread, 2) if b["out"] != "資料結束" else None
        if b["out"] == "資料結束":
            r["bt_exit"] = None; r["bt_exit_ts"] = None; r["bt_out"] = "持倉中"
    if t:
        r["live_money"] = round(t["profit"] + t["swap"] + t["commission"], 2) if t["exit_ts"] else None
        if t["exit"] is not None:
            r["live_pts"] = round((t["exit"] - t["entry"]) * t["dir"], 2)
    if t and b:
        d = t["dir"]
        r["entry_dt"] = t["entry_ts"] - b["entry_ts"]
        r["entry_slip"] = round(t["entry"] - (b["entry"] + spread) if d > 0 else b["entry"] - t["entry"], 2)
        if t["exit"] is not None and r["bt_exit"] is not None:
            r["exit_dt"] = t["exit_ts"] - b["exit_ts"]
            r["exit_slip"] = round(b["exit"] - t["exit"] if d > 0 else t["exit"] - (b["exit"] + spread), 2)
    return r


def reconcile_all(instances, now=None):
    out = []
    for inst in instances:
        if not inst.get("enabled", True) and not db.q("SELECT 1 FROM deals WHERE magic=? LIMIT 1", (int(inst["magic"]),)):
            continue
        try:
            out.append(reconcile(inst, now))
        except Exception as e:                       # 單一策略出錯不影響其他
            out.append({"id": inst["id"], "name": inst["name"], "error": f"{type(e).__name__}: {e}"})
    return out
