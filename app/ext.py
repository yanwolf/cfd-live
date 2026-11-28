"""外部訊號策略（instances.json 裡 strategy="external"）：策略在別的服務跑，這裡只負責在 MT5 上照著做。

第一個用到的是黃金（gold-scalper 的 lab 15 分K 纏論引擎）。對方的訊號用幣安 XAUUSDT 算、自己管停損與移動停損，
這裡的目的是**量出換到 MT5 之後的真實差異**：點差、滑價、延遲、幣安和券商的報價差。

做法是「對齊狀態」而不是「轉發事件」：
  · 對方每 20 秒（以及部位一有變化）POST 一次「我現在的部位」到 /ext/<source>：
      position = null 或 {key, direction, entry_time, entry_price, sl_price, sl_points, ...}，加上最近幾筆已平倉的模擬單
  · EA 每次同步（1–2 秒）這裡就比一次：
      - 對方有部位、MT5 沒有、這個 key 沒做過、訊號還新鮮（max_entry_delay_sec 內）→ 市價進場
      - MT5 有部位、對方已經空手或換成另一個 key → 市價平倉
      - 對方太久沒回報（stale_sec）→ 不進新倉、不主動平倉，發一次警示（持倉的安全停損在券商端）
    重啟、重送、漏一次都沒關係：狀態對了就不會重複下單（做過的 key 記在 done_keys）。
  · 進場時附「安全停損」：進場報價 ∓ sl_points × safety_mult（預設 2 倍）。只防斷線，平常碰不到；
    停損、移動停損都由對方決定（對方平倉 → 這裡平倉），兩邊的出場時機才對得起來。
  · 訊號太晚才收到（例如這邊或 EA 斷線）就不追：記成「略過：太晚」，不會在不同價位補進一筆不一樣的單。

每一筆記在 ext_trades（key 為主鍵）：下單當下的買賣報價、成交價、對方的幣安價格與模擬損益，/api/ext 算對照。
"""
import json
import time
from datetime import datetime

from . import alerts, db, timeconv as T

DONE_KEEP = 300


def _ts(iso):
    try:
        return int(datetime.fromisoformat(str(iso)).timestamp())
    except (TypeError, ValueError):
        return None


def _state(inst_id):
    return db.kv_get(f"st:{inst_id}", {}) or {}


def _set_state(inst_id, st):
    db.kv_set(f"st:{inst_id}", st)


def desired(inst_id):
    return db.kv_get(f"ext:{inst_id}") or {}


def trade_get(key):
    r = db.q("SELECT data FROM ext_trades WHERE key=?", (key,))
    return json.loads(r[0][0]) if r else None


def trade_put(inst_id, key, data):
    db.ex("INSERT INTO ext_trades(key, inst, ts, data) VALUES (?,?,?,?) "
          "ON CONFLICT(key) DO UPDATE SET data=excluded.data, ts=excluded.ts",
          (key, inst_id, int(data.get("signal_ts") or time.time()), json.dumps(data, ensure_ascii=False)))


def match_instance(instances, payload):
    """對方送來的 engine_id／source 對到哪一支外部策略"""
    eng = payload.get("engine_id")
    src = payload.get("source")
    for i in instances:
        if i.get("strategy") == "external" and i.get("engine_id") == eng and (not i.get("source") or i.get("source") == src):
            return i
    return None


def receive(inst, payload, now=None):
    """存下對方的最新狀態，順便把已平倉的模擬單補進對照表"""
    now = int(now or time.time())
    seq = int(payload.get("seq") or 0)
    old = desired(inst["id"])
    if old and int(old.get("seq") or 0) > seq and old.get("boot") == payload.get("boot"):
        return {"ok": True, "ignored": "older seq"}           # 網路亂序：同一次開機裡舊的不蓋新的
    d = {"received_at": now, "seq": seq, "boot": payload.get("boot"), "position": payload.get("position"),
         "price": payload.get("price"), "engine_id": payload.get("engine_id"), "source": payload.get("source"),
         "settings": payload.get("settings")}
    db.kv_set(f"ext:{inst['id']}", d)
    for t in payload.get("recent_closed") or []:
        key = t.get("key")
        if not key:
            continue
        cur = trade_get(key)
        if cur is None:
            continue                       # 這邊沒做過的單（服務還沒接上之前的）不放進對照表
        if cur.get("sim_exit_price") is None:
            cur.update({"sim_exit_price": t.get("exit_price"), "sim_exit_time": t.get("exit_time"),
                        "sim_pnl_points": t.get("pnl_points"), "sim_exit_reason": t.get("exit_reason")})
            trade_put(inst["id"], key, cur)
    return {"ok": True}


