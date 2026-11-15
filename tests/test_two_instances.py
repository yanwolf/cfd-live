"""同一商品兩支策略（不同 magic、Hedge 帳戶）同時跑：各自的交易要跟各自單獨回測一致，互不干擾"""
import os, sys
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
os.environ.setdefault("DB_SYNCHRONOUS", "OFF")
from synth import make_jp
from sim import simulate
from app.core import engine
from app.core.strategies import REGISTRY

base = {"symbol": "JP225", "broker_symbol": "JP225", "strategy": "GuYuan_2024", "lots": 1.0, "safety_units": 3.0,
        "enabled": True, "tick_size": 0.000001}
A = dict(base, id="a", name="A 台指參數", magic=225024, params={})
B = dict(base, id="b", name="B WIN9", magic=225029, params={"LOSS": 2, "WIN": 9, "EBX": 2})
m = make_jp(days=40, price=38000.0, seed=9)
fb, k0, dt = simulate("GuYuan_2024", "JP225", {k: v.copy() for k, v in m.items()}, 8.0, start_day=21,
                      bid_trigger=True, instances=[A, B])
costs = {"spread": 8.0, "slippage": 0.0, "commission": 0.0, "swap_long_pct": 0.0, "swap_short_pct": 0.0}
ok = True
for inst in (A, B):
    bt = engine.run(REGISTRY["GuYuan_2024"], inst["params"], m, costs, start_ts=int(m["ts"][k0]) - 3600,
                    symbol="JP225", cache_key=("two", inst["id"]))["trades"]
    lv = [x for x in fb.closed if x[6] == inst["magic"]]
    bi = {t["entry_ts"]: round(t["net"], 2) for t in bt}
    li = {x[3]: round((x[2] - x[1]) * x[0], 2) for x in lv}
    same = sum(1 for k in bi if k in li and abs(bi[k] - li[k]) < 0.01)
    print(f"{inst['name']}（magic {inst['magic']}）：回測 {len(bt)} 筆、實盤模擬 {len(lv)} 筆，逐筆相同 {same}")
    ok &= same >= len(bt) - 1
both = sum(1 for a in fb.closed if a[6] == A["magic"] for b in fb.closed if b[6] == B["magic"] and a[3] < b[4] and b[3] < a[4])
print(f"兩支同時持倉的筆數：{both}；耗時 {dt:.0f}s")
print("OK" if ok else "MISMATCH")
