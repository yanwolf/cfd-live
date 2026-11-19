"""cfd-live：MT5 實盤／模擬下單服務"""
import base64
import json
import os
import secrets
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from . import alerts, config, db, live, recon, sync

HERE = os.path.dirname(__file__)
app = FastAPI(title="cfd-live")
_sync_lock = threading.Lock()


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)
    if path.startswith("/ea/"):
        if not config.EA_TOKEN or not secrets.compare_digest(request.headers.get("x-token", ""), config.EA_TOKEN):
            return Response("bad token", status_code=401)
        return await call_next(request)
    if config.LIVE_PASSWORD:
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, config.LIVE_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("需要密碼", status_code=401, headers={"WWW-Authenticate": 'Basic realm="cfd-live"'})
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/ea/sync")
async def ea_sync(request: Request):
    payload = json.loads((await request.body()).decode("utf-8", "replace"))

    def run():
        with _sync_lock:
            return sync.handle(payload)
    return PlainTextResponse(await run_in_threadpool(run))


# ------------------------------------------------------------------ 儀表板
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as fh:
        return fh.read()


@app.get("/api/status")
def status():
    now = int(time.time())
    insts = sync.config_instances()
    snap = db.kv_get("snap", {}) or {}
    acct = db.kv_get("account", {}) or {}
    ea_last = db.kv_get("ea_last", 0) or 0
    paused = set(db.kv_get("paused", []))
    out = []
    for i in insts:
        st = live.get_state(i["id"])
        pos = [p for p in snap.get("positions", []) if p["magic"] == i["magic"]]
        ords = [o for o in snap.get("orders", []) if o["magic"] == i["magic"]]
        last_bar = db.q("SELECT MAX(ts) FROM bars WHERE symbol=?", (i["broker_symbol"],))[0][0]
        out.append({"id": i["id"], "name": i["name"], "symbol": i["symbol"], "broker_symbol": i["broker_symbol"],
                    "strategy": i["strategy"], "lots": i["lots"], "magic": i["magic"], "enabled": i.get("enabled", True),
                    "paused": i["id"] in paused, "paused_reason": st.get("paused_reason"),
                    "positions": pos, "orders": ords, "last_bar_ts": st.get("last_bar_ts"), "last_bar_end": st.get("last_bar_end"),
                    "last_close": st.get("last_close"), "last_orders": st.get("last_orders"), "processed_at": st.get("processed_at"),
                    "last_m1": last_bar})
    deals = db.q("SELECT data FROM deals ORDER BY ticket DESC LIMIT 40")
    acts = db.q("SELECT id, inst, op, payload, status, created, result FROM actions ORDER BY id DESC LIMIT 40")
    return {"now": now, "ea_last": ea_last, "ea_age": now - ea_last if ea_last else None, "account": acct,
            "kill": bool(db.kv_get("kill", False)), "instances": out,
            "deals": [json.loads(r[0]) for r in deals],
            "actions": [{"id": a[0], "inst": a[1], "op": a[2], "payload": json.loads(a[3]), "status": a[4], "created": a[5],
                         "result": json.loads(a[6]) if a[6] else None} for a in acts],
            "events": alerts.recent(60), "tg": alerts.tg_status()}


@app.get("/api/recon")
async def api_recon():
    """實盤 vs 回測逐筆對帳（每支策略從它開始跑的時間起算）"""
    return await run_in_threadpool(recon.reconcile_all, sync.config_instances())


@app.post("/api/kill")
def kill(body: dict):
    on = bool(body.get("on", True))
    db.kv_set("kill", on)
    if on:
        db.kv_set("kill_sent", [])
        alerts.event("error", "緊急停止：平掉所有持倉、撤銷所有掛單，停止下單")
    else:
        alerts.event("warn", "解除緊急停止，恢復運作")
    return {"ok": True}


@app.post("/api/pause")
def pause(body: dict):
    iid, on = body["id"], bool(body.get("on", True))
    paused = set(db.kv_get("paused", []))
    (paused.add if on else paused.discard)(iid)
    db.kv_set("paused", sorted(paused))
    if not on:
        st = live.get_state(iid)
        st.pop("paused_reason", None)
        live.set_state(iid, st)
    alerts.event("warn" if on else "info", f"{iid} {'暫停進場' if on else '恢復進場'}", iid)
    return {"ok": True}


# ------------------------------------------------------------------ 心跳監看
def _watchdog():
    alerted = False
    while True:
        time.sleep(30)
        try:
            last = db.kv_get("ea_last", 0) or 0
            if not last:
                continue
            age = time.time() - last
            if age > config.HEARTBEAT_ALERT_SEC and not alerted:
                alerts.event("error", f"EA 已 {int(age)} 秒沒有連線：檢查 Windows VPS 與 MT5 是否在執行（持倉的停損仍在券商端）")
                alerted = True
            elif age <= config.HEARTBEAT_ALERT_SEC and alerted:
                alerts.event("info", "EA 恢復連線", notify=True)
                alerted = False
        except Exception:
            pass


@app.on_event("startup")
def _start():
    db.conn()
    sync.config_instances()
    threading.Thread(target=_watchdog, daemon=True).start()
    alerts._ensure_worker()          # 重啟前沒送出的 Telegram 會接著送
