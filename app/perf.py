"""實盤績效統計：全部從券商成交紀錄（deals）重算，以帳戶幣別（美元）為主。

· 每支策略用自己的 magic 分開算（Hedge 帳戶，各策略持倉互不影響，所以「帳戶合計」就是各策略相加）
· 一筆交易 = 一個 position id 的進場到出場；金額 = 券商損益 + 隔夜利息 + 手續費
· 點數 = (出場價 − 進場價) × 方向，已含點差（券商成交價本來就是買在 ask、賣在 bid）
· magic 對不到 instances.json 的成交（例如手動下單）歸到「其他」
"""
import time

from . import db, recon


def _stats(trades):
    """trades：已平倉、依出場時間排序；回傳統計與累積曲線"""
    eq, peak, mdd, curve = 0.0, 0.0, 0.0, []
    wins = [t for t in trades if t["money"] > 0]
    gw = sum(t["money"] for t in wins)
    gl = -sum(t["money"] for t in trades if t["money"] < 0)
    for t in trades:
        eq += t["money"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        curve.append({"t": t["exit_ts"], "eq": round(eq, 2)})
    n = len(trades)
    return {"trades": n, "net_money": round(eq, 2), "net_pts": round(sum(t["pts"] for t in trades), 2),
            "wins": len(wins), "win_rate": round(len(wins) / n * 100, 1) if n else None,
            "pf": round(gw / gl, 2) if gl > 0 else (None if not gw else 99.0),
            "avg_money": round(eq / n, 2) if n else None, "max_dd": round(mdd, 2),
            "best": round(max((t["money"] for t in trades), default=0), 2),
            "worst": round(min((t["money"] for t in trades), default=0), 2), "curve": curve}


def compute(instances, start=None):
    start = int(start or 0)
    snap = db.kv_get("snap", {}) or {}
    acct = db.kv_get("account", {}) or {}
    known = {int(i["magic"]): i for i in instances}
    magics = [int(r[0]) for r in db.q("SELECT DISTINCT magic FROM deals")]
    groups = []
    for mg in sorted(set(magics) | set(known), key=lambda x: (x not in known, x)):
        inst = known.get(mg)
        lt = [t for t in recon.live_trades(mg) if t["entry_ts"] >= start]
        if not lt and not inst:
            continue
        closed = []
        for t in lt:
            if t["exit_ts"] is None:
                continue
            closed.append({"entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"], "dir": t["dir"], "vol": t["vol"],
                           "entry": t["entry"], "exit": t["exit"], "pts": round((t["exit"] - t["entry"]) * t["dir"], 2),
                           "money": round(t["profit"] + t["swap"] + t["commission"], 2), "swap": round(t["swap"], 2)})
        closed.sort(key=lambda t: t["exit_ts"])
        pos = [p for p in snap.get("positions", []) if int(p.get("magic", -1)) == mg]
        s = _stats(closed)
        s.update({
            "magic": mg, "id": inst["id"] if inst else f"magic_{mg}",
            "name": inst["name"] if inst else (f"其他（magic {mg}）" if mg else "手動下單（magic 0）"),
            "strategy": inst["strategy"] if inst else "", "symbol": inst["broker_symbol"] if inst else "",
            "enabled": inst.get("enabled", True) if inst else False, "external": bool(inst and inst.get("strategy") == "external"),
            "first": min((t["entry_ts"] for t in lt), default=None),
            "open": [{"side": "多" if p.get("type") == 0 else "空", "volume": p.get("volume"), "price": p.get("price"),
                      "profit": p.get("profit")} for p in pos],
            "last": list(reversed(closed[-15:])),
        })
        if not inst and not s["trades"] and not s["open"]:
            continue
        groups.append(s)
    # 帳戶合計：把各策略每筆的損益照出場時間合併
    events = []
    for g in groups:
        prev = 0.0
        for p in g["curve"]:
            events.append((p["t"], p["eq"] - prev)); prev = p["eq"]
    events.sort()
    eq, peak, mdd, curve = 0.0, 0.0, 0.0, []
    for t, d in events:
        eq += d; peak = max(peak, eq); mdd = max(mdd, peak - eq)
        curve.append({"t": t, "eq": round(eq, 2)})
    n = sum(g["trades"] for g in groups)
    w = sum(g["wins"] for g in groups)
    floating = sum(float(o.get("profit") or 0) for g in groups for o in g["open"])
    first = min((g["first"] for g in groups if g["first"]), default=None)
    return {"start": start or first, "now": int(time.time()), "currency": acct.get("currency") or "USD",
            "account": {"net_money": round(eq, 2), "closes": n, "wins": w, "win_rate": round(w / n * 100, 1) if n else None,
                        "max_dd": round(mdd, 2), "open_n": sum(len(g["open"]) for g in groups), "floating": round(floating, 2),
                        "balance": acct.get("balance"), "equity": acct.get("equity"), "curve": curve},
            "strategies": groups}
