"""Telegram 寄件匣：失敗會重試、保持順序、延遲送達會註明、400 類錯誤直接放棄"""
import sys, os, tempfile, json, io, urllib.error
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DB_SYNCHRONOUS"] = "OFF"
os.environ["TG_BOT_TOKEN"] = "x"; os.environ["TG_CHAT_ID"] = "1"
import importlib
from app import config; importlib.reload(config)
from app import db, alerts
db.reset_for_tests()
alerts._ensure_worker = lambda: None          # 測試裡手動 flush

sent, plan = [], []
class R:
    def __init__(self, b): self.b = b
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): pass
def fake(url, data=None, timeout=None):
    mode = plan.pop(0) if plan else "ok"
    if mode == "timeout": raise TimeoutError("timed out")
    if mode == "429": raise urllib.error.HTTPError(url, 429, "", {}, io.BytesIO(b'{"ok":false,"description":"Too Many Requests","parameters":{"retry_after":7}}'))
    if mode == "400": raise urllib.error.HTTPError(url, 400, "", {}, io.BytesIO(b'{"ok":false,"description":"Bad Request: chat not found"}'))
    sent.append(dict(x.split("=", 1) for x in data.decode().split("&")))
    return R(b'{"ok":true}')
alerts.urllib.request.urlopen = fake

t0 = 1_800_000_000
plan[:] = ["timeout", "429"]
alerts.tg("A 平倉"); alerts.tg("B 開倉")
db.ex("UPDATE tg_outbox SET created=?", (t0,))
assert alerts.flush(t0) == 0 and not sent                                       # 逾時 → 15 秒後再試
r = db.q("SELECT tries, next_try, last_err FROM tg_outbox ORDER BY id")[0]; assert r[0] == 1 and r[1] == t0 + 15, r
assert alerts.flush(t0 + 15) == 0                                                # 429 → 照 retry_after 7 秒
assert db.q("SELECT next_try FROM tg_outbox ORDER BY id")[0][0] == t0 + 15 + 7
assert alerts.flush(t0 + 200) == 2                                               # 兩則依序送出
texts = [urllib.parse.unquote_plus(x["text"]) for x in sent]
print(texts)
assert texts[0].startswith("A 平倉") and "延遲 3 分鐘送達" in texts[0] and texts[1].startswith("B 開倉")
print("狀態：", alerts.tg_status())
plan[:] = ["400"]
alerts.tg("C"); assert alerts.flush() == 0
assert db.q("SELECT gave_up FROM tg_outbox WHERE text='C'")[0][0] == 1
ev = [e["msg"] for e in alerts.recent()]
print(ev[:3])
assert any("放棄重送" in e for e in ev) and any("會自動重試" in e for e in ev)
print("OK")
