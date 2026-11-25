"""EA 同步：收下 EA 的回報（K 棒、持倉、掛單、成交、上次動作的結果），跑策略，回傳要執行的動作。

回傳格式是純文字、一行一筆（MQL5 解析 JSON 很麻煩，用 | 分隔最穩）：
  OK|<伺服器 UTC 秒>|<kill 0/1>
  S|<券商商品代碼>|<從這個伺服器時間之後的 1 分 K 傳過來>
  A|<id>|<op>|<symbol>|<magic>|<side>|<volume>|<price>|<sl>|<tp>|<ticket>|<comment>
op：MARKET（side=BUY/SELL）、PENDING（side=BUY_STOP/SELL_STOP/BUY_LIMIT/SELL_LIMIT）、
    CANCEL（ticket）、MODIFY（ticket, sl, tp）、CLOSE（ticket）
"""
import json
import time
import traceback

import numpy as np

from . import alerts, config, db, ext, live, timeconv as T

RETRY_CODES = {10018}          # TRADE_RETCODE_MARKET_CLOSED：休市中，一分鐘後再試
OK_CODES = {10008, 10009, 10010}   # PLACED、DONE、DONE_PARTIAL


def _err_once(key, msg, inst=None, every=600):
    last = db.kv_get(f"err:{key}", 0)
    if time.time() - last > every:
        db.kv_set(f"err:{key}", time.time())
        alerts.event("error", msg, inst)