def _tick(snap, sym):
    t = (snap.get("ticks") or {}).get(sym) or {}
    bid, ask = t.get("bid"), t.get("ask")
    return (float(bid), float(ask)) if bid and ask else (None, None)


def _market_closed(snap, sym, now, max_age=300):
    """MT5 這個商品超過 5 分鐘沒有新報價 = 休市（週末、每日休盤）。
    外部訊號來自幣安 XAUUSDT（24/7），週末也會換倉；這時候送單只會一直回「休市」，所以先不送，開盤再做"""
    t = (snap.get("ticks") or {}).get(sym) or {}
    if not t.get("time"):
        return False
    return now - T.srv2utc(int(t["time"])) > max_age


def _mark_done(st, key):
    done = st.setdefault("done_keys", [])
    if key not in done:
        done.append(key)
    del done[:-DONE_KEEP]


def _close_deal(magic, ticket):
    """這筆持倉的平倉成交（可能分好幾筆，取加權價）"""
    rows = db.q("SELECT data FROM deals WHERE magic=? AND entry=1 ORDER BY ticket DESC LIMIT 20", (int(magic),))
    vol = px = profit = swap = comm = 0.0
    ts = None
    for r in rows:
        d = json.loads(r[0])
        if d.get("position") != ticket:
            continue
        v = float(d.get("volume") or 0)
        vol += v
        px += float(d.get("price") or 0) * v
        profit += float(d.get("profit") or 0)
        swap += float(d.get("swap") or 0)
        comm += float(d.get("commission") or 0)
        ts = max(ts or 0, int(d.get("time_utc") or 0))
    if vol <= 0:
        return None
    return {"price": px / vol, "profit": profit, "swap": swap, "commission": comm, "ts": ts}


def _entry_commission(magic, ticket):
    rows = db.q("SELECT data FROM deals WHERE magic=? AND entry=0 ORDER BY ticket DESC LIMIT 20", (int(magic),))
    for r in rows:
        d = json.loads(r[0])
        if d.get("position") == ticket:
            return float(d.get("commission") or 0)
    return 0.0


