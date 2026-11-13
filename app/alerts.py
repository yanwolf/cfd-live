"""事件紀錄 + Telegram 通知（只通知實際下單與警示，不洗版）"""
import json
import threading
import time
import urllib.parse
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


def tg(text):
    if not (config.TG_BOT_TOKEN and config.TG_CHAT_ID):
        return

    def _send():
        try:
            data = urllib.parse.urlencode({"chat_id": config.TG_CHAT_ID, "text": text[:4000]}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage", data=data, timeout=10)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def recent(limit=100):
    rows = db.q("SELECT id, ts, level, inst, msg FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [{"id": r[0], "ts": r[1], "level": r[2], "inst": r[3], "msg": r[4]} for r in rows]
