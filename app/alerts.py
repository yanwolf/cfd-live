"""事件紀錄 + Telegram 通知（只通知實際下單與警示，不洗版）"""
import json
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

from . import config, db

LEVELS = ("info", "trade", "warn", "error")


def event(level, msg, inst=None, notify=None):
    db.ex("INSERT INTO events(ts, level, inst, msg) VALUES (?,?,?,?)", (int(time.time()), level, inst, msg))
    if notify is None:
        notify = level in ("trade", "warn", "error")
    if notify:
        prefix = {"trade": "🟡", "warn": "⚠️", "error": "🚨"}.get(level, "")
        tg(f"{prefix} {msg}")


# ---------------------------------------------------------------- Telegram（寄件匣：先寫進資料庫再送，失敗會重試）
MAX_TRIES = 30            # 退避 15 秒→最多 10 分鐘一次，大約重試 4 小時
_kick = threading.Event()
_worker = None
_wlock = threading.Lock()


def tg(text):
    if not (config.TG_BOT_TOKEN and config.TG_CHAT_ID):
        return
    db.ex("INSERT INTO tg_outbox(created, text) VALUES (?,?)", (int(time.time()), text[:3900]))
    _ensure_worker()
    _kick.set()


def _ensure_worker():
    global _worker
    with _wlock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True)
            _worker.start()


def _post(text):
    """送一則；回傳 (成功?, 錯誤訊息, 建議幾秒後重試)"""
    data = urllib.parse.urlencode({"chat_id": config.TG_CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage", data=data, timeout=15) as r:
            body = json.loads(r.read().decode() or "{}")
            return (bool(body.get("ok")), "" if body.get("ok") else str(body)[:300], None)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        retry = (body.get("parameters") or {}).get("retry_after")
        return (False, f"HTTP {e.code} {body.get('description', '')}"[:300], retry)
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}"[:300], None)


def flush(now=None):
    """把到期的待送訊息送出去；回傳這次成功幾則"""
    now = int(now or time.time())
    rows = db.q("SELECT id, created, text, tries FROM tg_outbox WHERE sent_at IS NULL AND gave_up=0 AND next_try<=? "
                "ORDER BY id LIMIT 20", (now,))
    ok_n = 0
    for mid, created, text, tries in rows:
        late = now - created
        body = text if late < 90 else f"{text}\n（延遲 {late // 60} 分鐘送達，原時間 {time.strftime('%m/%d %H:%M', time.gmtime(created + 8 * 3600))}）"
        ok, err, retry = _post(body)
        if ok:
            db.ex("UPDATE tg_outbox SET sent_at=?, tries=? WHERE id=?", (int(time.time()), tries + 1, mid))
            ok_n += 1
            continue
        tries += 1
        wait = int(retry) if retry else min(15 * 2 ** min(tries - 1, 6), 600)
        give_up = 1 if tries >= MAX_TRIES or (err.startswith("HTTP 400") or err.startswith("HTTP 401") or err.startswith("HTTP 403")) else 0
        db.ex("UPDATE tg_outbox SET tries=?, next_try=?, last_err=?, gave_up=? WHERE id=?", (tries, now + wait, err, give_up, mid))
        if tries == 1 or give_up:
            db.ex("INSERT INTO events(ts, level, inst, msg) VALUES (?,?,?,?)",
                  (int(time.time()), "warn", None, f"Telegram {'放棄重送' if give_up else '發送失敗，會自動重試'}：{err}"))
        break                       # 一則失敗就先停，保持順序，等下一輪
    return ok_n


def _loop():
    while True:
        _kick.wait(15)
        _kick.clear()
        try:
            flush()
        except Exception:
            pass


def tg_status():
    r = db.q("SELECT COUNT(*), MIN(created), MAX(last_err) FROM tg_outbox WHERE sent_at IS NULL AND gave_up=0")[0]
    g = db.q("SELECT COUNT(*) FROM tg_outbox WHERE gave_up=1 AND created>?", (int(time.time()) - 86400,))[0][0]
    return {"enabled": bool(config.TG_BOT_TOKEN and config.TG_CHAT_ID), "pending": r[0], "oldest": r[1], "last_err": r[2], "gave_up_24h": g}


def recent(limit=100):
    rows = db.q("SELECT id, ts, level, inst, msg FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [{"id": r[0], "ts": r[1], "level": r[2], "inst": r[3], "msg": r[4]} for r in rows]
