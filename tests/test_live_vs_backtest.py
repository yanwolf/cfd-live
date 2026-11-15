"""實盤邏輯 vs 回測引擎：同一份資料、同樣成本（只有點差），交易應該幾乎一樣"""
import sys, os
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
import numpy as np
os.environ.setdefault("DB_SYNCHRONOUS", "OFF")
from synth import make
from sim import simulate


def compare(strategy, symbol="NAS100", days=36, spread=1.5, params=None, seed=5):
    m = make(days=days, price=18000.0, seed=seed)
    fb, k0, dt = simulate(strategy, symbol, {k: v.copy() for k, v in m.items()}, spread, start_day=21, bid_trigger=True, inst_extra={"params": params or {}})
    from app.core import engine
    from app.core.strategies import REGISTRY
    costs = {"spread": spread, "slippage": 0.0, "commission": 0.0, "swap_long_pct": 0.0, "swap_short_pct": 0.0}
    start = int(m["ts"][k0]) - 3600          # 實盤第一次同步就處理上一根 K 的收盤，回測對齊到那一根
    r = engine.run(REGISTRY[strategy], params or {}, m, costs, start_ts=start, symbol=symbol, cache_key=("x", strategy))
    bt = r["trades"]
    live = fb.closed
    bt_net = sum(t["net"] for t in bt)
    live_net = sum((ex - en) * d for d, en, ex, *_ in live)
    print(f"{strategy}: 回測 {len(bt)} 筆 淨 {bt_net:.1f}｜實盤模擬 {len(live)} 筆 淨 {live_net:.1f}｜耗時 {dt:.0f}s")
    # 逐筆對照進場時間
    bt_in = [t["entry_ts"] for t in bt]
    lv_in = [x[3] for x in live]
    same = len(set(bt_in) & set(lv_in))
    print(f"  進場時間相同的筆數：{same}／回測 {len(bt_in)}")
    return bt, live


if __name__ == "__main__":
    compare("AR_crossover")
    compare("GuYuan_2024")
