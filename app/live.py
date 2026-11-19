"""策略執行：K 棒收盤 → 呼叫回測同一份 on_bar() → 翻成 MT5 動作。

和回測引擎的對應：
  · 每根 N 分 K 收盤（用券商的 1 分 K 組，切法跟回測完全一樣）呼叫一次 on_bar()
  · 部位狀態（mp、進場價、最大浮盈、當日進場次數、停損單位）以券商實際持倉與成交紀錄為準
  · 策略掛的單只對「下一根 K」有效：每根 K 收盤先取消舊掛單，再掛新的（MC 慣例）
  · 進場 stop／limit → 券商掛單；掛單價已被穿過 → 直接市價（回測裡等於下一根開盤跳空成交）
  · 出場 stop → 持倉 SL、出場 limit → 持倉 TP、出場市價 → 平倉；SL／TP 已被穿過 → 直接平倉
  · 進場時另外帶一張「安全停損」（safety_units 個單位），只防斷線、極端行情，平常碰不到；
    下一根 K 收盤後就換成策略自己的停損
  · 週末 WKX、不過夜 DAYX：依券商每週收盤時間、每日轉倉（美東 17:00）換算
"""
import json
import re
import time
from datetime import timedelta
from collections import Counter

import numpy as np

from . import alerts, config, db, timeconv as T
from .core import bars as B
from .core.strategies import REGISTRY

MIN_M1 = 300


# ---------------------------------------------------------------- 資料
def load_m1(broker_symbol, since_utc):
    rows = db.q("SELECT ts, o, h, l, c, v FROM bars WHERE symbol=? AND ts>=? ORDER BY ts", (broker_symbol, int(since_utc)))
    if not rows:
        return None
    a = np.array(rows, dtype=float)
    n = len(a)
    return {"ts": a[:, 0].astype("int64"), "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4],
            "v": a[:, 5], "sp": np.full(n, np.nan)}