def ingest(p, now):
    acct = {k: p.get(k) for k in ("login", "server", "company", "connected", "trade_allowed", "balance", "equity",
                                   "margin_free", "currency", "trade_mode", "ea_version")}
    acct["server_time"] = p.get("time")
    db.kv_set("account", acct)
    db.kv_set("ea_last", now)

    positions = []
    for x in p.get("positions") or []:
        x = dict(x)
        x["time_utc"] = T.srv2utc(x.get("time", 0))
        positions.append(x)
    orders = [dict(x) for x in (p.get("orders") or [])]
    ticks = p.get("ticks") or {}
    snap = {"positions": positions, "orders": orders, "ticks": ticks, "at": now}
    db.kv_set("snap", snap)

    # 1 分 K
    for sym, rows in (p.get("bars") or {}).items():
        if not rows:
            continue
        a = np.asarray(rows, dtype=float)
        utc = T.srv2utc_arr(a[:, 0].astype("int64"))
        db.exmany("INSERT OR REPLACE INTO bars(symbol, ts, o, h, l, c, v) VALUES (?,?,?,?,?,?,?)",
                  [(sym, int(u), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for u, r in zip(utc, a)])

    # 成交
    for d in p.get("deals") or []:
        d = dict(d)
        d["time_utc"] = T.srv2utc(d.get("time", 0))
        db.ex("INSERT OR IGNORE INTO deals(ticket, ts, magic, symbol, entry, data) VALUES (?,?,?,?,?,?)",
              (int(d["ticket"]), int(d["time_utc"]), int(d.get("magic", 0)), d.get("symbol"), int(d.get("entry", -1)),
               json.dumps(d)))

    # 上次動作的結果
    for r in p.get("results") or []:
        aid = int(r.get("id", 0))
        row = db.q("SELECT inst, op, payload, status FROM actions WHERE id=?", (aid,))
        if not row:
            continue
        inst, op, payload, status = row[0]
        code = int(r.get("retcode", 0))
        ok = bool(r.get("ok")) or code in OK_CODES
        db.ex("UPDATE actions SET status=?, result=? WHERE id=?", ("done" if ok else "failed", json.dumps(r), aid))
        if ok:
            continue
        pl = json.loads(payload)
        if code in RETRY_CODES and op in ("PENDING", "MODIFY", "MARKET", "CLOSE"):
            live.new_action(inst, op, pl, not_before=now + 60)      # 保留原 comment，開倉後才對得回進場參考價
            continue
        if op == "CANCEL" and code in (10013, 10036):     # 掛單已不存在（剛好成交或已撤）：不算錯
            continue
        alerts.event("error", f"動作失敗：{op} {pl.get('symbol')} retcode={code} {r.get('msg', '')}", inst)
    return snap


def checks(snap, now, instances):
    """對帳：部位數量、手數、保護單"""
    magics = {i["magic"]: i for i in config_instances()}       # 停用的也算已知（它們的持倉由 retire_actions 處理）
    for i in instances:
        poss = [x for x in snap["positions"] if x["magic"] == i["magic"]]
        if len(poss) > 1:
            _err_once(f"dup:{i['id']}", f"{i['name']} 同一個 magic 有 {len(poss)} 筆持倉，已暫停進場，請檢查 MT5", i["id"])
        for x in poss:
            if abs(float(x["volume"]) - float(i["lots"])) > 1e-9:
                _err_once(f"vol:{i['id']}", f"{i['name']} 持倉手數 {x['volume']} 與設定 {i['lots']} 不符", i["id"])
            if not x.get("sl") and now - x["time_utc"] > 180:
                _err_once(f"nosl:{x['ticket']}", f"{i['name']} 持倉 {x['ticket']} 超過 3 分鐘沒有停損", i["id"])
    for x in snap["positions"]:
        if x["magic"] and x["magic"] not in magics:
            _err_once(f"unk:{x['magic']}", f"MT5 上有未知 magic {x['magic']} 的持倉（{x['symbol']}），系統不會管理它")


def kill_actions(snap, instances):
    sent = set(tuple(x) for x in db.kv_get("kill_sent", []))
    mine = {i["magic"]: i for i in instances}
    for x in snap["positions"]:
        if x["magic"] in mine and ("p", x["ticket"]) not in sent:
            live.new_action(mine[x["magic"]]["id"], "CLOSE", {"ticket": x["ticket"], "symbol": x["symbol"], "magic": x["magic"],
                                                              "reason": "緊急停止"})
            sent.add(("p", x["ticket"]))
    for o in snap["orders"]:
        if o["magic"] in mine and ("o", o["ticket"]) not in sent:
            live.new_action(mine[o["magic"]]["id"], "CANCEL", {"ticket": o["ticket"], "symbol": o["symbol"], "magic": o["magic"]})
            sent.add(("o", o["ticket"]))
    db.kv_set("kill_sent", [list(x) for x in sent])


def retire_actions(snap, retired):
    """instances.json 裡 enabled=false 的策略：剩下的持倉平掉、掛單撤掉，之後不再進場"""
    sent = set(tuple(x) for x in db.kv_get("retire_sent", []))
    mine = {i["magic"]: i for i in retired}
    for x in snap["positions"]:
        if x["magic"] in mine and ("p", x["ticket"]) not in sent:
            live.new_action(mine[x["magic"]]["id"], "CLOSE", {"ticket": x["ticket"], "symbol": x["symbol"], "magic": x["magic"],
                                                              "reason": "策略停用"})
            alerts.event("warn", f"{mine[x['magic']]['name']} 已停用，平掉剩下的持倉 {x['ticket']}", mine[x["magic"]]["id"])
            sent.add(("p", x["ticket"]))
    for o in snap["orders"]:
        if o["magic"] in mine and ("o", o["ticket"]) not in sent:
            live.new_action(mine[o["magic"]]["id"], "CANCEL", {"ticket": o["ticket"], "symbol": o["symbol"], "magic": o["magic"]})
            sent.add(("o", o["ticket"]))
    db.kv_set("retire_sent", [list(x) for x in sent][-500:])


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(round(v, 6))
    return str(v).replace("|", "/").replace("\n", " ")


def handle(p, now=None):
    now = int(now or time.time())
    instances = [i for i in config_instances() if i.get("enabled", True)]
    retired = [i for i in config_instances() if not i.get("enabled", True)]
    snap = ingest(p, now)
    kill = bool(db.kv_get("kill", False))
    if kill:
        kill_actions(snap, instances + retired)
    else:
        retire_actions(snap, retired)
        paused = set(db.kv_get("paused", []))
        for inst in instances:
            try:
                if inst.get("strategy") == "external":
                    ext.process(inst, now, snap, paused=inst["id"] in paused)
                else:
                    live.process_instance(inst, now, snap, paused=inst["id"] in paused)
            except Exception as e:
                _err_once(f"proc:{inst['id']}", f"{inst['name']} 策略執行錯誤：{e}\n{traceback.format_exc()[-600:]}", inst["id"])
        checks(snap, now, instances)

    lines = [f"OK|{now}|{1 if kill else 0}"]
    syms = sorted({i["broker_symbol"] for i in instances})
    ext_only = {i["broker_symbol"] for i in instances if i.get("strategy") == "external"} - \
        {i["broker_symbol"] for i in instances if i.get("strategy") != "external"}
    for sym in syms:
        r = db.q("SELECT MAX(ts) FROM bars WHERE symbol=?", (sym,))
        last = r[0][0] if r and r[0][0] else None
        days = 2 if sym in ext_only else config.BACKFILL_DAYS      # 外部訊號只要報價，不用回補 60 天 K 棒
        since = T.utc2srv(last) if last else T.utc2srv(now - days * 86400)
        lines.append(f"S|{sym}|{since}")
    rows = db.q("SELECT id, op, payload FROM actions WHERE status='queued' AND not_before<=? ORDER BY id", (now,))
    for aid, op, payload in rows:
        pl = json.loads(payload)
        lines.append("A|" + "|".join(_fmt(x) for x in (aid, op, pl.get("symbol"), pl.get("magic"), pl.get("side", ""),
                                                        pl.get("volume", 0.0), pl.get("price", 0.0), pl.get("sl", 0.0),
                                                        pl.get("tp", 0.0), pl.get("ticket", 0), pl.get("comment", ""))))
        db.ex("UPDATE actions SET status='sent', sent_at=? WHERE id=?", (now, aid))
    return "\n".join(lines) + "\n"


_inst_cache = {"at": 0, "items": None}


def config_instances():
    if _inst_cache["items"] is None or time.time() - _inst_cache["at"] > 30:
        _inst_cache["items"] = config.load_instances()
        _inst_cache["at"] = time.time()
    return _inst_cache["items"]
