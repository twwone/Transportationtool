from __future__ import annotations
import os
import time
import threading
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
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
    {"id": "A1",   "name": "台北車站"},
    {"id": "A2",   "name": "三重站"},
    {"id": "A3",   "name": "新北產業園區站"},
    {"id": "A4",   "name": "新莊副都心站"},
    {"id": "A5",   "name": "泰山站"},
    {"id": "A6",   "name": "泰山貴和站"},
    {"id": "A7",   "name": "體育大學站"},
    {"id": "A8",   "name": "長庚醫院站"},
    {"id": "A9",   "name": "林口站"},
    {"id": "A10",  "name": "山鼻站"},
    {"id": "A11",  "name": "坑口站"},
    {"id": "A12",  "name": "機場第一航廈站"},
    {"id": "A13",  "name": "機場第二航廈站"},
    {"id": "A14a", "name": "機場旅館站"},
    {"id": "A15",  "name": "大園站"},
    {"id": "A16",  "name": "橫山站"},
    {"id": "A17",  "name": "領航站"},
    {"id": "A18",  "name": "高鐵桃園站"},
    {"id": "A19",  "name": "桃園體育園區站"},
    {"id": "A20",  "name": "興南站"},
    {"id": "A21",  "name": "環北站"},
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
@app.route("/sw.js")
def sw():
    from flask import send_from_directory, make_response
    resp = make_response(send_from_directory("static", "sw.js"))
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

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

_mrt_cache: dict = {"data": None, "expires_at": 0.0}
_mrt_lock = threading.Lock()

# ─── Siri 通勤捷徑設定（依個人需求修改這四行）────────
_SIRI_STATION_ID   = "A18"        # 站點代碼，例如 A18 = 高鐵桃園站
_SIRI_STATION_NAME = "高鐵桃園站"  # 顯示用中文站名
_SIRI_DIRECTION    = 0             # 0 = 往台北/機場方向；1 = 往環北方向
_SIRI_DIR_LABEL    = "往機場"      # 訊息裡顯示的方向文字

