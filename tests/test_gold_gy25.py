"""黃金 GuYuan_2025 90 分 K（gold_gy25_90）：實盤邏輯 vs 回測引擎逐筆對得上（黃金的交易日 17:00 換日、90 分 K 切法）"""
import sys, os
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
os.environ.setdefault("DB_SYNCHRONOUS", "OFF")
from synth import make
from sim import simulate


def compare(seed, days=40):
    m = make(days=days, price=4000.0, seed=seed)
    fb, k0, dt = simulate("GuYuan_2025", "XAUUSD", {k: v.copy() for k, v in m.items()}, 0.25, start_day=21, bid_trigger=True,
                          inst_extra={"minutes": 90})
    from app.core import engine
    from app.core.strategies import REGISTRY
    costs = {"spread": 0.25, "slippage": 0.0, "commission": 0.0, "swap_long_pct": 0.0, "swap_short_pct": 0.0}
    r = engine.run(REGISTRY["GuYuan_2025"], {}, m, costs, start_ts=int(m["ts"][k0]) - 5400, symbol="XAUUSD",
                   cache_key=("xau", seed), minutes=90)
    bt = [t for t in r["trades"] if t["out"] != "資料結束"]
    bt_in = {t["entry_ts"]: round(t["net"], 2) for t in bt}
    lv_in = {x[3]: round((x[2] - x[1]) * x[0], 2) for x in fb.closed}
    same_t = set(bt_in) & set(lv_in)
    same_n = sum(1 for t in same_t if abs(bt_in[t] - lv_in[t]) < 0.1)
    print(f"黃金 GY25 90 分 seed {seed}: 回測 {len(bt)} 筆、實盤模擬 {len(fb.closed)} 筆、進場時間相同 {len(same_t)}、淨利也相同 {same_n}（{dt:.0f}s）")
    return len(bt), len(fb.closed), same_n


if __name__ == "__main__":
    b, l, s = compare(11)
    assert b >= 1 and s >= b - 1, (b, l, s)
    print("OK")
