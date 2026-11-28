"""事先訂好的「檢討條件」：超出回測看過的最壞情況才算異常，觸發時推 Telegram、（預設）暫停進場。

instances.json 每支策略可以加：
  "review": {
    "max_consec_loss": 18,     連續虧損筆數「超過」這個數
    "min_pf50": 0.55,          最近 50 筆的獲利因子「低於」這個數（不到 50 筆不判斷）
    "max_dd_pts": 2000,        已平倉累積點數的最大回撤「超過」這個點數
    "action": "pause",         pause = 觸發時暫停進場（持倉照常出場）；alert = 只提醒
    "since": "2026-11-28",     從哪天的進場開始算（省略 = 這支策略的全部紀錄）
    "basis": "回測 …"          訂這組門檻的依據，提醒時一起顯示，免得到時候忘記為什麼
  }
規則的精神：條件是事前訂的、觸發是自動的；觸發後先停下來看原因，不是馬上重新調參數。
同一條觸發後只提醒一次；條件解除（例如連虧被一筆獲利打斷）後才會重新計算。
手動按「恢復進場」可以覆蓋，之後同一條件不會再自動暫停，直到它解除再觸發。
"""
import time
from datetime import datetime, timezone, timedelta

from . import alerts, db, recon

N_PF = 50
RULES = (("max_consec_loss", "連續虧損", "筆"), ("min_pf50", f"最近 {N_PF} 筆 PF", ""), ("max_dd_pts", "最大回撤", "點"))


def _since(cfg):
    s = cfg.get("since")
    if not s:
        return 0
    try:
        return int(datetime.strptime(str(s), "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
    except ValueError:
        return 0


def metrics(trades):
    """trades：已平倉、依出場時間排序，每筆有 pts"""
    streak = 0
    for t in reversed(trades):
        if t["pts"] > 0:
            break
        streak += 1
    last = trades[-N_PF:]
    gw = sum(t["pts"] for t in last if t["pts"] > 0)
    gl = -sum(t["pts"] for t in last if t["pts"] < 0)
    pf = None if len(trades) < N_PF else (round(gw / gl, 2) if gl > 0 else 99.0)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["pts"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    cur_dd = peak - eq
    return {"n": len(trades), "consec_loss": streak, "pf50": pf, "max_dd_pts": round(dd, 2), "cur_dd_pts": round(cur_dd, 2)}


def evaluate(cfg, m):
    out = []
    for key, label, unit in RULES:
        lim = cfg.get(key)
        if lim is None:
            continue
        if key == "max_consec_loss":
            val, hit = m["consec_loss"], m["consec_loss"] > lim
        elif key == "min_pf50":
            val, hit = m["pf50"], m["pf50"] is not None and m["pf50"] < lim
        else:
            val, hit = m["max_dd_pts"], m["max_dd_pts"] > lim
        out.append({"key": key, "label": label, "unit": unit, "limit": lim, "value": val, "hit": bool(hit),
                    "near": bool(not hit and val is not None and (
                        (key == "min_pf50" and val < lim * 1.15) or (key != "min_pf50" and val >= lim * 0.8)))})
    return out


def _trades(inst, cfg):
    since = _since(cfg)
    rows = []
    for t in recon.live_trades(int(inst["magic"])):
        if t["exit_ts"] is None or t["entry_ts"] < since:
            continue
        rows.append({"exit_ts": t["exit_ts"], "pts": (t["exit"] - t["entry"]) * t["dir"]})
    rows.sort(key=lambda r: r["exit_ts"])
    return rows


def status(instances):
    res = []
    for inst in instances:
        cfg = inst.get("review")
        if not cfg:
            continue
        m = metrics(_trades(inst, cfg))
        res.append({"id": inst["id"], "name": inst["name"], "magic": inst["magic"], "action": cfg.get("action", "pause"),
                    "basis": cfg.get("basis", ""), "since": cfg.get("since"), "metrics": m, "rules": evaluate(cfg, m)})
    return res


def check(instances, now=None):
    """定期跑：新觸發的條件 → 提醒（＋暫停進場）；已解除的條件 → 清掉，之後可以再觸發"""
    now = int(now or time.time())
    hits = set(db.kv_get("review_hits", []) or [])
    fired = []
    for r in status([i for i in instances if i.get("enabled", True)]):
        for rule in r["rules"]:
            k = f"{r['id']}:{rule['key']}"
            if not rule["hit"]:
                hits.discard(k)
                continue
            if k in hits:
                continue
            hits.add(k)
            fired.append(k)
            cmp = "低於" if rule["key"] == "min_pf50" else "超過"
            msg = (f"⚠ 檢討條件觸發：{r['name']}　{rule['label']} {rule['value']}{rule['unit']}，"
                   f"{cmp}事先訂的 {rule['limit']}{rule['unit']}。")
            if r["action"] == "pause":
                paused = set(db.kv_get("paused", []) or [])
                paused.add(r["id"])
                db.kv_set("paused", sorted(paused))
                msg += "已自動暫停進場（持倉照常出場）。"
            msg += "先查原因：行情型態變了、成交品質變差、還是程式問題；不要急著重新調參數。"
            if r["basis"]:
                msg += f"\n依據：{r['basis']}"
            alerts.event("error", msg, r["id"])
    db.kv_set("review_hits", sorted(hits))
    db.kv_set("review_at", now)
    return fired