def _get_mrt_data(token: str):
    """命中 30 秒快取就直接回傳，否則重打 TDX 並更新快取。"""
    with _mrt_lock:
        if _mrt_cache["data"] and time.time() < _mrt_cache["expires_at"]:
            return _mrt_cache["data"]
    try:
        resp = _requests.get(
            "https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/LiveBoard/TYMC",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        with _mrt_lock:
            _mrt_cache["data"] = data
            _mrt_cache["expires_at"] = time.time() + 30
        return data
    except Exception:
        return None

@app.route("/api/mrt/siri")
def mrt_siri():
    use_text = bool(request.args.get("text"))
    fallback_msg = "抱歉，目前無法取得機場捷運即時資料，請稍後再試。"

    # URL 參數優先，沒帶就用預設值
    station_id = request.args.get("station", _SIRI_STATION_ID)
    try:
        direction = int(request.args.get("dir", _SIRI_DIRECTION))
    except ValueError:
        direction = _SIRI_DIRECTION

    station_info = next((s for s in _MRT_STATIONS if s["id"] == station_id), None)
    station_name = station_info["name"] if station_info else station_id
    dir_label = "往台北" if direction == 0 else "往環北"

    def _resp(msg):
        if use_text:
            from flask import Response
            return Response(msg, mimetype="text/plain; charset=utf-8")
        return jsonify({"siri_message": msg})

    token = _get_tdx_token()
    if not token:
        return _resp(fallback_msg)

    data = _get_mrt_data(token)
    if not data:
        return _resp(fallback_msg)

    trains = [
        t for t in data
        if t.get("StationID") == station_id
        and t.get("Direction") == direction
    ]
    if not trains:
        return _resp(f"目前 {station_name} {dir_label} 無即時班次資料。")

    now_ts = time.time()
    best, best_mins = None, None
    for t in trains:
        raw = t.get("EstimatedArrivalTime", "")
        if not raw:
            continue
        try:
            arrival = _dt.fromisoformat(raw)
            if arrival.tzinfo is None:
                arrival = arrival.replace(tzinfo=_tz(_td(hours=8)))
            mins = (arrival.timestamp() - now_ts) / 60
            if mins < 0:
                continue
            if best_mins is None or mins < best_mins:
                best_mins, best = mins, t
        except Exception:
            continue

    if best is None:
        return _resp(f"目前 {station_name} {dir_label} 無即時班次資料。")

    type_raw = best.get("TrainTypeName", {})
    train_type = type_raw.get("Zh_tw", "列車") if isinstance(type_raw, dict) else str(type_raw or "列車")

    mins = round(best_mins)
    if mins <= 0:
        msg = f"下一班{dir_label}的{train_type}即將進站，請盡快前往月台。"
    else:
        msg = f"下一班{dir_label}的{train_type}還有 {mins} 分鐘進站，請把握時間。"

    return _resp(msg)

@app.route("/api/mrt/liveboard")
def mrt_liveboard():
    token = _get_tdx_token()
    if not token:
        return jsonify({"error": "TDX_CLIENT_ID / TDX_CLIENT_SECRET 未設定", "configured": False}), 503
    with _mrt_lock:
        now = time.time()
        if _mrt_cache["data"] and now < _mrt_cache["expires_at"]:
            return jsonify({"configured": True, "data": _mrt_cache["data"]})
    try:
        resp = _requests.get(
            "https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/LiveBoard/TYMC",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$format": "JSON"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        with _mrt_lock:
            _mrt_cache["data"] = data
            _mrt_cache["expires_at"] = time.time() + 30
        return jsonify({"configured": True, "data": data})
    except Exception as e:
        return jsonify({"error": str(e), "configured": True}), 500




# ──────────────────────────────────────────────
#  TIAS（航空資訊看板）
#  資料來源：TDX 交通部 FIDS 即時到離站 API
# ──────────────────────────────────────────────

# 可擴充：新增 {"code": "XX", "name": "..."} 即可納入過濾
_TIAS_AIRLINES = [
    {"code": "AK", "name": "AirAsia"},
    {"code": "FD", "name": "Thai AirAsia"},
    {"code": "QZ", "name": "AirAsia Indonesia"},
    {"code": "Z2", "name": "AirAsia Philippines"},
    {"code": "XT", "name": "AirAsia X Indonesia"},
    {"code": "D7", "name": "AirAsia X"},
    {"code": "XJ", "name": "Thai AirAsia X"},
]
_TIAS_CODES = {a["code"] for a in _TIAS_AIRLINES}
_TIAS_TTL   = 30  # 秒，與 MRT 模組一致

_tias_cache = {"arr": None, "dep": None, "expires_at": 0.0}
_tias_lock  = threading.Lock()

_TDX_FIDS = "https://tdx.transportdata.tw/api/basic/v2/Air/FIDS/Airport"

def _fetch_tias():
    """命中 30 秒快取就直接回傳，否則重打 TDX FIDS 並更新快取。"""
    with _tias_lock:
        if _tias_cache["arr"] is not None and time.time() < _tias_cache["expires_at"]:
            return _tias_cache["arr"], _tias_cache["dep"], True

    token = _get_tdx_token()
    if not token:
        return None, None, False

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params  = {"$format": "JSON", "$top": 400}

    def _get(direction: str):
        try:
            r = _requests.get(
                f"{_TDX_FIDS}/{direction}/TPE",
                headers=headers, params=params, timeout=12,
            )
            r.raise_for_status()
            body = r.json()
            return body if isinstance(body, list) else body.get("data", [])
        except Exception:
            return []

    arr = _get("Arrival")
    dep = _get("Departure")

    with _tias_lock:
        _tias_cache["arr"] = arr
        _tias_cache["dep"] = dep
        _tias_cache["expires_at"] = time.time() + _TIAS_TTL

    return arr, dep, True


@app.route("/tias")
def tias():
    return render_template("tias.html")


@app.route("/api/tias/flights")
def tias_flights_api():
    arr_all, dep_all, ok = _fetch_tias()
    if not ok:
        return jsonify({"error": "TDX_CLIENT_ID / TDX_CLIENT_SECRET 未設定", "configured": False}), 503

    return jsonify({
        "configured":     True,
        "arrivals":       [f for f in arr_all if f.get("AirlineID") in _TIAS_CODES],
        "departures":     [f for f in dep_all if f.get("AirlineID") in _TIAS_CODES],
        "all_arrivals":   arr_all,
        "all_departures": dep_all,
        "airlines":       _TIAS_AIRLINES,
        "updated_at":     time.strftime("%H:%M:%S"),
    })


@app.route("/api/tias/debug")
def tias_debug():
    arr, dep, ok = _fetch_tias()
    return jsonify({
        "tdx_configured": ok,
        "tdx_client_id":  bool(os.environ.get("TDX_CLIENT_ID")),
        "cached_arr":     len(arr) if arr else 0,
        "cached_dep":     len(dep) if dep else 0,
        "airasia_arr":    len([f for f in (arr or []) if f.get("AirlineID") in _TIAS_CODES]),
        "airasia_dep":    len([f for f in (dep or []) if f.get("AirlineID") in _TIAS_CODES]),
        "sample_arr":     (arr or [])[:2],
        "sample_dep":     (dep or [])[:2],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5566))
    print(f"啟動中，請開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
