from __future__ import annotations
import os
import time
import threading
import requests as _requests
from flask import Flask, render_template, request, jsonify, redirect as flask_redirect
from bot import THSRBot, STATIONS, TIME_OPTIONS, DISCOUNT_OPTIONS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ──────────────────────────────────────────────
#  THSR Bot 狀態
# ──────────────────────────────────────────────
def _resolve_time_to(display: str) -> str:
    code = TIME_OPTIONS.get(display, "2359")
    return "2359" if code == "0000" else code

_bot: THSRBot | None = None
_thread: threading.Thread | None = None
_base_url: str = ""
_lock = threading.Lock()
_status = {
    "running": False,
    "status":  "idle",
    "message": "尚未啟動",
    "found":   False,
    "log":     [],
}

def _update(status: str, message: str, found: bool = False):
    with _lock:
        _status["status"]  = status
        _status["message"] = message
        _status["found"]   = found
        _status["running"] = status == "running"
        ts = time.strftime("%H:%M:%S")
        _status["log"].append(f"[{ts}] {message}")
        if len(_status["log"]) > 100:
            _status["log"] = _status["log"][-100:]

# ──────────────────────────────────────────────
#  TDX API（桃園機場捷運即時資料）
# ──────────────────────────────────────────────
_tdx_cache: dict = {"token": None, "expires_at": 0.0}
_tdx_lock = threading.Lock()

_MRT_STATIONS = [
    {"id": "A01", "name": "台北車站"},
    {"id": "A02", "name": "三重站"},
    {"id": "A03", "name": "新北產業園區站"},
    {"id": "A04", "name": "機場第一航廈站"},
    {"id": "A05", "name": "機場第二航廈站"},
    {"id": "A06", "name": "機場旅館站"},
    {"id": "A07", "name": "大園站"},
    {"id": "A08", "name": "坑口站"},
    {"id": "A09", "name": "長庚醫院站"},
    {"id": "A10", "name": "中壢站"},
]

def _get_tdx_token() -> str | None:
    client_id     = os.environ.get("TDX_CLIENT_ID", "")
    client_secret = os.environ.get("TDX_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    with _tdx_lock:
        now = time.time()
        if _tdx_cache["token"] and now < _tdx_cache["expires_at"] - 60:
            return _tdx_cache["token"]
        try:
            resp = _requests.post(
                "https://tdx.transportdata.tw/auth/realms/TDXConnect"
                "/protocol/openid-connect/token",
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                },
                timeout=10,
            )
            data = resp.json()
            token = data.get("access_token")
            _tdx_cache["token"]      = token
            _tdx_cache["expires_at"] = now + data.get("expires_in", 300)
            return token
        except Exception:
            return None

# ──────────────────────────────────────────────
#  首頁（交通工具選單）
# ──────────────────────────────────────────────
@app.route("/")
def home():
    return render_template("home.html")

# ──────────────────────────────────────────────
#  高鐵刷票
# ──────────────────────────────────────────────
@app.route("/thsr")
def thsr():
    return render_template(
        "index.html",
        stations=list(STATIONS.keys()),
        times=list(TIME_OPTIONS.keys()),
        has_tg_token=bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        discounts=list(DISCOUNT_OPTIONS.keys()),
    )

@app.route("/api/start", methods=["POST"])
def start():
    global _bot, _thread, _base_url
    data = request.json or {}
    for f in ["origin", "destination", "date", "time_from", "seat_type", "adult"]:
        if not data.get(f):
            return jsonify({"error": f"缺少欄位: {f}"}), 400
    if data["origin"] == data["destination"]:
        return jsonify({"error": "出發站與到達站不能相同"}), 400

    _base_url = request.host_url.rstrip("/")
    config = {
        "origin":      data["origin"],
        "destination": data["destination"],
        "date":        data["date"].replace("-", "/"),
        "time":        TIME_OPTIONS.get(data["time_from"], "0000"),
        "time_to":     _resolve_time_to(data.get("time_to", "不限")),
        "discount":    DISCOUNT_OPTIONS.get(data.get("discount", "全票"), ""),
        "seat_type":   data["seat_type"],
        "adult":       data["adult"],
        "interval":    data.get("interval", 30),
        "tg_token":    data.get("tg_token", ""),
        "tg_chat_id":  data.get("tg_chat_id", ""),
        "base_url":    _base_url,
    }
    with _lock:
        thread_alive = _thread is not None and _thread.is_alive()
        if _status["running"] and thread_alive:
            return jsonify({"error": "機器人已在執行中"}), 400
        if not thread_alive:
            _status["running"] = False
        _status.update({"running": True, "status": "running",
                        "message": "啟動中...", "found": False, "log": []})
        _bot = THSRBot(config, _update)
        _thread = threading.Thread(target=_bot.run, daemon=True)
        _thread.start()
    return jsonify({"success": True})

@app.route("/api/stop", methods=["POST"])
def stop():
    global _bot, _thread
    with _lock:
        if _bot:
            _bot.stop()
            _bot = None
        _status.update({"running": False, "status": "stopped", "message": "使用者手動停止"})
    if _thread is not None:
        _thread.join(timeout=3)
    return jsonify({"success": True})

@app.route("/api/status")
def status():
    return jsonify(_status)

@app.route("/api/go")
def booking_go():
    from bot import _get_search_url
    config = {
        "origin_code": request.args.get("o", "TaiPei"),
        "dest_code":   request.args.get("d", "ZuoYing"),
        "date":        request.args.get("dt", ""),
        "time_from":   request.args.get("t", "0000"),
        "discount":    request.args.get("dis", ""),
    }
    url, err = _get_search_url(config)
    if err:
        app.logger.warning(f"[go] Encrypt 失敗: {err}")
    return flask_redirect(url)

# ──────────────────────────────────────────────
#  機場捷運
# ──────────────────────────────────────────────
@app.route("/mrt")
def mrt():
    return render_template(
        "mrt.html",
        stations=_MRT_STATIONS,
        has_tdx=bool(os.environ.get("TDX_CLIENT_ID")),
    )

@app.route("/api/mrt/liveboard")
def mrt_liveboard():
    token = _get_tdx_token()
    if not token:
        return jsonify({"error": "TDX_CLIENT_ID / TDX_CLIENT_SECRET 未設定", "configured": False}), 503
    try:
        resp = _requests.get(
            "https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/LiveBoard/TYMC",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
            timeout=10,
        )
        resp.raise_for_status()
        return jsonify({"configured": True, "data": resp.json()})
    except Exception as e:
        return jsonify({"error": str(e), "configured": True}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5566))
    print(f"啟動中，請開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
