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
網頁上也可以改（存在資料庫，優先於 instances.json；按「恢復設定檔」回到 instances.json 的值），每次修改都記一筆事件。
規則的精神：條件是事前訂的、觸發是自動的；觸發後先停下來看原因，不是馬上重新調參數。
同一條觸發後只提醒一次；條件解除（例如連虧被一筆獲利打斷）後才會重新計算。
手動按「恢復進場」可以覆蓋，之後同一條件不會再自動暫停，直到它解除再觸發。
"""
import time
from datetime import datetime, timezone, timedelta

from . import alerts, config, db, recon

N_PF = 50
RULES = (("max_consec_loss", "連續虧損", "筆"), ("min_pf50", f"最近 {N_PF} 筆 PF", ""), ("max_dd_pts", "最大回撤", "點"))
ACC_RULES = (("max_dd_money", "帳戶已實現回撤", " 美元"),)


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


FIELDS = {"max_consec_loss": int, "min_pf50": float, "max_dd_pts": float}
ACC_ID = "_account"
ACC_FIELDS = {"max_dd_money": float}
ACC = {"id": ACC_ID, "name": "帳戶合計（所有策略）", "magic": None}


def _fields(inst_id):
    return ACC_FIELDS if inst_id == ACC_ID else FIELDS


def _form(inst_id):
    """給網頁產生輸入欄位"""
    if inst_id == ACC_ID:
        return [{"key": "max_dd_money", "label": "已實現回撤超過（美元）", "step": 1}]
    return [{"key": "max_consec_loss", "label": "連續虧損超過（筆）", "step": 1},
            {"key": "min_pf50", "label": "最近 50 筆 PF 低於", "step": 0.01},
            {"key": "max_dd_pts", "label": "最大回撤超過（點）", "step": 1}]


def _file_cfg(inst):
    if inst["id"] == ACC_ID:
        return {"max_dd_money": config.ACCOUNT_MAX_DD_USD, "action": "pause",
                "basis": f"服務環境變數 ACCOUNT_MAX_DD_USD = {config.ACCOUNT_MAX_DD_USD:g} 美元"} if config.ACCOUNT_MAX_DD_USD > 0 else {}
    return inst.get("review") or {}


def get_cfg(inst):
    """網頁改過的優先，否則用 instances.json 的 review（帳戶合計用環境變數）"""
    ov = (db.kv_get("review_cfg", {}) or {}).get(inst["id"])
    if ov is not None:
        return ov, "網頁"
    return _file_cfg(inst), "設定檔"


def set_cfg(inst, body):
    """body = None → 恢復設定檔；否則 {max_consec_loss, min_pf50, max_dd_pts, action, since, basis}，空白欄位 = 不檢查"""
    allc = db.kv_get("review_cfg", {}) or {}
    old, _ = get_cfg(inst)
    if body is None:
        allc.pop(inst["id"], None)
        new = _file_cfg(inst)
    else:
        new = {}
        for k, typ in _fields(inst["id"]).items():
            v = body.get(k)
            if v in (None, ""):
                continue
            v = typ(v)
            if v <= 0:
                raise ValueError(f"{k} 要大於 0")
            new[k] = v
        act = body.get("action") or "pause"
        if act not in ("pause", "alert"):
            raise ValueError("action 只能是 pause 或 alert")
        new["action"] = act
        if body.get("since"):
            datetime.strptime(str(body["since"]), "%Y-%m-%d")
            new["since"] = str(body["since"])
        new["basis"] = str(body.get("basis") or old.get("basis") or "")[:300]
        allc[inst["id"]] = new
    db.kv_set("review_cfg", allc)
    hits = set(db.kv_get("review_hits", []) or [])
    db.kv_set("review_hits", sorted(h for h in hits if not h.startswith(inst["id"] + ":")))   # 新門檻重新判斷
    desc = lambda c: "、".join(f"{lab} {c[k]}{u}" for k, lab, u in RULES + ACC_RULES if k in c) or "不檢查"
    alerts.event("info", f"檢討條件修改：{inst['name']}　{desc(old)} → {desc(new)}"
                 + ("（恢復設定檔）" if body is None else f"（觸發後：{'暫停進場' if new['action'] == 'pause' else '只通知'}）"),
                 inst["id"], notify=False)
    return new


def _account_dd(instances, cfg):
    """所有策略（含停用的）已平倉交易照出場時間合併，算美元的最大回撤"""
    since = _since(cfg)
    ev = []
    for inst in instances:
        for t in recon.live_trades(int(inst["magic"])):
            if t["exit_ts"] is not None and t["entry_ts"] >= since:
                ev.append((t["exit_ts"], t["profit"] + t["swap"] + t["commission"]))
    ev.sort()
    eq = peak = dd = 0.0
    for _, v in ev:
        eq += v; peak = max(peak, eq); dd = max(dd, peak - eq)
    return {"n": len(ev), "net_money": round(eq, 2), "max_dd_money": round(dd, 2), "cur_dd_money": round(peak - eq, 2)}


def status(instances):
    res = []
    cfg, src = get_cfg(ACC)
    if True:                                   # 帳戶合計永遠列出（可以從網頁設定）
        configured = "max_dd_money" in cfg
        m = _account_dd(instances, cfg) if configured else None
        rules = []
        if configured:
            v, lim = m["max_dd_money"], cfg["max_dd_money"]
            rules = [{"key": "max_dd_money", "label": "帳戶已實現回撤", "unit": " 美元", "limit": lim, "value": v,
                      "hit": v > lim, "near": v <= lim and v >= lim * 0.8}]
        res.append({**ACC, "configured": configured, "source": src, "cfg": cfg, "file_cfg": _file_cfg(ACC),
                    "action": cfg.get("action", "pause"), "basis": cfg.get("basis", ""), "since": cfg.get("since"),
                    "metrics": m, "rules": rules, "form": _form(ACC_ID)})
    for inst in instances:
        if not inst.get("enabled", True):
            continue
        cfg, src = get_cfg(inst)
        configured = any(k in cfg for k in FIELDS)
        m = metrics(_trades(inst, cfg)) if configured else None
        res.append({"id": inst["id"], "name": inst["name"], "magic": inst["magic"], "configured": configured, "source": src,
                    "cfg": cfg, "file_cfg": inst.get("review") or {}, "action": cfg.get("action", "pause"),
                    "basis": cfg.get("basis", ""), "since": cfg.get("since"), "metrics": m,
                    "rules": evaluate(cfg, m) if configured else [], "form": _form(inst["id"])})
    return res


def check(instances, now=None):
    """定期跑：新觸發的條件 → 提醒（＋暫停進場）；已解除的條件 → 清掉，之後可以再觸發"""
    now = int(now or time.time())
    hits = set(db.kv_get("review_hits", []) or [])
    fired = []
    enabled_ids = [i["id"] for i in instances if i.get("enabled", True)]
    for r in status(instances):
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
                if r["id"] == ACC_ID:
                    paused.update(enabled_ids)
                    msg += "已自動暫停「所有策略」進場（持倉照常出場）。"
                else:
                    paused.add(r["id"])
                    msg += "已自動暫停進場（持倉照常出場）。"
                db.kv_set("paused", sorted(paused))
            msg += "先查原因：行情型態變了、成交品質變差、還是程式問題；不要急著重新調參數。"
            if r["basis"]:
                msg += f"\n依據：{r['basis']}"
            alerts.event("error", msg, r["id"])
    db.kv_set("review_hits", sorted(hits))
    db.kv_set("review_at", now)
    return fired
