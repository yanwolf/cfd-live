"""檢討條件（app/review.py）：連虧、最近 50 筆 PF、最大回撤；觸發只提醒一次、自動暫停進場、解除後可再觸發"""
import json, os, sys, tempfile
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DB_SYNCHRONOUS"] = "OFF"
from app import db, alerts
db.reset_for_tests()
alerts._ensure_worker = lambda: None
from app import review

INST = {"id": "nas", "name": "那指 GY25", "magic": 100060, "enabled": True,
        "review": {"max_consec_loss": 5, "min_pf50": 0.8, "max_dd_pts": 300, "action": "pause", "basis": "測試"}}
T0 = 1_800_000_000
seq = [0]


def trade(pts, i):
    seq[0] += 1
    pid = seq[0]
    for entry, price, t in ((0, 1000.0, T0 + i * 3600), (1, 1000.0 + pts, T0 + i * 3600 + 1800)):
        db.ex("INSERT INTO deals(ticket, ts, magic, symbol, entry, data) VALUES (?,?,?,?,?,?)",
              (pid * 10 + entry, t, 100060, "NAS100", entry,
               json.dumps({"ticket": pid * 10 + entry, "position": pid, "entry": entry, "type": 0, "price": price,
                           "volume": 0.1, "profit": pts * 0.1 if entry else 0, "time_utc": t})))


def events():
    return [m for (m,) in db.q("SELECT msg FROM events ORDER BY rowid")]


# 40 筆：贏 30、輸 10 交錯 → 不觸發
for i in range(40):
    trade(40 if i % 4 else -20, i)
assert review.check([INST]) == [], review.status([INST])
st = [x for x in review.status([INST]) if x["id"] == "nas"][0]
assert st["metrics"]["pf50"] is None and st["metrics"]["consec_loss"] == 0, st["metrics"]

# 連虧 6 筆（每筆 −30）→ 連虧觸發；回撤 180 點還沒到 300
for i in range(40, 46):
    trade(-30, i)
fired = review.check([INST])
assert fired == ["nas:max_consec_loss"], fired
assert "nas" in db.kv_get("paused", []), "應自動暫停進場"
assert any("連續虧損 6筆" in e and "依據：測試" in e for e in events()), events()[-1]
# 再檢查一次：不重複提醒
assert review.check([INST]) == []

# 手動恢復進場後，同一條件不會再暫停（直到解除再觸發）
db.kv_set("paused", [])
trade(-30, 46)
assert review.check([INST]) == [] and "nas" not in db.kv_get("paused", [])

# 一筆獲利打斷連虧 → 解除；之後再連虧 6 筆 → 再觸發；同時 50 筆 PF、回撤也會到
trade(10, 47)
review.check([INST])
for i in range(48, 54):
    trade(-30, i)
fired = review.check([INST])
st = [x for x in review.status([INST]) if x["id"] == "nas"][0]
print(st["metrics"], fired)
assert "nas:max_consec_loss" in fired
assert "nas:max_dd_pts" in fired and st["metrics"]["max_dd_pts"] > 300
assert st["metrics"]["pf50"] is not None

# since：只算這天之後進場的
INST2 = dict(INST, id="nas2", review=dict(INST["review"], since="2099-01-01"))
assert [x for x in review.status([INST2]) if x["id"] == "nas2"][0]["metrics"]["n"] == 0
# 帳戶合計：已實現回撤（美元）超過門檻 → 暫停所有啟用中的策略；修改會記事件
OTHER = {"id": "gold", "name": "黃金", "magic": 900025, "enabled": True}
db.kv_set("paused", [])
review.set_cfg(review.ACC, {"max_dd_money": "30", "action": "pause", "basis": "帳戶 2000 的 1.5%（測試）"})
assert any("檢討條件修改：帳戶合計" in e for e in events())
acc = [x for x in review.status([INST, OTHER]) if x["id"] == "_account"][0]
print(acc["metrics"], acc["rules"])
assert acc["metrics"]["max_dd_money"] == 38.0, acc["metrics"]
fired = review.check([INST, OTHER])
assert "_account:max_dd_money" in fired, fired
assert set(db.kv_get("paused", [])) >= {"nas", "gold"}, db.kv_get("paused", [])
assert any("暫停「所有策略」" in e for e in events())
# 網頁清空 = 不檢查
review.set_cfg(review.ACC, {"max_dd_money": "", "action": "pause"})
assert not [x for x in review.status([INST]) if x["id"] == "_account"][0]["configured"]
print("OK")
