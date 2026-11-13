"""MT5 伺服器時間 ↔ UTC。

MT5 的時間都是「券商伺服器牆鐘」當成 epoch 秒。Pepperstone 等多數券商用紐約收盤制：
伺服器時間 = 美東時間 + 7 小時（冬令 GMT+2、夏令 GMT+3）。
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config

NY = ZoneInfo("America/New_York")


def _fixed():
    m = config.SERVER_TZ
    return int(m.split(":")[1]) if m.startswith("fixed:") else None


def srv2utc(ts):
    """單一數值"""
    off = _fixed()
    if off is not None:
        return int(ts) - off * 3600
    wall = datetime(1970, 1, 1) + timedelta(seconds=int(ts) - 7 * 3600)
    return int(wall.replace(tzinfo=NY).timestamp())


def srv2utc_arr(arr):
    arr = np.asarray(arr, dtype="int64")
    off = _fixed()
    if off is not None:
        return arr - off * 3600
    wall = pd.to_datetime(arr - 7 * 3600, unit="s")
    loc = wall.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
    out = loc.tz_convert("UTC").as_unit("s").asi8
    bad = pd.isna(loc)
    if bad.any():                                   # 秋季重複的那一小時：當作夏令
        fix = wall[bad].tz_localize("America/New_York", ambiguous=True).tz_convert("UTC").as_unit("s").asi8
        out = out.copy(); out[np.asarray(bad)] = fix
    return out


def utc2srv(ts):
    off = _fixed()
    if off is not None:
        return int(ts) + off * 3600
    et = datetime.fromtimestamp(int(ts), NY).replace(tzinfo=None)
    return int((et + timedelta(hours=7) - datetime(1970, 1, 1)).total_seconds())


def et_of(ts_utc):
    return datetime.fromtimestamp(int(ts_utc), NY)
