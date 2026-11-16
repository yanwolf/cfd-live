"""GuYuan_2025（20 分 K）：實盤邏輯 vs 回測引擎；另測停用策略會平倉撤單"""
import sys, os, json, tempfile
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
os.environ.setdefault("DB_SYNCHRONOUS", "OFF")
from synth import make
from sim import simulate


def compare(symbol, seed, days=40):
    m = make(days=days, price=18000.0, seed=seed)
    fb, k0, dt = simulate("GuYuan_2025", symbol, {k: v.copy() for k, v in m.items()}, 1.5, start_day=21, bid_trigger=True,
                          inst_extra={"minutes": 20})
    from app.core import engine
    from app.core.strategies import REGISTRY
    costs = {"spread": 1.5, "slippage": 0.0, "commission": 0.0, "swap_long_pct": 0.0, "swap_short_pct": 0.0}
    r = engine.run(REGISTRY["GuYuan_2025"], {}, m, costs, start_ts=int(m["ts"][k0]) - 1200, symbol=symbol, cache_key=("g", seed))
    bt = [t for t in r["trades"] if t["out"] != "資料結束"]
    lv = fb.closed
    bt_in = {t["entry_ts"]: round(t["net"], 1) for t in bt}
    lv_in = {x[3]: round((x[2] - x[1]) * x[0], 1) for x in lv}
    same_t = set(bt_in) & set(lv_in)
    same_n = sum(1 for t in same_t if abs(bt_in[t] - lv_in[t]) < 0.6)
    print(f"GuYuan_2025 {symbol} seed {seed}: 回測 {len(bt)} 筆、實盤模擬 {len(lv)} 筆、進場時間相同 {len(same_t)}、淨利也相同 {same_n}（{dt:.0f}s）")
    return len(bt), len(lv), same_n


def retire():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    insts = [{"id": "a", "name": "A", "symbol": "JP225", "broker_symbol": "JPN225", "strategy": "GuYuan_2024", "lots": 0.1, "magic": 1, "enabled": True},
             {"id": "w9", "name": "W9", "symbol": "JP225", "broker_symbol": "JPN225", "strategy": "GuYuan_2024", "lots": 0.1, "magic": 2, "enabled": False}]
    with open(os.path.join(tmp, "instances.json"), "w") as fh:
        json.dump(insts, fh)
    os.environ["INSTANCES_FILE"] = os.path.join(tmp, "instances.json")
    import importlib
    from app import config, db, sync
    importlib.reload(config); importlib.reload(sync)
    db.reset_for_tests()
    snap = {"positions": [{"ticket": 11, "magic": 2, "symbol": "JPN225", "type": 0, "volume": 0.1},
                          {"ticket": 12, "magic": 1, "symbol": "JPN225", "type": 0, "volume": 0.1}],
            "orders": [{"ticket": 21, "magic": 2, "symbol": "JPN225"}, {"ticket": 22, "magic": 1, "symbol": "JPN225"}]}
    retired = [i for i in config.load_instances() if not i["enabled"]]
    sync.retire_actions(snap, retired)
    sync.retire_actions(snap, retired)          # 第二次不能重送
    acts = db.q("SELECT inst, op, payload FROM actions ORDER BY id")
    print("停用策略送出的動作：", [(a[0], a[1], json.loads(a[2]).get("ticket")) for a in acts])
    assert [(a[1], json.loads(a[2])["ticket"]) for a in acts] == [("CLOSE", 11), ("CANCEL", 21)], acts


if __name__ == "__main__":
    retire()
    res = [compare("NAS100", 5), compare("JP225", 7)]
    assert all(s >= b - 1 and s >= 1 for b, l, s in res), res
    print("OK")
