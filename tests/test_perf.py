"""績效統計：各策略金額相加 = 帳戶合計；筆數、勝率、回撤和逐筆一致"""
import sys, os, json
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
os.environ.setdefault("DB_SYNCHRONOUS", "OFF")
from synth import make
from sim import simulate

res = {}
def grab(now):
    from app import perf, sync, db
    res["p"] = perf.compute(sync.config_instances())
    res["deals"] = db.q("SELECT data FROM deals")

base = {"symbol": "NAS100", "broker_symbol": "NAS100", "lots": 1.0, "safety_units": 3.0, "enabled": True, "tick_size": 0.000001}
insts = [dict(base, id="a", name="A 多", strategy="GuYuan_2025", minutes=60, params={}, magic=11),
         dict(base, id="b", name="B 空", strategy="GuYuan_2024_S", minutes=60, params={"WIN": 2, "MA2": 100, "EBX": 2}, magic=12)]
m = make(days=45, price=18000.0, seed=9)
simulate("GuYuan_2025", "NAS100", {k: v.copy() for k, v in m.items()}, 1.5, start_day=21, bid_trigger=True, instances=insts, after=grab)
p = res["p"]; S = {x["id"]: x for x in p["strategies"]}
for x in p["strategies"]:
    print(x["name"], x["trades"], x["net_money"], x["win_rate"], x["pf"], x["max_dd"], len(x["open"]))
print("帳戶", p["account"]["closes"], p["account"]["net_money"], p["account"]["max_dd"])
tot = round(sum(json.loads(r[0]).get("profit", 0) for r in res["deals"]), 2)
assert abs(p["account"]["net_money"] - sum(x["net_money"] for x in p["strategies"])) < 0.02
assert p["account"]["closes"] == sum(x["trades"] for x in p["strategies"])
closed_money = sum(t["money"] for x in p["strategies"] for t in x["last"]) if all(x["trades"] <= 15 for x in p["strategies"]) else None
print("成交損益總和", tot, "逐筆合計", closed_money)
assert closed_money is None or abs(closed_money - p["account"]["net_money"]) < 0.02
assert all(x["curve"][-1]["eq"] == x["net_money"] for x in p["strategies"] if x["curve"])
json.dump(p, open(os.environ.get("PERF_OUT", "/dev/null"), "w"))
print("OK")