def process(inst, now, snap, paused=False):
    """跟 live.process_instance 同一個位置呼叫；回傳新增的動作數"""
    from .live import new_action          # 避免循環匯入
    st = _state(inst["id"])
    st.setdefault("first_seen", int(now))
    sym, magic = inst["broker_symbol"], inst["magic"]
    poss = [p for p in snap.get("positions", []) if p["magic"] == magic and p["symbol"] == sym]
    if len(poss) > 1:
        st["paused_reason"] = f"同一個 magic 有 {len(poss)} 筆持倉"
        _set_state(inst["id"], st)
        return 0
    pos = poss[0] if poss else None
    n_new = 0
    want = desired(inst["id"])
    age = now - int(want.get("received_at") or 0) if want else None
    stale = (not want) or age > int(inst.get("stale_sec", 180))
    tgt = (want.get("position") or None) if not stale else None
    bid, ask = _tick(snap, sym)

    # ---- 開倉被券商確認
    if pos and st.get("open_ticket") != pos["ticket"]:
        op = st.get("opening") or {}
        key = op.get("key") or f"unknown:{pos['ticket']}"
        tr = trade_get(key) or {"key": key}
        d = 1 if pos["type"] == 0 else -1
        q = op.get("quote_ask") if d > 0 else op.get("quote_bid")
        tr.update({"status": "open", "ticket": pos["ticket"], "fill_price": float(pos["price"]),
                   "fill_ts": int(pos.get("time_utc") or now), "volume": float(pos["volume"]),
                   "entry_slip": round(d * (float(pos["price"]) - q), 4) if q else None})
        trade_put(inst["id"], key, tr)
        st["open_ticket"], st["open_key"] = pos["ticket"], key
        st.pop("opening", None)
        alerts.event("trade", f"{inst['name']} 開倉 {'多' if d > 0 else '空'} {pos['volume']} 手 @ {pos['price']}"
                     + (f"（下單時報價 {q}，滑價 {tr['entry_slip']:+.2f}）" if q else ""), inst["id"])

    # ---- 平倉被券商確認（訊號平倉、安全停損、手動、緊急停止都算）
    if not pos and st.get("open_ticket"):
        ticket, key = st["open_ticket"], st.get("open_key")
        cd = _close_deal(magic, ticket)
        if cd is None and now - int(st.get("close_wait_since") or now) < 120:
            st.setdefault("close_wait_since", now)          # 平倉成交紀錄還沒同步過來：等一下
            _set_state(inst["id"], st)
            return n_new
        tr = trade_get(key) or {"key": key}
        d = tr.get("dir") or 0
        closing = st.get("closing") or {}
        q = closing.get("quote_bid") if d > 0 else closing.get("quote_ask")
        if cd:
            comm = (cd["commission"] or 0) + _entry_commission(magic, ticket)
            tr.update({"exit_price": cd["price"], "exit_ts": cd["ts"], "profit": cd["profit"], "swap": cd["swap"],
                       "commission": comm,
                       "exit_slip": round(-d * (cd["price"] - q), 4) if (q and d) else None,
                       "mt5_points": round(d * (cd["price"] - float(tr.get("fill_price") or 0)), 4) if d else None})
        tr["status"] = "closed"
        tr["close_by"] = closing.get("why") or "券商端(安全停損／手動／緊急停止)"
        trade_put(inst["id"], key, tr)
        for k in ("open_ticket", "open_key", "closing", "close_wait_since"):
            st.pop(k, None)
        alerts.event("trade", f"{inst['name']} 平倉 @ {cd['price'] if cd else '?'}"
                     + (f"，MT5 {tr.get('mt5_points'):+.2f} 點" if tr.get("mt5_points") is not None else "")
                     + f"（{tr['close_by']}）", inst["id"])

    # ---- 對方太久沒回報
    if stale:
        if want and not st.get("stale_alerted"):
            alerts.event("warn", f"{inst['name']}：{inst.get('source') or '對方'} 已 {age} 秒沒有回報部位，暫停進場（持倉的安全停損在券商端）",
                         inst["id"])
            st["stale_alerted"] = True
        _set_state(inst["id"], st)
        return n_new
    if st.pop("stale_alerted", None):
        alerts.event("info", f"{inst['name']}：對方恢復回報", inst["id"], notify=True)

    base = {"symbol": sym, "magic": magic}
    tgt_key = tgt.get("key") if tgt else None

    # ---- 有持倉：對方已經空手或換了一筆 → 平倉
    if pos:
        if tgt_key != st.get("open_key") and _market_closed(snap, sym, now):
            if not st.get("closed_hold"):
                alerts.event("warn", f"{inst['name']}：對方已平倉，但 MT5 {sym} 休市中，開盤後再平倉", inst["id"])
                st["closed_hold"] = now
            _set_state(inst["id"], st)
            return n_new
        st.pop("closed_hold", None)
        if tgt_key != st.get("open_key"):
            cl = st.get("closing") or {}
            if cl.get("ticket") != pos["ticket"] or now - int(cl.get("at") or 0) > 120:
                aid = new_action(inst["id"], "CLOSE", {**base, "ticket": pos["ticket"], "reason": "外部訊號平倉"})
                st["closing"] = {"ticket": pos["ticket"], "at": now, "aid": aid, "quote_bid": bid, "quote_ask": ask,
                                 "why": "訊號平倉" if not tgt_key else "訊號換倉", "sim_price": want.get("price")}
                n_new += 1
        _set_state(inst["id"], st)
        return n_new

    # ---- 空手：對方有新部位 → 進場
    if tgt_key and tgt_key not in (st.get("done_keys") or []):
        op = st.get("opening") or {}
        if op.get("key") == tgt_key:
            row = db.q("SELECT status, result FROM actions WHERE id=?", (int(op.get("aid") or 0),))
            status = row[0][0] if row else None
            if status == "failed" or (status in ("done",) and now - int(op.get("at") or now) > 120) \
                    or (status in ("queued", "sent") and now - int(op.get("at") or now) > 600):
                tr = trade_get(tgt_key) or {"key": tgt_key}
                tr.update({"status": "skipped", "skip": "下單失敗" if status == "failed" else "送出後沒有成交"})
                trade_put(inst["id"], tgt_key, tr)
                _mark_done(st, tgt_key)
                st.pop("opening", None)
            _set_state(inst["id"], st)
            return n_new
        d = 1 if tgt.get("direction") == "bullish" else -1
        sig_ts = _ts(tgt.get("entry_time")) or now
        tr = {"key": tgt_key, "dir": d, "signal_ts": sig_ts, "sim_entry_price": tgt.get("entry_price"),
              "sim_sl_points": tgt.get("sl_points"), "entry_reason": tgt.get("reason")}
        block = paused or bool(st.get("paused_reason"))
        late = now - sig_ts > int(inst.get("max_entry_delay_sec", 300))
        shut = _market_closed(snap, sym, now)
        if block or late or shut:
            tr.update({"status": "skipped", "skip": "暫停進場" if block else ("MT5 休市" if shut else f"收到時已過 {now - sig_ts} 秒，不追")})
            trade_put(inst["id"], tgt_key, tr)
            _mark_done(st, tgt_key)
            if late and not block and not shut:
                alerts.event("warn", f"{inst['name']}：訊號 {tgt_key} 收到時已過 {now - sig_ts} 秒，不追", inst["id"])
            _set_state(inst["id"], st)
            return n_new
        if bid is None:
            _set_state(inst["id"], st)               # 還沒有報價（EA 剛連上）：下一次同步再看
            return n_new
        slp = float(tgt.get("sl_points") or 0)
        q = ask if d > 0 else bid
        safety = round(q - d * slp * float(inst.get("safety_mult", 2.0)), 2) if slp > 0 else 0.0
        aid = new_action(inst["id"], "MARKET", {**base, "side": "BUY" if d > 0 else "SELL", "volume": inst["lots"],
                                                 "sl": safety, "tp": 0.0, "reason": "外部訊號進場",
                                                 "comment": f"GOLD{sig_ts}"})
        tr.update({"status": "sent", "quote_bid": bid, "quote_ask": ask, "spread": round(ask - bid, 4),
                   "sent_ts": now, "latency_signal_to_send": now - sig_ts, "safety_sl": safety,
                   "basis": round((bid + ask) / 2 - float(tgt.get("entry_price") or 0), 4) if tgt.get("entry_price") else None})
        trade_put(inst["id"], tgt_key, tr)
        st["opening"] = {"key": tgt_key, "aid": aid, "at": now, "quote_bid": bid, "quote_ask": ask}
        _mark_done(st, tgt_key)
        n_new += 1
    _set_state(inst["id"], st)
    return n_new


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def report(inst, limit=200):
    """對照表：每筆的模擬 vs MT5，加上平均點差、滑價、延遲"""
    rows = db.q("SELECT data FROM ext_trades WHERE inst=? ORDER BY ts DESC LIMIT ?", (inst["id"], int(limit)))
    trades = [json.loads(r[0]) for r in rows]
    closed = [t for t in trades if t.get("status") == "closed" and t.get("mt5_points") is not None]
    both = [t for t in closed if t.get("sim_pnl_points") is not None]
    cost = lambda t: float(t.get("swap") or 0) + float(t.get("commission") or 0)
    lots = float(inst.get("lots") or 0.01)
    oz = lots * float(inst.get("contract_size", 100))
    summary = {
        "trades": len(closed), "matched": len(both),
        "skipped": sum(1 for t in trades if t.get("status") == "skipped"),
        "open": sum(1 for t in trades if t.get("status") in ("sent", "open")),
        "mt5_points": round(sum(t["mt5_points"] for t in closed), 2),
        "mt5_usd": round(sum(float(t.get("profit") or 0) + cost(t) for t in closed), 2),
        "swap_commission_usd": round(sum(cost(t) for t in closed), 2),
        "sim_points_matched": round(sum(float(t["sim_pnl_points"]) for t in both), 2),
        "mt5_points_matched": round(sum(t["mt5_points"] for t in both), 2),
        "avg_diff_points": _avg([t["mt5_points"] - float(t["sim_pnl_points"]) for t in both]),
        "avg_spread": _avg([t.get("spread") for t in trades]),
        "avg_entry_slip": _avg([t.get("entry_slip") for t in trades]),
        "avg_exit_slip": _avg([t.get("exit_slip") for t in closed]),
        "avg_latency_sec": _avg([(t.get("fill_ts") or 0) - t["signal_ts"] for t in trades if t.get("fill_ts") and t.get("signal_ts")]),
        "avg_basis": _avg([t.get("basis") for t in trades]),
        "oz_per_trade": oz,
    }
    want = desired(inst["id"])
    return {"id": inst["id"], "name": inst["name"], "source": inst.get("source"), "engine_id": inst.get("engine_id"),
            "desired": want, "desired_age": int(time.time()) - int(want.get("received_at") or 0) if want else None,
            "summary": summary, "trades": trades}
