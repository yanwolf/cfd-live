"""對帳：模擬券商沒有滑價時，相符筆的滑價應該 ≈ 0，筆數一致；人為加滑價後要量得出來"""
import sys, os
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
os.environ.setdefault("DB_SYNCHRONOUS", "OFF")
from synth import make
from sim import simulate
import sim

res = {}
def grab(now):
    from app import recon, sync
    res["r"] = recon.reconcile_all(sync.config_instances(), now)

m = make(days=40, price=18000.0, seed=5)
simulate("GuYuan_2025", "NAS100", {k: v.copy() for k, v in m.items()}, 1.5, start_day=21, bid_trigger=True,
         inst_extra={"minutes": 60}, after=grab)
x = res["r"][0]; s = x["summary"]
print({k: s[k] for k in ("live_n", "bt_n", "matched", "missed", "extra", "entry_slip_avg", "exit_slip_avg", "slip_per_side", "live_pts", "bt_pts")})
assert s["matched"] >= 1 and s["extra"] == 0 and s["missed"] <= 1, s
assert abs(s["entry_slip_avg"]) < 0.05 and (s["exit_slip_avg"] is None or abs(s["exit_slip_avg"]) < 0.05), s

# 人為滑價：每筆成交價都比報價差 2 點
orig = sim.FakeBroker.open, sim.FakeBroker.close
def open_slip(self, ts, d, price, *a, **k): return orig[0](self, ts, d, price + 2 * d, *a, **k)
def close_slip(self, ts, p, price, *a, **k): return orig[1](self, ts, p, price - 2 * (1 if p["type"] == 0 else -1), *a, **k)
sim.FakeBroker.open, sim.FakeBroker.close = open_slip, close_slip
simulate("GuYuan_2025", "NAS100", {k: v.copy() for k, v in m.items()}, 1.5, start_day=21, bid_trigger=True,
         inst_extra={"minutes": 60}, after=grab)
s = res["r"][0]["summary"]
print({k: s[k] for k in ("live_n", "matched", "entry_slip_avg", "exit_slip_avg", "slip_per_side")})
assert abs(s["entry_slip_avg"] - 2) < 0.05, s
print("OK")