def _keys(symbol, ts, minutes, align):
    """與 bars.build 完全相同的 K 棒分組鍵"""
    ts = np.asarray(ts, dtype="int64")
    z = np.zeros(len(ts))
    m = B.minute_frame({"ts": ts, "o": z, "h": z, "l": z, "c": z, "v": z, "sp": z}, symbol)
    if align == "clock":
        return m["tday"] * 10000 + m["day_mins"] // minutes
    return (m["tday"] % 1000) + 1000 * (m["ses_mins"] // minutes + 2000 * m["ses_key"])


def last_bar_end(symbol, b, m, minutes, align):
    """最後一根 K 的結束時間：時槽終點，或更早遇到的時段／交易日切換點"""
    end = int(b.ts_end[-1])
    last = int(m["ts"][-1])
    cand = np.arange(last + 60, end, 60, dtype="int64")
    if len(cand):
        k = _keys(symbol, np.concatenate([[last], cand]), minutes, align)
        diff = np.flatnonzero(k[1:] != k[0])
        if len(diff):
            end = int(cand[diff[0]])
    return end


# ---------------------------------------------------------------- 週末、轉倉
def week_close_tow(broker_symbol, now):
    """從歷史資料找出這個商品「每週最後一分鐘」的美東星期與時刻（取眾數）。
    只有真的從資料算出來才快取一天；資料不夠時用預設值但不快取，等資料補齊再算。"""
    key = f"wclose:{broker_symbol}"
    cached = db.kv_get(key)
    if cached and 0 <= now - cached.get("at", 0) < 86400:
        return cached["tow"]
    rows = db.q("SELECT ts FROM bars WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts", (broker_symbol, int(now) - 120 * 86400, int(now)))
    tow = None
    if len(rows) > 1000:
        ts = np.array([r[0] for r in rows], dtype="int64")
        gaps = np.flatnonzero(np.diff(ts) > 40 * 3600)
        vals = []
        for g in gaps:
            et = T.et_of(ts[g])
            vals.append(et.weekday() * 1440 + et.hour * 60 + et.minute)
        if vals:
            tow = Counter(vals).most_common(1)[0][0]
    if tow is None:
        return 4 * 1440 + 16 * 60 + 59          # 預設：週五 16:59 美東（不快取）
    db.kv_set(key, {"tow": tow, "at": int(now)})
    return tow


def next_week_last_minute(now_utc, tow):
    """這週（若已收盤超過一小時則下週）的「每週最後一分鐘」，UTC 秒"""
    et = T.et_of(now_utc).replace(tzinfo=None)
    monday = (et - timedelta(days=et.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    for add in (0, 7):
        wall = monday + timedelta(days=add, minutes=tow)
        cand = int(wall.replace(tzinfo=T.NY).timestamp())
        if cand + 3600 > now_utc:
            return cand
    return None


def in_week_cut(inst, now_utc):
    wkx = int(params_of(inst).get("WKX", 15))
    if wkx <= 0:
        return False
    last = next_week_last_minute(now_utc, week_close_tow(inst["broker_symbol"], now_utc))
    return last is not None and last - wkx * 60 + 60 <= now_utc < last + 3600


def in_day_cut(inst, now_utc):
    dayx = int(params_of(inst).get("DAYX", 0))
    if dayx <= 0:
        return False
    et = T.et_of(now_utc)
    roll = et.replace(hour=17, minute=0, second=0, microsecond=0)
    return roll.timestamp() - dayx * 60 <= now_utc < roll.timestamp()


# ---------------------------------------------------------------- 狀態
def params_of(inst):
    cls = REGISTRY[inst["strategy"]]
    p = cls.all_inputs()
    p.update(inst.get("params") or {})
    return p


def get_state(inst_id):
    return db.kv_get(f"st:{inst_id}", {}) or {}


def set_state(inst_id, st):
    db.kv_set(f"st:{inst_id}", st)


def new_action(inst_id, op, payload, not_before=0):
    aid = db.ex("INSERT INTO actions(inst, op, payload, status, created, not_before) VALUES (?,?,?,?,?,?)",
                (inst_id, op, "{}", "queued", int(time.time()), int(not_before)))
    payload = dict(payload)
    payload.setdefault("comment", f"{'L' if op in ('MARKET', 'PENDING') else 'X'}{aid}")
    db.ex("UPDATE actions SET payload=? WHERE id=?", (json.dumps(payload), aid))
    return aid


def drop_queued(inst_id):
    """新 K 棒收盤時，上一根還沒送出（例如休市重試中）的動作作廢"""
    db.ex("UPDATE actions SET status='expired' WHERE inst=? AND status='queued'", (inst_id,))


def tday_of(symbol, ts_utc_list):
    if not ts_utc_list:
        return []
    ts = np.asarray(ts_utc_list, dtype="int64")
    z = np.zeros(len(ts))
    m = B.minute_frame({"ts": ts, "o": z, "h": z, "l": z, "c": z, "v": z, "sp": z}, symbol)
    return list(m["tday"])


# ---------------------------------------------------------------- 主流程
def rp(inst, x):
    """對齊商品的最小跳動單位（instances.json 的 tick_size），避免每根 K 都送一樣的修改"""
    if not x:
        return 0.0
    t = float(inst.get("tick_size") or 0.01)
    return round(round(float(x) / t) * t, 10)


def tick_price(snap, inst, side):
    t = (snap.get("ticks") or {}).get(inst["broker_symbol"]) or {}
    v = t.get("ask" if side > 0 else "bid")
    return float(v) if v else None


def handle_position_changes(inst, st, pos, pend):
    """偵測新開倉／平倉：記下進場參考價與停損單位、取消多餘掛單、發通知"""
    cur = pos["ticket"] if pos else None
    old = st.get("pos_ticket")
    if cur and cur != old:
        m = re.search(r"L(\d+)", pos.get("comment") or "")
        info = (st.get("pend") or {}).get(m.group(1)) if m else None
        st["pos_ticket"] = cur
        ref = (info or {}).get("ref")
        st["entry_ref"] = ref or pos["price"]
        st["entry_mkt"] = bool(info) and ref is None       # 市價進場：之後改用該分鐘開盤（bid）當參考價，跟回測一致
        st["entry_u"] = (info or {}).get("u")
        st["entry_utc"] = pos["time_utc"]
        st["pend"] = {}
        side = "多" if pos["type"] == 0 else "空"
        alerts.event("trade", f"{inst['name']} 開倉 {side} {pos['volume']} 手 @ {pos['price']}", inst["id"])
        for o in pend:                                  # 一根 K 只成交一次：其餘進場掛單取消
            new_action(inst["id"], "CANCEL", {"ticket": o["ticket"], "symbol": inst["broker_symbol"], "magic": inst["magic"]})
    if old and not cur:
        rows = db.q("SELECT data FROM deals WHERE magic=? AND entry=1 ORDER BY ticket DESC LIMIT 5", (inst["magic"],))
        pnl = None
        for r in rows:
            d = json.loads(r[0])
            if d.get("position") == old:
                pnl = (pnl or 0) + d.get("profit", 0) + d.get("swap", 0) + d.get("commission", 0)
        msg = f"{inst['name']} 平倉" + (f"，損益 {pnl:+.2f}" if pnl is not None else "")
        alerts.event("trade", msg, inst["id"])
        for k in ("pos_ticket", "entry_ref", "entry_u", "entry_utc", "entry_mkt"):
            st.pop(k, None)


def process_instance(inst, now, snap, paused=False):
    """回傳這次新增的動作數。snap：EA 最新回報（持倉、掛單、報價，時間已換成 UTC）"""
    st = get_state(inst["id"])
    st.setdefault("first_seen", int(now))          # 給「實盤 vs 回測」對帳當起算點
    poss = [p for p in snap.get("positions", []) if p["magic"] == inst["magic"] and p["symbol"] == inst["broker_symbol"]]
    pend = [o for o in snap.get("orders", []) if o["magic"] == inst["magic"] and o["symbol"] == inst["broker_symbol"]]
    if len(poss) > 1:
        st["paused_reason"] = f"同一個 magic 有 {len(poss)} 筆持倉"
        set_state(inst["id"], st)
        return 0
    pos = poss[0] if poss else None
    handle_position_changes(inst, st, pos, pend)
    n_new = 0

    # 週末／不過夜：時間到就平倉、撤單
    cut = in_week_cut(inst, now) or in_day_cut(inst, now)
    if cut:
        if pos and st.get("cut_sent") != pos["ticket"]:
            new_action(inst["id"], "CLOSE", {"ticket": pos["ticket"], "symbol": inst["broker_symbol"], "magic": inst["magic"],
                                             "reason": "週末出場" if in_week_cut(inst, now) else "轉倉前出場"})
            st["cut_sent"] = pos["ticket"]; n_new += 1
        for o in pend:
            if o["ticket"] not in (st.get("cut_cancel") or []):
                new_action(inst["id"], "CANCEL", {"ticket": o["ticket"], "symbol": inst["broker_symbol"], "magic": inst["magic"]})
                st.setdefault("cut_cancel", []).append(o["ticket"]); n_new += 1
    else:
        st.pop("cut_cancel", None)

    # K 棒收盤？（EA 每 1–2 秒同步一次，只有可能收盤的時間點才做完整計算）
    if now < st.get("next_check_at", 0):
        set_state(inst["id"], st)
        return n_new
    minutes = int(inst.get("minutes") or REGISTRY[inst["strategy"]].minutes)
    align = inst.get("align", "session")
    m = load_m1(inst["broker_symbol"], now - config.LOOKBACK_DAYS * 86400)
    if m is None or len(m["ts"]) < MIN_M1:
        set_state(inst["id"], st)
        return n_new
    # 資料還在補（第一次連線回補歷史、EA 斷線後追資料）就先不動作。
    # EA 1.01 起回報券商「最新一根已完成的 1 分 K」時間（last_m1）：我們的資料追上它就算完整，
    # 休市後重新開盤（例如日經 08:45）不會被誤判成資料落後。舊版 EA 沒有 last_m1，退回用報價時間判斷。
    tk = (snap.get("ticks") or {}).get(inst["broker_symbol"]) or {}
    if tk.get("last_m1"):
        if T.srv2utc(int(tk["last_m1"])) > int(m["ts"][-1]):
            set_state(inst["id"], st)
            return n_new
    elif tk.get("time"):
        tick_utc = T.srv2utc(int(tk["time"]))
        if tick_utc - int(m["ts"][-1]) > 180:
            set_state(inst["id"], st)
            return n_new
    m = B.minute_frame(m, inst["symbol"])
    b = B.build(m, minutes, align, symbol=inst["symbol"])
    i = b.n - 1
    end_last = last_bar_end(inst["symbol"], b, m, minutes, align)
    complete = now >= end_last + 2 and (int(m["ts"][-1]) >= end_last - 60 or now >= end_last + 120)
    if not complete:
        i -= 1
        st["next_check_at"] = end_last + 2
    else:
        st["next_check_at"] = end_last + 60
    if i < 5:
        set_state(inst["id"], st)
        return n_new
    bar_ts = int(b.ts_start[i])
    if st.get("last_bar_ts", 0) >= bar_ts:
        set_state(inst["id"], st)
        return n_new
    skipped = 0
    if st.get("last_bar_ts"):
        skipped = int(np.sum((b.ts_start > st["last_bar_ts"]) & (b.ts_start < bar_ts)))
    if skipped:
        alerts.event("warn", f"{inst['name']} 漏處理 {skipped} 根 K（可能 EA 斷線），只處理最新一根", inst["id"])

    # 呼叫策略
    cls = REGISTRY[inst["strategy"]]
    s = cls(inst.get("params") or {})
    s.b = b
    p = s.p
    for j in range(5, i):                       # 重播一次讓有狀態的策略（例如 TMFF 的 BS）跟回測一致
        s.i = j; s.mp = 0; s.orders = []
        s.on_bar()
    s.i = i
    s.orders = []
    s.u_now = s.avgrange(p["UNIT_N"]) if int(p.get("UNIT_MODE", 0)) == 0 else b.c[i] * 0.001
    bar_end = int(b.ts_end[i])
    if pos:
        d = 1 if pos["type"] == 0 else -1
        s.mp = d
        s.entryprice = float(st.get("entry_ref") or pos["price"])
        eu = st.get("entry_utc") or pos["time_utc"]
        if st.get("entry_u"):
            s.u = float(st["entry_u"])
        else:
            j = int(np.searchsorted(b.ts_start, eu, side="right")) - 1
            jj = max(j - 1, 0)
            s.u = float(B.avgrange(b, p["UNIT_N"])[jj]) if int(p.get("UNIT_MODE", 0)) == 0 else float(b.c[jj] * 0.001)
        em = eu - eu % 60
        mask = (m["ts"] > em) & (m["ts"] < bar_end)
        k0 = np.flatnonzero(m["ts"] == em)
        if st.get("entry_mkt") and len(k0):
            s.entryprice = float(m["o"][k0[0]])
        hi = list(m["h"][mask]) + ([m["c"][k0[0]]] if len(k0) else [])
        lo = list(m["l"][mask]) + ([m["c"][k0[0]]] if len(k0) else [])
        mfe = (max(hi) - s.entryprice) if d > 0 else (s.entryprice - min(lo)) if lo else 0.0
        s.maxprofit = max(0.0, float(mfe)) if hi else 0.0
    else:
        s.mp = 0
    dts = [r[0] for r in db.q("SELECT ts FROM deals WHERE magic=? AND entry=0 AND ts>=?", (inst["magic"], now - 4 * 86400))]
    tds = tday_of(inst["symbol"], dts)
    s.entries_today = int(sum(1 for t in tds if t == b.tday[i]))
    s.on_bar()

    # 翻成 MT5 動作
    drop_queued(inst["id"])
    for o in pend:
        new_action(inst["id"], "CANCEL", {"ticket": o["ticket"], "symbol": inst["broker_symbol"], "magic": inst["magic"]}); n_new += 1
    entries = [o for o in s.orders if o.action in ("buy", "sellshort")]
    exits = [o for o in s.orders if o.action in ("sell", "buytocover")]
    base = {"symbol": inst["broker_symbol"], "magic": inst["magic"]}
    close_px = float(b.c[i])
    if pos:
        d = 1 if pos["type"] == 0 else -1
        px = tick_price(snap, inst, -d) or close_px          # 多單看 bid、空單看 ask
        mk = [o for o in exits if o.kind == "market"]
        stops = [o.price for o in exits if o.kind == "stop"]
        lims = [o.price for o in exits if o.kind == "limit"]
        sl = rp(inst, (max(stops) if d > 0 else min(stops)) if stops else (pos.get("sl") or 0.0))
        tp = rp(inst, (min(lims) if d > 0 else max(lims)) if lims else (pos.get("tp") or 0.0))
        crossed = (d > 0 and ((sl and sl >= px) or (tp and tp <= px))) or (d < 0 and ((sl and sl <= px) or (tp and tp >= px)))
        if mk or crossed:
            reason = mk[0].label if mk else "出場價已穿過，市價平倉"
            new_action(inst["id"], "CLOSE", {**base, "ticket": pos["ticket"], "reason": reason}); n_new += 1
        elif abs(sl - rp(inst, pos.get("sl"))) > 1e-9 or abs(tp - rp(inst, pos.get("tp"))) > 1e-9:
            new_action(inst["id"], "MODIFY", {**base, "ticket": pos["ticket"], "sl": sl or 0.0, "tp": tp or 0.0}); n_new += 1
        if any((o.action == "sellshort") == (d > 0) for o in entries):
            alerts.event("warn", f"{inst['name']} 持倉中出現反向進場單（反手），第一版不支援，已略過", inst["id"], notify=False)
    else:
        # 跟回測一致：這根 K 只要碰到週末／轉倉的截止時段，就不掛下一根的進場單（不會把週五的單留到週日開盤）
        last_min = bar_end - 60
        block = paused or bool(st.get("paused_reason")) or in_week_cut(inst, now) or in_day_cut(inst, now) \
            or in_week_cut(inst, last_min) or in_day_cut(inst, last_min)
        if entries and not block:
            st["pend"] = {}
            su = float(inst.get("safety_units") or 0)
            for o in entries:
                side = 1 if o.action == "buy" else -1
                px = tick_price(snap, inst, side) or close_px
                ref = o.price if o.price is not None else close_px
                ebx = float(p.get("EBX", 0) or 0)
                if ebx > 0:      # 策略有 EBX：安全停損就是回測裡的進場 K 災難停損（AvgRange(200)×EBX）
                    safety = rp(inst, ref - side * ebx * float(B.avgrange(b, 200)[i]))
                else:
                    safety = rp(inst, ref - side * su * float(s.u_now)) if su > 0 else 0.0
                market = o.kind == "market" or (o.kind == "stop" and (o.price - px) * side <= 0) \
                    or (o.kind == "limit" and (o.price - px) * side >= 0)
                if market:
                    aid = new_action(inst["id"], "MARKET", {**base, "side": "BUY" if side > 0 else "SELL",
                                                             "volume": inst["lots"], "sl": safety, "tp": 0.0, "reason": o.label})
                    st["pend"][str(aid)] = {"ref": None, "u": float(s.u_now), "label": o.label}
                else:
                    typ = ("BUY_" if side > 0 else "SELL_") + ("STOP" if o.kind == "stop" else "LIMIT")
                    aid = new_action(inst["id"], "PENDING", {**base, "side": typ, "volume": inst["lots"], "price": rp(inst, o.price),
                                                              "sl": safety, "tp": 0.0, "reason": o.label})
                    st["pend"][str(aid)] = {"ref": float(o.price), "u": float(s.u_now), "label": o.label}
                n_new += 1
    st["last_bar_ts"] = bar_ts
    st["last_bar_end"] = bar_end
    st["last_close"] = close_px
    st["last_orders"] = [{"kind": o.kind, "action": o.action, "price": o.price, "label": o.label} for o in s.orders]
    st["u_now"] = float(s.u_now)
    st["processed_at"] = now
    set_state(inst["id"], st)
    return n_new
