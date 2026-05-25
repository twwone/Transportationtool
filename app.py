from __future__ import annotations
import os
import time
import threading
import random
import hashlib
import hmac
import secrets
import math
import re  as _re
import json as _json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
import requests as _requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=True)  # 本機 vercel dev 注入的 KV 環境變數

app = Flask(__name__)

# ──────────────────────────────────────────────
#  TDX API（桃園機場捷運即時資料）
# ──────────────────────────────────────────────
_tdx_cache: dict = {"token": None, "expires_at": 0.0}
_tdx_lock         = threading.Lock()
_tdx_refresh_lock = threading.Lock()  # 確保同時只有一個執行緒去刷新 token

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
    now = time.time()
    with _tdx_lock:
        if _tdx_cache["token"] and now < _tdx_cache["expires_at"] - 60:
            return _tdx_cache["token"]
    # 同時只允許一個執行緒去刷新；其他執行緒等待後直接讀快取
    with _tdx_refresh_lock:
        with _tdx_lock:
            if _tdx_cache["token"] and time.time() < _tdx_cache["expires_at"] - 60:
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
            data  = resp.json()
            token = data.get("access_token")
            with _tdx_lock:
                _tdx_cache["token"]      = token
                _tdx_cache["expires_at"] = time.time() + data.get("expires_in", 300)
            return token
        except Exception:
            with _tdx_lock:
                return _tdx_cache.get("token")

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
        # 重打失敗時回傳舊快取，避免短暫網路中斷造成整頁錯誤
        with _mrt_lock:
            stale = _mrt_cache.get("data")
        if stale:
            return jsonify({"configured": True, "data": stale, "stale": True})
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

_TDX_FIDS    = "https://tdx.transportdata.tw/api/basic/v2/Air/FIDS/Airport"
_TDX_AIRLINE = "https://tdx.transportdata.tw/api/basic/v2/Air/Airline"
_TDX_AIRPORT = "https://tdx.transportdata.tw/api/basic/v2/Air/Airport"

# 元資料快取（航空公司 / 機場）：靜態資料，24 小時刷一次
_META_TTL   = 86400
_meta_cache: dict = {"airlines": {}, "airports": {}, "expires_at": 0.0}
_meta_lock  = threading.Lock()


def _fetch_metadata() -> tuple[dict, dict]:
    """回傳 (airline_map, airport_map)；24 h 快取，失敗時保留舊資料。

    airline_map  = { "AK": {"zh": "亞洲航空", "en": "AirAsia"}, ... }
    airport_map  = { "TPE": {"zh": "桃園", "en": "Taoyuan", "ap_zh": "桃園國際機場"}, ... }
    """
    with _meta_lock:
        if _meta_cache["airlines"] and time.time() < _meta_cache["expires_at"]:
            return _meta_cache["airlines"], _meta_cache["airports"]

    token = _get_tdx_token()
    if not token:
        with _meta_lock:
            return _meta_cache["airlines"], _meta_cache["airports"]

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ── 航空公司 ──
    airline_map: dict[str, dict] = {}
    try:
        r = _requests.get(
            _TDX_AIRLINE, headers=headers,
            params={"$format": "JSON", "$top": 2000}, timeout=8,
        )
        r.raise_for_status()
        body = r.json()
        for a in (body if isinstance(body, list) else body.get("data", [])):
            code = a.get("AirlineID", "")
            if not code:
                continue
            name = a.get("AirlineName", {})
            airline_map[code] = {
                "zh": name.get("Zh_tw", "") if isinstance(name, dict) else "",
                "en": name.get("En",    "") if isinstance(name, dict) else str(name or ""),
            }
    except Exception:
        pass

    # ── 機場 ──
    airport_map: dict[str, dict] = {}
    try:
        r = _requests.get(
            _TDX_AIRPORT, headers=headers,
            params={"$format": "JSON", "$top": 2000}, timeout=8,
        )
        r.raise_for_status()
        body = r.json()
        for a in (body if isinstance(body, list) else body.get("data", [])):
            # TDX v2 AirportID 即 IATA 三碼，與 FIDS DepartureAirportID 對應
            iata = a.get("AirportID", "")
            if not iata:
                continue
            city = a.get("CityName", {})
            apn  = a.get("AirportName", {})
            airport_map[iata] = {
                "zh":    city.get("Zh_tw", "") if isinstance(city, dict) else "",
                "en":    city.get("En",    "") if isinstance(city, dict) else "",
                "ap_zh": apn.get("Zh_tw",  "") if isinstance(apn,  dict) else "",
            }
    except Exception:
        pass

    with _meta_lock:
        if airline_map:
            _meta_cache["airlines"] = airline_map
        if airport_map:
            _meta_cache["airports"] = airport_map
        # 兩者都成功才快取完整 24h；任一失敗 5 分鐘後重試
        both_ok = bool(airline_map) and bool(airport_map)
        _meta_cache["expires_at"] = time.time() + (_META_TTL if both_ok else 300)
        return _meta_cache["airlines"], _meta_cache["airports"]

def _fetch_tias():
    """命中 30 秒快取就直接回傳，否則重打 TDX FIDS 並更新快取。
    入港/出港改為平行請求，最差只等 1 次 timeout 而非 2 次。
    任一方向 API 失敗時保留舊快取，避免回傳空資料覆蓋正常資料。"""
    with _tias_lock:
        if _tias_cache["arr"] is not None and time.time() < _tias_cache["expires_at"]:
            return _tias_cache["arr"], _tias_cache["dep"], True
        stale = (_tias_cache.get("arr"), _tias_cache.get("dep"))

    token = _get_tdx_token()
    if not token:
        if stale[0] is not None:
            return stale[0], stale[1], True
        return None, None, False

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    now_tw  = _dt.now(_tz(_td(hours=8)))
    today     = now_tw.strftime("%Y-%m-%d")
    yesterday = (now_tw - _td(days=1)).strftime("%Y-%m-%d")
    valid_dates = {today, yesterday}

    def _get(direction: str, time_field: str) -> tuple:
        try:
            r = _requests.get(
                f"{_TDX_FIDS}/{direction}/TPE",
                headers=headers,
                # $top=2000 確保取到今天+昨天的班機（凌晨班機 FlightDate 可能是前一天）
                params={"$format": "JSON", "$top": 2000, "$orderby": "FlightDate desc"},
                timeout=10,
            )
            r.raise_for_status()
            body = r.json()
            data = body if isinstance(body, list) else body.get("data", [])
            filtered = [f for f in data if f.get("FlightDate", "") in valid_dates]
            filtered.sort(key=lambda f: f.get(time_field, ""))
            return filtered, True
        except Exception:
            return [], False

    # ── 平行發送入港 / 出港兩支 API（最差等 1 個 timeout 而非 2 個）──
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_arr = pool.submit(_get, "Arrival",   "ScheduleArrivalTime")
        fut_dep = pool.submit(_get, "Departure", "ScheduleDepartureTime")
        arr, arr_ok = fut_arr.result()
        dep, dep_ok = fut_dep.result()

    # 任一方向失敗：優先用剛拿到的成功資料 + 舊快取補另一邊
    if not arr_ok or not dep_ok:
        if stale[0] is not None:
            # 哪邊失敗就用舊快取那邊補回
            final_arr = arr if arr_ok else stale[0]
            final_dep = dep if dep_ok else stale[1]
            with _tias_lock:
                _tias_cache["arr"] = final_arr
                _tias_cache["dep"] = final_dep
                _tias_cache["expires_at"] = time.time() + _TIAS_TTL
            return final_arr, final_dep, True
        # 完全沒舊資料：回傳已成功那一邊，讓前端至少能顯示部分資料
        if arr_ok or dep_ok:
            return arr, dep, True
        return None, None, False

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
    has_creds = bool(os.environ.get("TDX_CLIENT_ID") and os.environ.get("TDX_CLIENT_SECRET"))
    arr_all, dep_all, ok = _fetch_tias()
    if not ok:
        if not has_creds:
            return jsonify({"error": "TDX_CLIENT_ID / TDX_CLIENT_SECRET 未設定", "configured": False}), 503
        return jsonify({"error": "TDX API 暫時無法連線，請稍後再試", "configured": True, "retry": True}), 503

    airline_map, airport_map = _fetch_metadata()

    # 只回傳本次航班裡實際出現的代碼，避免傳送全球 2000 筆進瀏覽器造成記憶體崩潰
    all_flights = arr_all + dep_all
    used_airlines = {f.get("AirlineID") for f in all_flights if f.get("AirlineID")}
    used_airports = {
        code for f in all_flights
        for code in (f.get("DepartureAirportID"), f.get("ArrivalAirportID"))
        if code
    }
    filtered_airline_map = {k: v for k, v in airline_map.items() if k in used_airlines}
    filtered_airport_map = {k: v for k, v in airport_map.items() if k in used_airports}

    return jsonify({
        "configured":     True,
        "arrivals":       [f for f in arr_all if f.get("AirlineID") in _TIAS_CODES],
        "departures":     [f for f in dep_all if f.get("AirlineID") in _TIAS_CODES],
        "all_arrivals":   arr_all,
        "all_departures": dep_all,
        "airlines":       _TIAS_AIRLINES,
        "airline_map":    filtered_airline_map,
        "airport_map":    filtered_airport_map,
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


# ──────────────────────────────────────────────
#  週班表（定期航班班表）
#  資料來源：TDX 定期航班班表 API（每小時快取一次）
# ──────────────────────────────────────────────
_SCHEDULE_TTL     = 3600
_schedule_cache   = {"dep": None, "arr": None, "expires_at": 0.0}
_schedule_lock    = threading.Lock()
_TDX_SCHED_INTL   = "https://tdx.transportdata.tw/api/basic/v2/Air/GeneralSchedule/International"
_TDX_SCHED_DOM    = "https://tdx.transportdata.tw/api/basic/v2/Air/GeneralSchedule/Domestic"
_WEEKDAY_FIELDS   = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _fetch_schedule():
    with _schedule_lock:
        if _schedule_cache["dep"] is not None and time.time() < _schedule_cache["expires_at"]:
            return _schedule_cache["dep"], _schedule_cache["arr"], True

    token = _get_tdx_token()
    if not token:
        return None, None, False

    headers  = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    today    = _dt.now(_tz(_td(hours=8))).strftime("%Y-%m-%d")

    end_window = (_dt.now(_tz(_td(hours=8))) + _td(days=6)).strftime("%Y-%m-%d")

    def _get(ep: str, airport_field: str) -> list:
        try:
            r = _requests.get(
                ep,
                headers=headers,
                params={
                    "$format": "JSON",
                    "$top":    3000,
                    "$filter": f"{airport_field} eq 'TPE'",
                },
                timeout=20,
            )
            r.raise_for_status()
            body = r.json()
            data = body if isinstance(body, list) else body.get("data", [])
            # 保留與 7 天視窗有交集的所有週記錄
            # 條件：ScheduleStartDate ≤ 窗口結束 且 ScheduleEndDate ≥ 今天
            return [f for f in data
                    if f.get("ScheduleStartDate", "") <= end_window
                    and f.get("ScheduleEndDate", "9999-12-31") >= today]
        except Exception:
            return []

    dep_intl = _get(_TDX_SCHED_INTL, "DepartureAirportID")
    arr_intl = _get(_TDX_SCHED_INTL, "ArrivalAirportID")
    dep_dom  = _get(_TDX_SCHED_DOM,  "DepartureAirportID")
    arr_dom  = _get(_TDX_SCHED_DOM,  "ArrivalAirportID")

    dep = dep_intl + dep_dom
    arr = arr_intl + arr_dom

    with _schedule_lock:
        _schedule_cache["dep"] = dep
        _schedule_cache["arr"] = arr
        _schedule_cache["expires_at"] = time.time() + _SCHEDULE_TTL

    return dep, arr, True


def _flights_on_weekday(flights: list, weekday: int, for_date: str) -> list:
    """weekday: 0=Monday … 6=Sunday（Python convention）
    for_date: "YYYY-MM-DD"，確保該筆記錄在那天的班期範圍內。"""
    day_field = _WEEKDAY_FIELDS[weekday]
    return [f for f in flights
            if f.get(day_field)
            and f.get("ScheduleStartDate", "") <= for_date <= f.get("ScheduleEndDate", "9999-12-31")]


@app.route("/schedule")
def schedule():
    return render_template("schedule.html")


@app.route("/api/schedule/week")
def schedule_week_api():
    dep_all, arr_all, ok = _fetch_schedule()
    if not ok:
        return jsonify({"error": "TDX_CLIENT_ID / TDX_CLIENT_SECRET 未設定", "configured": False}), 503

    airasia_only = request.args.get("airasia", "1") == "1"
    deps = [f for f in dep_all if not airasia_only or f.get("AirlineID") in _TIAS_CODES]
    arrs = [f for f in arr_all if not airasia_only or f.get("AirlineID") in _TIAS_CODES]

    now_tw = _dt.now(_tz(_td(hours=8)))
    days   = []

    for i in range(7):
        d        = now_tw + _td(days=i)
        weekday  = d.weekday()
        date_str = d.strftime("%Y-%m-%d")

        d_flt = _flights_on_weekday(deps, weekday, date_str)
        a_flt = _flights_on_weekday(arrs, weekday, date_str)

        dep_hours = [0] * 24
        for f in d_flt:
            t = f.get("DepartureTime", "")
            if t and ":" in t:
                try:
                    dep_hours[int(t.split(":")[0])] += 1
                except Exception:
                    pass

        arr_hours = [0] * 24
        for f in a_flt:
            t = f.get("ArrivalTime", "")
            if t and ":" in t:
                try:
                    arr_hours[int(t.split(":")[0])] += 1
                except Exception:
                    pass

        days.append({
            "date":       d.strftime("%Y-%m-%d"),
            "weekday_zh": ["一", "二", "三", "四", "五", "六", "日"][weekday],
            "is_today":   i == 0,
            "dep_count":  len(d_flt),
            "arr_count":  len(a_flt),
            "total":      len(d_flt) + len(a_flt),
            "dep_hours":  dep_hours,
            "arr_hours":  arr_hours,
        })

    avg = sum(d["total"] for d in days) / 7
    for d in days:
        ratio = d["total"] / avg if avg > 0 else 0
        d["peak"] = "high" if ratio >= 1.5 else "mid" if ratio >= 1.2 else "normal"

    return jsonify({
        "configured":   True,
        "airasia_only": airasia_only,
        "days":         days,
        "total_dep":    len(deps),
        "total_arr":    len(arrs),
        "updated_at":   time.strftime("%H:%M:%S"),
    })


@app.route("/api/schedule/debug")
def schedule_debug():
    dep, arr, ok = _fetch_schedule()
    return jsonify({
        "tdx_configured": ok,
        "cached_dep":     len(dep) if dep else 0,
        "cached_arr":     len(arr) if arr else 0,
        "airasia_dep":    len([f for f in (dep or []) if f.get("AirlineID") in _TIAS_CODES]),
        "airasia_arr":    len([f for f in (arr or []) if f.get("AirlineID") in _TIAS_CODES]),
        "sample_dep":     (dep or [])[:2],
        "sample_arr":     (arr or [])[:2],
    })


# ──────────────────────────────────────────────
#  機場設施指南（/amenities）
#  TDX basic 層無 AirportFacility API → 使用精選靜態資料備援
# ──────────────────────────────────────────────
_TDX_AMENITY   = "https://tdx.transportdata.tw/api/basic/v2/Air/AirportFacility"
_AMENITY_TTL   = 3600
_amenity_cache: dict = {"data": None, "expires_at": 0.0, "source": "static"}
_amenity_lock  = threading.Lock()

_AMENITY_CATEGORIES = {
    "nursery":    {"label": "育嬰室",   "label_en": "Nursery",    "emoji": "🍼", "color": "#f43f5e"},
    "water":      {"label": "飲水機",   "label_en": "Water",      "emoji": "💧", "color": "#0ea5e9"},
    "exchange":   {"label": "換匯",     "label_en": "Currency",   "emoji": "💱", "color": "#f59e0b"},
    "taxrefund":  {"label": "退稅",     "label_en": "Tax Refund", "emoji": "🧾", "color": "#10b981"},
    "banking":    {"label": "銀行/ATM", "label_en": "Bank/ATM",   "emoji": "🏦", "color": "#6366f1"},
    "luggage":    {"label": "行李",     "label_en": "Luggage",    "emoji": "🧳", "color": "#8b5cf6"},
    "medical":    {"label": "醫療",     "label_en": "Medical",    "emoji": "🏥", "color": "#ef4444"},
    "shower":     {"label": "淋浴",     "label_en": "Shower",     "emoji": "🚿", "color": "#06b6d4"},
    "prayer":     {"label": "祈禱室",   "label_en": "Prayer",     "emoji": "🛐", "color": "#84cc16"},
    "shop":       {"label": "商店",     "label_en": "Shop",       "emoji": "🏪", "color": "#f97316"},
    "info":       {"label": "服務台",   "label_en": "Info",       "emoji": "ℹ️",  "color": "#3b82f6"},
    "accessible": {"label": "無障礙",   "label_en": "Access",     "emoji": "♿",  "color": "#64748b"},
}

# 桃園國際機場（TPE）精選設施靜態資料（中英雙語）
_TPE_AMENITIES = [
    # ── 育嬰室 ──────────────────────────────────
    {"id":1,  "name":"育嬰室",           "name_en":"Nursing Room",                  "category":"nursery",   "terminal":"T1","floor":"3F",  "zone":"出境安檢後","desc":"安全檢查後候機廊道 A 側，近 A4 登機門旁，提供哺乳、換尿布設備","desc_en":"Past security in Concourse A, near Gate A4. Breastfeeding & baby changing available.","tags":["育嬰","哺乳","換尿布","嬰兒","nursing","baby","breastfeed"]},
    {"id":2,  "name":"育嬰室",           "name_en":"Nursing Room",                  "category":"nursery",   "terminal":"T1","floor":"B1F", "zone":"入境大廳", "desc":"行李提領大廳 A 轉盤旁，入境通關後即可使用","desc_en":"Adjacent to Carousel A in the baggage claim hall, accessible immediately after customs.","tags":["育嬰","哺乳","嬰兒","nursing","baby"]},
    {"id":3,  "name":"育嬰室",           "name_en":"Nursing Room",                  "category":"nursery",   "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"安全檢查後 D 廊道入口，近免稅商店，24 小時開放","desc_en":"Concourse D entrance past security, near duty-free shops. Open 24 hours.","tags":["育嬰","哺乳","換尿布","嬰兒","nursing","baby","breastfeed"]},
    {"id":4,  "name":"育嬰室",           "name_en":"Nursing Room",                  "category":"nursery",   "terminal":"T2","floor":"1F",  "zone":"入境大廳", "desc":"入境出口右轉，計程車搭乘處前方走廊","desc_en":"Turn right past the arrival exit, in the corridor before the taxi stand.","tags":["育嬰","哺乳","嬰兒","nursing","baby"]},
    # ── 飲水機 ──────────────────────────────────
    {"id":5,  "name":"飲水機",           "name_en":"Water Fountain",                "category":"water",     "terminal":"T1","floor":"3F",  "zone":"出境安檢後","desc":"A/B 登機廊道各設兩台，溫熱冷三溫，免費使用","desc_en":"Two units in each of Concourses A & B. Hot, warm and cold water, free to use.","tags":["飲水","開水","熱水","冷水","water","fountain","drink"]},
    {"id":6,  "name":"飲水機",           "name_en":"Water Fountain",                "category":"water",     "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"D/E 廊道各廊道入口均有設置，建議進安檢後再補水","desc_en":"Located at each concourse entrance in D & E. Refill your bottle after clearing security.","tags":["飲水","開水","熱水","water","fountain","drink"]},
    {"id":7,  "name":"飲水機",           "name_en":"Water Fountain",                "category":"water",     "terminal":"T1","floor":"B1F", "zone":"入境大廳", "desc":"行李轉盤大廳出口走廊右側牆面","desc_en":"On the right side wall of the corridor exiting the baggage carousel hall.","tags":["飲水","開水","water","fountain"]},
    {"id":8,  "name":"飲水機",           "name_en":"Water Fountain",                "category":"water",     "terminal":"T2","floor":"1F",  "zone":"入境大廳", "desc":"入境出口通道兩側，靠近旅客服務中心","desc_en":"Both sides of the arrival exit passage, near the Passenger Service Center.","tags":["飲水","開水","water","fountain","drink"]},
    # ── 外幣兌換 ─────────────────────────────────
    {"id":9,  "name":"臺灣銀行 換匯",    "name_en":"Bank of Taiwan – Currency Exchange","category":"exchange",  "terminal":"T1","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"出境報到大廳左側，安全檢查前，鄰近旅平險服務台","desc_en":"Left side of departure hall, before security. Travel insurance desk nearby.","tags":["換匯","外幣","台銀","兌換","換錢","currency","exchange","money"]},
    {"id":10, "name":"臺灣銀行 換匯",    "name_en":"Bank of Taiwan – Currency Exchange","category":"exchange",  "terminal":"T1","floor":"3F",  "zone":"出境安檢後","desc":"安檢後 A 廊道入口右側，可兌換日圓、美金、歐元等主要幣別","desc_en":"Right of Concourse A entrance past security. JPY, USD, EUR and more available.","tags":["換匯","外幣","台銀","兌換","換錢","currency","exchange","money"]},
    {"id":11, "name":"臺灣銀行 換匯",    "name_en":"Bank of Taiwan – Currency Exchange","category":"exchange",  "terminal":"T1","floor":"B1F", "zone":"入境大廳", "desc":"行李轉盤出口後，入境通關前走廊","desc_en":"In the corridor between baggage carousels and customs.","tags":["換匯","外幣","台銀","兌換","currency","exchange","money"]},
    {"id":12, "name":"臺灣銀行 換匯",    "name_en":"Bank of Taiwan – Currency Exchange","category":"exchange",  "terminal":"T2","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"報到大廳 D 排報到區右側，人工服務 06:30–22:00","desc_en":"Right of Row D check-in counters in departure hall. Teller hours: 06:30–22:00.","tags":["換匯","外幣","台銀","兌換","換錢","currency","exchange","money"]},
    {"id":13, "name":"臺灣銀行 換匯",    "name_en":"Bank of Taiwan – Currency Exchange","category":"exchange",  "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"安檢後 E 廊道起點，免稅區入口旁","desc_en":"Start of Concourse E past security, beside the duty-free zone entrance.","tags":["換匯","外幣","台銀","兌換","currency","exchange"]},
    {"id":14, "name":"兆豐銀行 換匯",    "name_en":"Mega Bank – Currency Exchange",  "category":"exchange",  "terminal":"T1","floor":"1F",  "zone":"入境大廳", "desc":"入境出口右側大廳，提供 24 小時 ATM 及人工換匯","desc_en":"Right of the arrival exit. 24-hour ATM plus teller currency exchange available.","tags":["換匯","外幣","兆豐","兌換","換錢","currency","exchange","money"]},
    # ── 退稅 ───────────────────────────────────
    {"id":15, "name":"Global Blue 退稅", "name_en":"Global Blue – Tax Refund",       "category":"taxrefund", "terminal":"T1","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"出境安全檢查前，出發大廳左翼服務台，可退現金或刷回信用卡","desc_en":"Left-wing service desk in departure hall, before security. Cash or credit card refund.","tags":["退稅","Tax Refund","Global Blue","購物退稅","tax refund","vat","shopping"]},
    {"id":16, "name":"Global Blue 退稅", "name_en":"Global Blue – Tax Refund",       "category":"taxrefund", "terminal":"T2","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"報到大廳中央走道左側，D 排報到區斜前方","desc_en":"Left of the central walkway in departure hall, diagonally ahead of Row D check-in.","tags":["退稅","Tax Refund","Global Blue","購物退稅","tax refund","vat"]},
    {"id":17, "name":"Premier Tax Free", "name_en":"Premier Tax Free",               "category":"taxrefund", "terminal":"T2","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"緊鄰 Global Blue 服務台，同處可一次辦理兩家退稅","desc_en":"Right next to the Global Blue desk — process both refund providers in one stop.","tags":["退稅","Tax Refund","Premier","購物退稅","tax refund","vat","shopping"]},
    {"id":18, "name":"海關退稅蓋章",     "name_en":"Customs VAT Stamp Desk",         "category":"taxrefund", "terminal":"T1","floor":"3F",  "zone":"安檢前海關查驗","desc":"未開箱退稅商品需在此蓋章後才可至服務台領款，位於安檢排隊旁","desc_en":"Sealed goods must be stamped here before refund. Located beside the security queue.","tags":["退稅","海關","蓋章","驗貨","customs","stamp","vat"]},
    {"id":19, "name":"海關退稅蓋章",     "name_en":"Customs VAT Stamp Desk",         "category":"taxrefund", "terminal":"T2","floor":"3F",  "zone":"安檢前海關查驗","desc":"出境安檢入口旁海關查驗台，持退稅收據蓋章後再辦理退款","desc_en":"Customs inspection desk at the security entrance. Stamp your receipt before claiming refund.","tags":["退稅","海關","蓋章","驗貨","customs","stamp","vat"]},
    # ── 銀行/ATM ─────────────────────────────────
    {"id":20, "name":"ATM（臺灣銀行）",  "name_en":"ATM – Bank of Taiwan",           "category":"banking",   "terminal":"T1","floor":"1F",  "zone":"入境大廳", "desc":"入境出口大廳轉角，可提領台幣及外幣，24 小時服務","desc_en":"Corner of the arrival hall. TWD and foreign currency withdrawals, open 24 hours.","tags":["ATM","提款","台銀","現金","cash","withdraw"]},
    {"id":21, "name":"ATM（臺灣銀行）",  "name_en":"ATM – Bank of Taiwan",           "category":"banking",   "terminal":"T1","floor":"3F",  "zone":"出境大廳", "desc":"出境報到大廳，換匯服務台旁外側牆面","desc_en":"Outer wall beside the currency exchange desk in the departure hall.","tags":["ATM","提款","台銀","現金","cash","withdraw"]},
    {"id":22, "name":"ATM（兆豐銀行）",  "name_en":"ATM – Mega Bank",                "category":"banking",   "terminal":"T2","floor":"3F",  "zone":"出境大廳", "desc":"報到大廳 7-Eleven 便利商店旁，24 小時開放","desc_en":"Next to 7-Eleven in the departure hall, open 24 hours.","tags":["ATM","提款","兆豐","現金","cash","withdraw"]},
    {"id":23, "name":"ATM（中華郵政）",  "name_en":"ATM – Chunghwa Post",            "category":"banking",   "terminal":"T2","floor":"1F",  "zone":"入境大廳", "desc":"入境旅客服務中心旁，支援 VISA/Master 海外提款","desc_en":"Next to the arrival Passenger Service Center. Accepts VISA/Mastercard international cards.","tags":["ATM","提款","郵局","現金","cash","withdraw","visa","mastercard"]},
    {"id":24, "name":"旅行平安保險",     "name_en":"Travel Insurance",               "category":"banking",   "terminal":"T1","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"臺灣銀行換匯台旁保險服務台，可當場購買旅遊平安險","desc_en":"Insurance desk beside Bank of Taiwan's exchange counter. Purchase travel insurance on the spot.","tags":["保險","旅平險","投保","意外險","insurance","travel insurance"]},
    {"id":25, "name":"旅行平安保險",     "name_en":"Travel Insurance",               "category":"banking",   "terminal":"T2","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"兆豐銀行服務台可辦旅平險，亦有自動投保機","desc_en":"Available at Mega Bank service desk. Self-service insurance kiosks also available.","tags":["保險","旅平險","投保","意外險","insurance","travel insurance"]},
    # ── 行李服務 ─────────────────────────────────
    {"id":26, "name":"行李寄存",         "name_en":"Luggage Storage",                "category":"luggage",   "terminal":"T1","floor":"B1F", "zone":"行李提領大廳","desc":"行李轉盤大廳出口右側，24 小時服務，依件數/天數計費","desc_en":"Right of the baggage carousel hall exit. Open 24 hours. Charged per item per day.","tags":["寄存","行李","寄放","存放","luggage storage","locker","left luggage"]},
    {"id":27, "name":"行李寄存",         "name_en":"Luggage Storage",                "category":"luggage",   "terminal":"T2","floor":"1F",  "zone":"入境大廳", "desc":"入境通關後一樓，近巴士售票處，可短期或多日寄放","desc_en":"Ground floor after customs, near the bus ticket counter. Short or multi-day storage.","tags":["寄存","行李","寄放","存放","luggage storage","locker","left luggage"]},
    {"id":28, "name":"行李縮膜包裝",     "name_en":"Luggage Wrapping",               "category":"luggage",   "terminal":"T1","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"報到大廳中央服務台附近，依件計費，可防止行李箱損壞","desc_en":"Near the central service counter in the departure hall. Charged per item. Prevents damage.","tags":["包裝","纏膜","行李保護","縮膜","wrapping","wrap","suitcase"]},
    {"id":29, "name":"行李縮膜包裝",     "name_en":"Luggage Wrapping",               "category":"luggage",   "terminal":"T2","floor":"3F",  "zone":"出境大廳", "desc":"D 排報到區左側，服務時間 06:00–最後班機","desc_en":"Left of Row D check-in. Service hours: 06:00 – last departure.","tags":["包裝","纏膜","行李保護","縮膜","wrapping","wrap","suitcase"]},
    {"id":30, "name":"DHL 快遞服務",     "name_en":"DHL Express",                    "category":"luggage",   "terminal":"T2","floor":"3F",  "zone":"出境大廳", "desc":"報到大廳旁，可辦理國際快遞寄件及超重行李單獨托運","desc_en":"Next to the departure hall. International courier and overweight baggage shipping.","tags":["快遞","DHL","托運","寄件","寄包裹","courier","parcel","shipping"]},
    {"id":31, "name":"行李超重寄送",     "name_en":"Overweight Baggage",             "category":"luggage",   "terminal":"T1","floor":"3F",  "zone":"出境大廳", "desc":"各航空公司報到櫃台可詢問超重行李加購，或洽機場快遞服務","desc_en":"Enquire at airline check-in desks for add-on fees, or contact the airport courier service.","tags":["超重","行李","加購","托運","overweight","excess baggage"]},
    # ── 醫療/急救 ─────────────────────────────────
    {"id":32, "name":"醫療急救站",       "name_en":"Medical First Aid Station",      "category":"medical",   "terminal":"T1","floor":"3F",  "zone":"出境大廳", "desc":"出境大廳靠近 A 出口，設有 AED 及基本急救設備，24 小時有護理人員","desc_en":"Near Exit A in the departure hall. AED and first aid on-site. Nurse on duty 24 hours.","tags":["醫療","急救","AED","護士","救護","medical","first aid","emergency","nurse"]},
    {"id":33, "name":"醫療急救站",       "name_en":"Medical First Aid Station",      "category":"medical",   "terminal":"T2","floor":"3F",  "zone":"出境大廳", "desc":"出境大廳中央服務台旁，有 AED 掛壁，駐點護理人員","desc_en":"Next to the central service counter in the departure hall. AED on-site, nurse on duty.","tags":["醫療","急救","AED","護士","救護","medical","first aid","emergency","nurse"]},
    {"id":34, "name":"機場診所",         "name_en":"Airport Clinic",                 "category":"medical",   "terminal":"T2","floor":"1F",  "zone":"入境大廳", "desc":"入境出口右側，提供一般門診及藥品，服務時間 08:00–20:00","desc_en":"Right of the arrival exit. General consultation and medicine. Hours: 08:00–20:00.","tags":["診所","藥局","看診","藥品","醫療","clinic","pharmacy","doctor","medicine"]},
    {"id":35, "name":"AED 體外電擊器",   "name_en":"AED Defibrillator",              "category":"medical",   "terminal":"T1","floor":"3F",  "zone":"出境安檢後","desc":"A/B 各登機廊道均設有 AED，紅色標示清楚，同仁可協助使用","desc_en":"Red wall-mounted cabinets in Concourses A & B. Staff available to assist.","tags":["AED","急救","心臟","電擊器","defibrillator","heart","emergency"]},
    {"id":36, "name":"AED 體外電擊器",   "name_en":"AED Defibrillator",              "category":"medical",   "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"D/E 廊道入口及中段各設一台，位置有黃色地板標示","desc_en":"At the entrance and midpoint of Concourses D & E. Yellow floor markings indicate location.","tags":["AED","急救","心臟","電擊器","defibrillator","heart","emergency"]},
    # ── 淋浴/休憩 ─────────────────────────────────
    {"id":37, "name":"淋浴間",           "name_en":"Shower Room",                    "category":"shower",    "terminal":"T1","floor":"3F",  "zone":"出境安檢後","desc":"安檢後 A 廊道近 A10 登機門，需付費購票，提供毛巾備品","desc_en":"Near Gate A10 in Concourse A (airside). Ticketed entry. Towels and toiletries provided.","tags":["淋浴","沐浴","梳洗","盥洗","shower","wash","clean up"]},
    {"id":38, "name":"淋浴間",           "name_en":"Shower Room",                    "category":"shower",    "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"安檢後 D 廊道，近星宇貴賓室附近，付費對外開放","desc_en":"Concourse D (airside), near the Starlux lounge. Paid access open to all passengers.","tags":["淋浴","沐浴","梳洗","盥洗","shower","wash","clean up"]},
    # ── 祈禱室 ──────────────────────────────────
    {"id":39, "name":"祈禱室",           "name_en":"Prayer Room",                    "category":"prayer",    "terminal":"T1","floor":"3F",  "zone":"出境安檢後","desc":"安檢後 B 廊道，近 B5 登機門附近，設有洗淨設備，全日開放","desc_en":"Concourse B (airside), near Gate B5. Ablution facilities available. Open all day.","tags":["祈禱","禮拜","穆斯林","清真","伊斯蘭","prayer","mosque","muslim","islamic","worship"]},
    {"id":40, "name":"祈禱室",           "name_en":"Prayer Room",                    "category":"prayer",    "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"安檢後 E 廊道，近 E1 登機門，設有獨立洗淨區，指向麥加方向","desc_en":"Concourse E (airside), near Gate E1. Separate ablution area. Qibla direction marked.","tags":["祈禱","禮拜","穆斯林","清真","伊斯蘭","prayer","mosque","muslim","islamic","worship","qibla"]},
    # ── 商店/便利 ─────────────────────────────────
    {"id":41, "name":"7-Eleven",         "name_en":"7-Eleven Convenience Store",     "category":"shop",      "terminal":"T1","floor":"3F",  "zone":"出境大廳（安檢前）","desc":"出境報到大廳，可購買旅行用品、食品飲料，24 小時營業","desc_en":"Departure hall near check-in counters. Travel essentials, food & drinks. Open 24 hours.","tags":["超商","7-11","便利商店","購物","convenience store","snacks","drinks"]},
    {"id":42, "name":"7-Eleven",         "name_en":"7-Eleven Convenience Store",     "category":"shop",      "terminal":"T2","floor":"3F",  "zone":"出境大廳", "desc":"報到大廳近中央服務台，24 小時，備有雨衣、轉接頭等旅行小物","desc_en":"Near the central service counter in departure hall. Open 24 hours. Travel accessories available.","tags":["超商","7-11","便利商店","購物","convenience store","snacks","drinks"]},
    {"id":43, "name":"藥妝店（松本清）", "name_en":"Matsumoto Kiyoshi (Drugstore)",  "category":"shop",      "terminal":"T2","floor":"3F",  "zone":"出境安檢後","desc":"免稅區入口旁，可購買護膚品、藥妝，結合免稅折扣","desc_en":"Next to the duty-free zone entrance (airside). Skincare, cosmetics with duty-free discount.","tags":["藥妝","美妝","保養","松本清","drugstore","cosmetics","skincare","beauty"]},
    # ── 旅客服務台 ───────────────────────────────
    {"id":44, "name":"旅客服務中心",     "name_en":"Passenger Service Center",       "category":"info",      "terminal":"T1","floor":"1F",  "zone":"入境大廳", "desc":"入境出口正對面，提供旅遊諮詢、地圖索取、WiFi 分享器租借","desc_en":"Directly across from the arrival exit. Travel info, maps, and Wi-Fi router rentals.","tags":["服務台","諮詢","WiFi","SIM卡","問路","WiFi租借","information","service center","help desk"]},
    {"id":45, "name":"旅客服務中心",     "name_en":"Passenger Service Center",       "category":"info",      "terminal":"T2","floor":"1F",  "zone":"入境大廳", "desc":"入境通關後一樓，近巴士售票處，提供中英日語服務","desc_en":"Ground floor after customs, near the bus ticket counter. Staff speak Chinese, English, Japanese.","tags":["服務台","諮詢","WiFi","SIM卡","問路","巴士","information","service center","help","english"]},
    {"id":46, "name":"機場免費Wi-Fi",    "name_en":"Free Airport Wi-Fi",             "category":"info",      "terminal":"T1","floor":"全廳", "zone":"全航廈",   "desc":"全航廈免費 Wi-Fi：Airport-Free-WiFi_auto；登入後直接使用","desc_en":'Free Wi-Fi throughout the terminal. Network: "Airport-Free-WiFi_auto". Sign in to connect.',"tags":["WiFi","網路","免費","上網","wifi","internet","free wifi"]},
    {"id":47, "name":"機場免費Wi-Fi",    "name_en":"Free Airport Wi-Fi",             "category":"info",      "terminal":"T2","floor":"全廳", "zone":"全航廈",   "desc":"全航廈免費 Wi-Fi：Airport-Free-WiFi_auto；無需密碼直接連線","desc_en":'Free Wi-Fi throughout the terminal. Network: "Airport-Free-WiFi_auto". No password needed.',"tags":["WiFi","網路","免費","上網","wifi","internet","free wifi"]},
    {"id":48, "name":"SIM 卡販售",       "name_en":"SIM Card Sales",                 "category":"info",      "terminal":"T1","floor":"1F",  "zone":"入境大廳", "desc":"旅客服務中心及中華電信櫃台，可購買台灣數據 SIM，中英文服務","desc_en":"Passenger Service Center and Chunghwa Telecom counter. Taiwan data SIM, English service.","tags":["SIM卡","網路","電話卡","中華電信","遠傳","sim card","mobile data","prepaid","phone"]},
    # ── 無障礙設施 ───────────────────────────────
    {"id":49, "name":"無障礙洗手間",     "name_en":"Accessible Restroom",            "category":"accessible","terminal":"T1","floor":"各樓", "zone":"各樓層洗手間旁","desc":"各樓層一般廁所旁均設無障礙廁所，門口有輪椅標示，空間寬敞","desc_en":"Adjacent to regular restrooms on each floor. Wheelchair-accessible, clearly marked.","tags":["無障礙","殘障","輪椅","廁所","accessible","wheelchair","restroom","toilet","disabled"]},
    {"id":50, "name":"無障礙洗手間",     "name_en":"Accessible Restroom",            "category":"accessible","terminal":"T2","floor":"各樓", "zone":"各樓層洗手間旁","desc":"T2 各樓層廁所均附設無障礙廁所，部分含嬰兒換尿布台","desc_en":"Available on every floor. Some include baby changing tables.","tags":["無障礙","殘障","輪椅","廁所","accessible","wheelchair","restroom","toilet","disabled"]},
    {"id":51, "name":"輪椅借用服務",     "name_en":"Wheelchair Loan",                "category":"accessible","terminal":"T1","floor":"1F",  "zone":"旅客服務中心","desc":"入境大廳旅客服務中心可免費借用輪椅，行動不便旅客可事先通知航空公司安排","desc_en":"Free wheelchair loan at the Passenger Service Center. Notify your airline in advance for electric wheelchair assistance.","tags":["輪椅","借用","無障礙","行動不便","wheelchair","loan","mobility","disabled"]},
    {"id":52, "name":"輪椅借用服務",     "name_en":"Wheelchair Loan",                "category":"accessible","terminal":"T2","floor":"1F",  "zone":"旅客服務中心","desc":"提供輪椅免費借用，亦可至各航空公司報到台申請電動輪椅協助","desc_en":"Free wheelchair loan available. Electric wheelchair assistance can be arranged at airline check-in.","tags":["輪椅","借用","無障礙","行動不便","wheelchair","loan","mobility","disabled"]},
]


def _fetch_amenities() -> tuple[list, str]:
    """回傳 (amenity_list, source)；1h 快取，TDX 失敗則用靜態資料。"""
    with _amenity_lock:
        if _amenity_cache["data"] and time.time() < _amenity_cache["expires_at"]:
            return _amenity_cache["data"], _amenity_cache["source"]

    token = _get_tdx_token()
    if token:
        try:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            r = _requests.get(
                _TDX_AMENITY, headers=headers,
                params={"$format": "JSON", "$top": 1000, "$filter": "AirportID eq 'TPE'"},
                timeout=12,
            )
            if r.status_code == 200:
                body = r.json()
                raw  = body if isinstance(body, list) else body.get("data", [])
                if raw:
                    cleaned = _clean_tdx_amenities(raw)
                    with _amenity_lock:
                        _amenity_cache.update({"data": cleaned, "expires_at": time.time() + _AMENITY_TTL, "source": "tdx"})
                    return cleaned, "tdx"
        except Exception:
            pass

    with _amenity_lock:
        _amenity_cache.update({"data": _TPE_AMENITIES, "expires_at": time.time() + _AMENITY_TTL, "source": "static"})
    return _TPE_AMENITIES, "static"


def _clean_tdx_amenities(raw: list) -> list:
    """TDX AirportFacility 欄位轉為統一格式（若 API 有朝一日開放）。"""
    result = []
    cat_map = {
        "nursery": "nursery", "baby": "nursery",
        "water":   "water",   "drink": "water",
        "exchange":"exchange","currency":"exchange","fx":"exchange",
        "tax":     "taxrefund","refund":"taxrefund",
        "atm":     "banking", "bank":"banking","insurance":"banking",
        "luggage": "luggage", "baggage":"luggage","storage":"luggage",
        "medical": "medical", "clinic":"medical","pharmacy":"medical",
        "shower":  "shower",
        "prayer":  "prayer",  "worship":"prayer",
        "shop":    "shop",    "store":"shop","convenience":"shop",
        "info":    "info",    "service":"info",
        "access":  "accessible","wheelchair":"accessible",
    }
    for i, a in enumerate(raw):
        name_obj = a.get("FacilityName", {})
        name = name_obj.get("Zh_tw", "") if isinstance(name_obj, dict) else str(name_obj or "")
        loc_obj = a.get("LocationDescription", {})
        desc = loc_obj.get("Zh_tw", "") if isinstance(loc_obj, dict) else str(loc_obj or "")
        raw_cat = str(a.get("FacilityType", "")).lower()
        cat = next((v for k, v in cat_map.items() if k in raw_cat), "info")
        result.append({
            "id":       i + 1,
            "name":     name or a.get("FacilityID", ""),
            "category": cat,
            "terminal": a.get("TerminalID", ""),
            "floor":    a.get("Floor", ""),
            "zone":     a.get("Area", ""),
            "desc":     desc,
            "tags":     [name],
        })
    return result


@app.route("/visa-guide")
def visa_guide():
    return render_template("visa-guide.html")


@app.route("/code-dictionary")
def code_dictionary():
    return render_template("code_dictionary.html")


@app.route("/amenities")
def amenities():
    return render_template("amenities.html")


@app.route("/api/amenities")
def amenities_api():
    data, source = _fetch_amenities()
    return jsonify({
        "data":       data,
        "categories": _AMENITY_CATEGORIES,
        "source":     source,
        "count":      len(data),
        "updated_at": time.strftime("%H:%M:%S"),
    })


# ──────────────────────────────────────────────
#  Share 剪貼簿後端（Upstash Redis 優先，本機 fallback in-memory）
# ──────────────────────────────────────────────
_SHARE_TTL  = 43200  # 12 小時

# ── in-memory fallback（本機開發用）────────────
_SHARE: dict      = {}
_SHARE_LOCK       = threading.Lock()

def _share_cleanup():
    while True:
        time.sleep(3600)
        cutoff = time.time() - _SHARE_TTL
        with _SHARE_LOCK:
            expired = [k for k, v in _SHARE.items() if v.get("updated_at", 0) < cutoff]
            for k in expired:
                del _SHARE[k]

threading.Thread(target=_share_cleanup, daemon=True).start()

# ── Upstash Redis REST helpers ──────────────────
def _redis(method: str, *args):
    """執行單一 Upstash Redis REST 命令；失敗回傳 None。"""
    url   = os.environ.get("KV_REST_API_URL", "")
    token = os.environ.get("KV_REST_API_TOKEN", "")
    if not url or not token:
        return None
    try:
        r = _requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=[method, *[str(a) for a in args]],
            timeout=5,
        )
        return r.json().get("result")
    except Exception:
        return None

def _share_get(code: str) -> dict | None:
    raw = _redis("GET", f"share:{code}")
    if raw is None:
        # fallback
        with _SHARE_LOCK:
            v = _SHARE.get(code)
            return dict(v) if v else None
    try:
        return _json.loads(raw)
    except Exception:
        return None

def _share_set(code: str, room: dict):
    serialized = _json.dumps(room)
    result = _redis("SET", f"share:{code}", serialized, "EX", _SHARE_TTL)
    if result is None:
        with _SHARE_LOCK:
            _SHARE[code] = room

def _share_del(code: str):
    result = _redis("DEL", f"share:{code}")
    if result is None:
        with _SHARE_LOCK:
            _SHARE.pop(code, None)

def _share_exists(code: str) -> bool:
    result = _redis("EXISTS", f"share:{code}")
    if result is not None:
        return bool(int(result))
    with _SHARE_LOCK:
        return code in _SHARE

def _sc(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/share")
def share():
    return render_template("share.html")

@app.route("/api/share/imagekit-auth")
def imagekit_auth():
    private_key = os.environ.get("IMAGEKIT_PRIVATE_KEY", "")
    if not private_key:
        return _sc(jsonify({"error": "IMAGEKIT_PRIVATE_KEY not set"})), 503
    token  = secrets.token_hex(16)
    expire = int(time.time()) + 1800
    signature = hmac.new(
        private_key.encode(),
        (token + str(expire)).encode(),
        hashlib.sha1
    ).hexdigest()
    return _sc(jsonify({"token": token, "expire": expire, "signature": signature}))

@app.route("/api/share/room", methods=["POST", "OPTIONS"])
def share_create():
    if request.method == "OPTIONS":
        return _sc(jsonify({}))
    now  = time.time()
    code = None
    for _ in range(10):
        c = str(random.randint(1000, 9999))
        if not _share_exists(c):
            _share_set(c, {"type": "text", "content": "", "file_name": "", "updated_at": now})
            code = c
            break
    if not code:
        return _sc(jsonify({"error": "retry later"})), 503
    return _sc(jsonify({"code": code}))

@app.route("/api/share/room/<code>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
def share_room(code):
    if request.method == "OPTIONS":
        return _sc(jsonify({}))
    if request.method == "GET":
        room = _share_get(code)
        if not room:
            return _sc(jsonify({"error": "not found"})), 404
        return _sc(jsonify({
            "type":       room.get("type", "text"),
            "content":    room.get("content", ""),
            "file_name":  room.get("file_name", ""),
            "updated_at": room.get("updated_at", 0),
        }))
    elif request.method == "PUT":
        room = _share_get(code)
        if not room:
            return _sc(jsonify({"error": "not found"})), 404
        body = request.get_json(silent=True) or {}
        if "type"      in body: room["type"]      = body["type"]
        if "content"   in body: room["content"]   = body["content"]
        if "file_name" in body: room["file_name"] = body["file_name"]
        room["updated_at"] = time.time()
        _share_set(code, room)
        return _sc(jsonify({"ok": True}))
    elif request.method == "DELETE":
        _share_del(code)
        return _sc(jsonify({"ok": True}))


# ══════════════════════════════════════════════
#  Weather 飛安天氣防禦面板（CWA API）
# ══════════════════════════════════════════════
_CWA_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"

# 30 分鐘快取
_WEATHER_CACHE: dict = {}
_WEATHER_CACHE_LOCK = threading.Lock()
_WEATHER_CACHE_TTL  = 1800

# 縣市 → CWA 鄉鎮預報端點 ID
_COUNTY_ENDPOINT = {
    # 偶數端點 = 12 小時預報，含最高/最低溫、最高體感溫、12h降雨機率
    "宜蘭縣": "F-D0047-003", "桃園市": "F-D0047-007", "新竹縣": "F-D0047-011",
    "苗栗縣": "F-D0047-015", "彰化縣": "F-D0047-019", "南投縣": "F-D0047-023",
    "雲林縣": "F-D0047-027", "嘉義縣": "F-D0047-031", "屏東縣": "F-D0047-035",
    "臺東縣": "F-D0047-039", "花蓮縣": "F-D0047-043", "澎湖縣": "F-D0047-047",
    "基隆市": "F-D0047-051", "新竹市": "F-D0047-055", "嘉義市": "F-D0047-059",
    "臺北市": "F-D0047-063", "高雄市": "F-D0047-067", "新北市": "F-D0047-071",
    "臺中市": "F-D0047-075", "臺南市": "F-D0047-079", "連江縣": "F-D0047-083",
    "金門縣": "F-D0047-087",
}

# Wx 代碼 → emoji（CWA 1-42）
_WX_EMOJI = {
    1:"☀️",2:"🌤️",3:"⛅",4:"⛅",5:"🌥️",6:"☁️",7:"☁️",
    8:"🌦️",9:"🌧️",10:"⛈️",11:"🌧️",12:"⛈️",13:"🌦️",
    14:"⛈️",15:"🌦️",16:"🌦️",17:"⛈️",18:"🌦️",19:"⛈️",
    20:"🌦️",21:"⛈️",22:"⛈️",23:"🌦️",24:"🌫️",25:"🌧️",
    26:"🌧️",27:"⛈️",28:"⛈️",29:"🌧️",30:"🌫️",31:"🌫️",
    32:"🌧️",33:"🌧️",34:"⛈️",35:"🌫️",36:"🌫️",37:"🌦️",
    38:"⛈️",39:"🌦️",40:"⛈️",41:"🌦️",42:"⛈️",
}
_ALERT_WX_CODES = {10,12,14,17,19,21,22,25,26,27,28,34,38,40,42}

# 完整台灣鄉鎮市區資料庫：(顯示名, CWA 地名, 縣市, 緯度, 經度)
_TOWNSHIPS = [
    # ── 臺北市 (12) ──
    ("台北市中正區","中正區","臺北市",25.0439,121.5198),
    ("台北市大同區","大同區","臺北市",25.0621,121.5132),
    ("台北市中山區","中山區","臺北市",25.0636,121.5244),
    ("台北市松山區","松山區","臺北市",25.0497,121.5653),
    ("台北市大安區","大安區","臺北市",25.0262,121.5433),
    ("台北市萬華區","萬華區","臺北市",25.0352,121.5003),
    ("台北市信義區","信義區","臺北市",25.0336,121.5644),
    ("台北市士林區","士林區","臺北市",25.0930,121.5252),
    ("台北市北投區","北投區","臺北市",25.1322,121.4990),
    ("台北市內湖區","內湖區","臺北市",25.0632,121.5878),
    ("台北市南港區","南港區","臺北市",25.0539,121.6068),
    ("台北市文山區","文山區","臺北市",24.9993,121.5697),
    # ── 新北市 (29) ──
    ("新北市板橋區","板橋區","新北市",25.0118,121.4648),
    ("新北市三重區","三重區","新北市",25.0627,121.4875),
    ("新北市中和區","中和區","新北市",24.9963,121.4999),
    ("新北市永和區","永和區","新北市",25.0143,121.5183),
    ("新北市新莊區","新莊區","新北市",25.0401,121.4429),
    ("新北市新店區","新店區","新北市",24.9628,121.5412),
    ("新北市樹林區","樹林區","新北市",24.9900,121.4176),
    ("新北市鶯歌區","鶯歌區","新北市",24.9583,121.3450),
    ("新北市三峽區","三峽區","新北市",24.9352,121.3720),
    ("新北市淡水區","淡水區","新北市",25.1679,121.4473),
    ("新北市汐止區","汐止區","新北市",25.0643,121.6558),
    ("新北市瑞芳區","瑞芳區","新北市",25.1090,121.8020),
    ("新北市土城區","土城區","新北市",24.9700,121.4392),
    ("新北市蘆洲區","蘆洲區","新北市",25.0835,121.4690),
    ("新北市五股區","五股區","新北市",25.0821,121.4346),
    ("新北市泰山區","泰山區","新北市",25.0589,121.4289),
    ("新北市林口區","林口區","新北市",25.0801,121.3821),
    ("新北市深坑區","深坑區","新北市",25.0007,121.6143),
    ("新北市石碇區","石碇區","新北市",24.9761,121.6579),
    ("新北市坪林區","坪林區","新北市",24.9307,121.7107),
    ("新北市三芝區","三芝區","新北市",25.2583,121.5018),
    ("新北市石門區","石門區","新北市",25.2920,121.5720),
    ("新北市八里區","八里區","新北市",25.1422,121.4066),
    ("新北市平溪區","平溪區","新北市",25.0171,121.7377),
    ("新北市雙溪區","雙溪區","新北市",25.0271,121.8692),
    ("新北市貢寮區","貢寮區","新北市",25.0162,121.9202),
    ("新北市金山區","金山區","新北市",25.2228,121.6368),
    ("新北市萬里區","萬里區","新北市",25.1830,121.6918),
    ("新北市烏來區","烏來區","新北市",24.8674,121.5503),
    # ── 基隆市 (7) ──
    ("基隆市仁愛區","仁愛區","基隆市",25.1288,121.7417),
    ("基隆市信義區","信義區","基隆市",25.1300,121.7266),
    ("基隆市中正區","中正區","基隆市",25.1143,121.7406),
    ("基隆市中山區","中山區","基隆市",25.1390,121.7365),
    ("基隆市安樂區","安樂區","基隆市",25.1495,121.7118),
    ("基隆市暖暖區","暖暖區","基隆市",25.1049,121.7694),
    ("基隆市七堵區","七堵區","基隆市",25.1060,121.7261),
    # ── 桃園市 (13) ──
    ("桃園市桃園區","桃園區","桃園市",24.9936,121.3010),
    ("桃園市中壢區","中壢區","桃園市",24.9700,121.3108),
    ("桃園市大溪區","大溪區","桃園市",24.8777,121.2882),
    ("桃園市楊梅區","楊梅區","桃園市",24.9143,121.1430),
    ("桃園市蘆竹區","蘆竹區","桃園市",25.0570,121.2960),
    ("桃園市大園區","大園區","桃園市",25.0780,121.2320),
    ("桃園市龜山區","龜山區","桃園市",25.0391,121.3486),
    ("桃園市八德區","八德區","桃園市",24.9537,121.3031),
    ("桃園市龍潭區","龍潭區","桃園市",24.8620,121.2150),
    ("桃園市平鎮區","平鎮區","桃園市",24.9471,121.2203),
    ("桃園市新屋區","新屋區","桃園市",24.9743,121.0848),
    ("桃園市觀音區","觀音區","桃園市",24.9981,121.1441),
    ("桃園市復興區","復興區","桃園市",24.7922,121.3598),
    # ── 新竹市 (3) ──
    ("新竹市東區","東區","新竹市",24.8020,120.9710),
    ("新竹市北區","北區","新竹市",24.8310,120.9642),
    ("新竹市香山區","香山區","新竹市",24.7683,120.9340),
    # ── 新竹縣 (13) ──
    ("新竹縣竹北市","竹北市","新竹縣",24.8232,121.0043),
    ("新竹縣湖口鄉","湖口鄉","新竹縣",24.8993,121.0390),
    ("新竹縣新豐鄉","新豐鄉","新竹縣",24.9341,121.0430),
    ("新竹縣新埔鎮","新埔鎮","新竹縣",24.8395,121.0752),
    ("新竹縣關西鎮","關西鎮","新竹縣",24.7946,121.1766),
    ("新竹縣芎林鄉","芎林鄉","新竹縣",24.7725,121.0607),
    ("新竹縣寶山鄉","寶山鄉","新竹縣",24.7540,120.9889),
    ("新竹縣竹東鎮","竹東鎮","新竹縣",24.7360,121.0888),
    ("新竹縣五峰鄉","五峰鄉","新竹縣",24.6393,121.0052),
    ("新竹縣橫山鄉","橫山鄉","新竹縣",24.7336,121.1093),
    ("新竹縣尖石鄉","尖石鄉","新竹縣",24.6660,121.1918),
    ("新竹縣北埔鄉","北埔鄉","新竹縣",24.6963,121.0546),
    ("新竹縣峨眉鄉","峨眉鄉","新竹縣",24.6726,121.0160),
    # ── 苗栗縣 (18) ──
    ("苗栗縣竹南鎮","竹南鎮","苗栗縣",24.6878,120.8838),
    ("苗栗縣頭份市","頭份市","苗栗縣",24.6833,120.9007),
    ("苗栗縣三灣鄉","三灣鄉","苗栗縣",24.6475,120.8752),
    ("苗栗縣南庄鄉","南庄鄉","苗栗縣",24.5940,121.0048),
    ("苗栗縣頭屋鄉","頭屋鄉","苗栗縣",24.5867,120.8935),
    ("苗栗縣公館鄉","公館鄉","苗栗縣",24.5136,120.8296),
    ("苗栗縣苗栗市","苗栗市","苗栗縣",24.5634,120.8195),
    ("苗栗縣苑裡鎮","苑裡鎮","苗栗縣",24.4289,120.6667),
    ("苗栗縣通霄鎮","通霄鎮","苗栗縣",24.4876,120.6916),
    ("苗栗縣造橋鄉","造橋鄉","苗栗縣",24.5474,120.8713),
    ("苗栗縣後龍鎮","後龍鎮","苗栗縣",24.5962,120.8014),
    ("苗栗縣西湖鄉","西湖鄉","苗栗縣",24.4822,120.7955),
    ("苗栗縣銅鑼鄉","銅鑼鄉","苗栗縣",24.4924,120.7453),
    ("苗栗縣三義鄉","三義鄉","苗栗縣",24.3983,120.7589),
    ("苗栗縣大湖鄉","大湖鄉","苗栗縣",24.4289,120.8566),
    ("苗栗縣獅潭鄉","獅潭鄉","苗栗縣",24.5000,120.9140),
    ("苗栗縣泰安鄉","泰安鄉","苗栗縣",24.3985,121.0151),
    ("苗栗縣卓蘭鎮","卓蘭鎮","苗栗縣",24.3148,120.8220),
    # ── 臺中市 (29) ──
    ("台中市中區","中區","臺中市",24.1458,120.6780),
    ("台中市東區","東區","臺中市",24.1407,120.7008),
    ("台中市南區","南區","臺中市",24.1228,120.6718),
    ("台中市西區","西區","臺中市",24.1485,120.6634),
    ("台中市北區","北區","臺中市",24.1627,120.6747),
    ("台中市北屯區","北屯區","臺中市",24.1743,120.7069),
    ("台中市西屯區","西屯區","臺中市",24.1643,120.6328),
    ("台中市南屯區","南屯區","臺中市",24.1350,120.6441),
    ("台中市太平區","太平區","臺中市",24.1268,120.7270),
    ("台中市大里區","大里區","臺中市",24.1022,120.6778),
    ("台中市霧峰區","霧峰區","臺中市",24.0540,120.6966),
    ("台中市烏日區","烏日區","臺中市",24.1272,120.6847),
    ("台中市豐原區","豐原區","臺中市",24.2376,120.7205),
    ("台中市后里區","后里區","臺中市",24.3097,120.6897),
    ("台中市石岡區","石岡區","臺中市",24.2611,120.7786),
    ("台中市東勢區","東勢區","臺中市",24.2557,120.8198),
    ("台中市和平區","和平區","臺中市",24.3614,121.1700),
    ("台中市新社區","新社區","臺中市",24.2199,120.8096),
    ("台中市潭子區","潭子區","臺中市",24.2090,120.7064),
    ("台中市大雅區","大雅區","臺中市",24.2182,120.6619),
    ("台中市神岡區","神岡區","臺中市",24.2547,120.6673),
    ("台中市大肚區","大肚區","臺中市",24.1508,120.5506),
    ("台中市沙鹿區","沙鹿區","臺中市",24.1959,120.5871),
    ("台中市龍井區","龍井區","臺中市",24.1648,120.5604),
    ("台中市梧棲區","梧棲區","臺中市",24.2488,120.5283),
    ("台中市清水區","清水區","臺中市",24.2710,120.5648),
    ("台中市大甲區","大甲區","臺中市",24.3422,120.6204),
    ("台中市外埔區","外埔區","臺中市",24.3099,120.6289),
    ("台中市大安區","大安區","臺中市",24.3791,120.6050),
    # ── 彰化縣 (26) ──
    ("彰化縣彰化市","彰化市","彰化縣",23.9922,120.5717),
    ("彰化縣芬園鄉","芬園鄉","彰化縣",24.0069,120.6310),
    ("彰化縣花壇鄉","花壇鄉","彰化縣",24.0374,120.5620),
    ("彰化縣秀水鄉","秀水鄉","彰化縣",23.9798,120.4849),
    ("彰化縣鹿港鎮","鹿港鎮","彰化縣",24.0479,120.4365),
    ("彰化縣福興鄉","福興鄉","彰化縣",23.9933,120.4397),
    ("彰化縣線西鄉","線西鄉","彰化縣",24.0725,120.4269),
    ("彰化縣和美鎮","和美鎮","彰化縣",24.0737,120.4895),
    ("彰化縣伸港鄉","伸港鄉","彰化縣",24.1091,120.4445),
    ("彰化縣員林市","員林市","彰化縣",23.9574,120.5703),
    ("彰化縣社頭鄉","社頭鄉","彰化縣",23.9186,120.5722),
    ("彰化縣永靖鄉","永靖鄉","彰化縣",23.9286,120.5312),
    ("彰化縣埔心鄉","埔心鄉","彰化縣",23.9516,120.5329),
    ("彰化縣溪湖鎮","溪湖鎮","彰化縣",23.9562,120.4850),
    ("彰化縣大村鄉","大村鄉","彰化縣",23.9709,120.5398),
    ("彰化縣埔鹽鄉","埔鹽鄉","彰化縣",23.9962,120.5056),
    ("彰化縣田中鎮","田中鎮","彰化縣",23.8670,120.5670),
    ("彰化縣北斗鎮","北斗鎮","彰化縣",23.8742,120.5323),
    ("彰化縣田尾鄉","田尾鄉","彰化縣",23.8946,120.5288),
    ("彰化縣埤頭鄉","埤頭鄉","彰化縣",23.8731,120.4948),
    ("彰化縣溪州鄉","溪州鄉","彰化縣",23.8488,120.5190),
    ("彰化縣竹塘鄉","竹塘鄉","彰化縣",23.8684,120.4592),
    ("彰化縣二林鎮","二林鎮","彰化縣",23.9139,120.3930),
    ("彰化縣大城鄉","大城鄉","彰化縣",23.8669,120.3469),
    ("彰化縣芳苑鄉","芳苑鄉","彰化縣",23.9551,120.3442),
    ("彰化縣二水鄉","二水鄉","彰化縣",23.8196,120.6097),
    # ── 南投縣 (13) ──
    ("南投縣南投市","南投市","南投縣",23.9139,120.6836),
    ("南投縣中寮鄉","中寮鄉","南投縣",23.8389,120.7263),
    ("南投縣草屯鎮","草屯鎮","南投縣",23.9736,120.6788),
    ("南投縣國姓鄉","國姓鄉","南投縣",24.0564,120.8556),
    ("南投縣埔里鎮","埔里鎮","南投縣",23.9637,120.9766),
    ("南投縣仁愛鄉","仁愛鄉","南投縣",24.0475,121.1457),
    ("南投縣名間鄉","名間鄉","南投縣",23.8657,120.6919),
    ("南投縣集集鎮","集集鎮","南投縣",23.8315,120.7970),
    ("南投縣水里鄉","水里鄉","南投縣",23.8135,120.8579),
    ("南投縣魚池鄉","魚池鄉","南投縣",23.9157,120.9274),
    ("南投縣信義鄉","信義鄉","南投縣",23.6946,120.8693),
    ("南投縣竹山鎮","竹山鎮","南投縣",23.7568,120.6660),
    ("南投縣鹿谷鄉","鹿谷鄉","南投縣",23.8166,120.7601),
    # ── 雲林縣 (20) ──
    ("雲林縣斗南鎮","斗南鎮","雲林縣",23.6797,120.4794),
    ("雲林縣大埤鄉","大埤鄉","雲林縣",23.6550,120.4312),
    ("雲林縣虎尾鎮","虎尾鎮","雲林縣",23.7085,120.4408),
    ("雲林縣土庫鎮","土庫鎮","雲林縣",23.6652,120.3933),
    ("雲林縣褒忠鄉","褒忠鄉","雲林縣",23.6941,120.3435),
    ("雲林縣東勢鄉","東勢鄉","雲林縣",23.6879,120.3132),
    ("雲林縣台西鄉","台西鄉","雲林縣",23.7019,120.2092),
    ("雲林縣崙背鄉","崙背鄉","雲林縣",23.7495,120.3316),
    ("雲林縣麥寮鄉","麥寮鄉","雲林縣",23.7697,120.2387),
    ("雲林縣斗六市","斗六市","雲林縣",23.7074,120.5427),
    ("雲林縣林內鄉","林內鄉","雲林縣",23.7584,120.6111),
    ("雲林縣古坑鄉","古坑鄉","雲林縣",23.6380,120.5656),
    ("雲林縣莿桐鄉","莿桐鄉","雲林縣",23.7556,120.5163),
    ("雲林縣西螺鎮","西螺鎮","雲林縣",23.8003,120.4688),
    ("雲林縣二崙鄉","二崙鄉","雲林縣",23.7813,120.4260),
    ("雲林縣北港鎮","北港鎮","雲林縣",23.5686,120.3022),
    ("雲林縣水林鄉","水林鄉","雲林縣",23.5670,120.2560),
    ("雲林縣口湖鄉","口湖鄉","雲林縣",23.6054,120.1906),
    ("雲林縣四湖鄉","四湖鄉","雲林縣",23.6431,120.2377),
    ("雲林縣元長鄉","元長鄉","雲林縣",23.6291,120.3213),
    # ── 嘉義市 (2) ──
    ("嘉義市東區","東區","嘉義市",23.4798,120.4491),
    ("嘉義市西區","西區","嘉義市",23.4844,120.4288),
    # ── 嘉義縣 (18) ──
    ("嘉義縣番路鄉","番路鄉","嘉義縣",23.4956,120.5311),
    ("嘉義縣梅山鄉","梅山鄉","嘉義縣",23.5758,120.5598),
    ("嘉義縣竹崎鄉","竹崎鄉","嘉義縣",23.5189,120.5475),
    ("嘉義縣阿里山鄉","阿里山鄉","嘉義縣",23.5167,120.7200),
    ("嘉義縣中埔鄉","中埔鄉","嘉義縣",23.4133,120.4970),
    ("嘉義縣大埔鄉","大埔鄉","嘉義縣",23.2975,120.5781),
    ("嘉義縣水上鄉","水上鄉","嘉義縣",23.4278,120.4086),
    ("嘉義縣鹿草鄉","鹿草鄉","嘉義縣",23.3884,120.3530),
    ("嘉義縣太保市","太保市","嘉義縣",23.4922,120.3236),
    ("嘉義縣朴子市","朴子市","嘉義縣",23.4650,120.2490),
    ("嘉義縣東石鄉","東石鄉","嘉義縣",23.4552,120.1679),
    ("嘉義縣六腳鄉","六腳鄉","嘉義縣",23.4547,120.2260),
    ("嘉義縣新港鄉","新港鄉","嘉義縣",23.5520,120.3430),
    ("嘉義縣民雄鄉","民雄鄉","嘉義縣",23.5561,120.4281),
    ("嘉義縣大林鎮","大林鎮","嘉義縣",23.5969,120.4706),
    ("嘉義縣溪口鄉","溪口鄉","嘉義縣",23.5826,120.3863),
    ("嘉義縣義竹鄉","義竹鄉","嘉義縣",23.3682,120.2814),
    ("嘉義縣布袋鎮","布袋鎮","嘉義縣",23.3755,120.1689),
    # ── 臺南市 (37) ──
    ("台南市中西區","中西區","臺南市",22.9947,120.1997),
    ("台南市東區","東區","臺南市",23.0046,120.2225),
    ("台南市南區","南區","臺南市",22.9762,120.1978),
    ("台南市北區","北區","臺南市",23.0154,120.2067),
    ("台南市安平區","安平區","臺南市",22.9973,120.1692),
    ("台南市安南區","安南區","臺南市",23.0472,120.1688),
    ("台南市永康區","永康區","臺南市",23.0346,120.2558),
    ("台南市歸仁區","歸仁區","臺南市",22.9524,120.2456),
    ("台南市新化區","新化區","臺南市",23.0381,120.3104),
    ("台南市左鎮區","左鎮區","臺南市",23.0151,120.3706),
    ("台南市玉井區","玉井區","臺南市",23.1212,120.4539),
    ("台南市楠西區","楠西區","臺南市",23.1695,120.5003),
    ("台南市南化區","南化區","臺南市",23.1413,120.4787),
    ("台南市仁德區","仁德區","臺南市",22.9368,120.2418),
    ("台南市關廟區","關廟區","臺南市",22.9073,120.3014),
    ("台南市龍崎區","龍崎區","臺南市",22.9283,120.3679),
    ("台南市官田區","官田區","臺南市",23.0760,120.3145),
    ("台南市麻豆區","麻豆區","臺南市",23.1800,120.2519),
    ("台南市佳里區","佳里區","臺南市",23.1770,120.1795),
    ("台南市西港區","西港區","臺南市",23.1422,120.2219),
    ("台南市七股區","七股區","臺南市",23.1474,120.1048),
    ("台南市將軍區","將軍區","臺南市",23.1986,120.1345),
    ("台南市學甲區","學甲區","臺南市",23.2280,120.1885),
    ("台南市北門區","北門區","臺南市",23.2657,120.1201),
    ("台南市新營區","新營區","臺南市",23.3116,120.3152),
    ("台南市後壁區","後壁區","臺南市",23.3720,120.3454),
    ("台南市白河區","白河區","臺南市",23.3533,120.4189),
    ("台南市東山區","東山區","臺南市",23.2575,120.4487),
    ("台南市六甲區","六甲區","臺南市",23.2184,120.3568),
    ("台南市下營區","下營區","臺南市",23.2367,120.2620),
    ("台南市柳營區","柳營區","臺南市",23.2878,120.3009),
    ("台南市鹽水區","鹽水區","臺南市",23.3093,120.2596),
    ("台南市善化區","善化區","臺南市",23.1368,120.2882),
    ("台南市大內區","大內區","臺南市",23.0951,120.3678),
    ("台南市山上區","山上區","臺南市",23.0669,120.3249),
    ("台南市新市區","新市區","臺南市",23.0696,120.2740),
    ("台南市安定區","安定區","臺南市",23.0896,120.2327),
    # ── 高雄市 (38) ──
    ("高雄市楠梓區","楠梓區","高雄市",22.7372,120.3203),
    ("高雄市左營區","左營區","高雄市",22.6848,120.2956),
    ("高雄市鼓山區","鼓山區","高雄市",22.6620,120.2789),
    ("高雄市三民區","三民區","高雄市",22.6430,120.3100),
    ("高雄市新興區","新興區","高雄市",22.6312,120.3037),
    ("高雄市前金區","前金區","高雄市",22.6322,120.2997),
    ("高雄市鹽埕區","鹽埕區","高雄市",22.6273,120.2849),
    ("高雄市苓雅區","苓雅區","高雄市",22.6208,120.3178),
    ("高雄市前鎮區","前鎮區","高雄市",22.5996,120.3275),
    ("高雄市旗津區","旗津區","高雄市",22.5785,120.2838),
    ("高雄市小港區","小港區","高雄市",22.5701,120.3497),
    ("高雄市鳳山區","鳳山區","高雄市",22.6263,120.3577),
    ("高雄市林園區","林園區","高雄市",22.4990,120.3974),
    ("高雄市大寮區","大寮區","高雄市",22.5893,120.4026),
    ("高雄市大樹區","大樹區","高雄市",22.7109,120.4370),
    ("高雄市大社區","大社區","高雄市",22.7339,120.3502),
    ("高雄市仁武區","仁武區","高雄市",22.6984,120.3474),
    ("高雄市鳥松區","鳥松區","高雄市",22.6431,120.3802),
    ("高雄市岡山區","岡山區","高雄市",22.7965,120.3002),
    ("高雄市橋頭區","橋頭區","高雄市",22.7503,120.3069),
    ("高雄市燕巢區","燕巢區","高雄市",22.7874,120.3754),
    ("高雄市田寮區","田寮區","高雄市",22.8715,120.3813),
    ("高雄市阿蓮區","阿蓮區","高雄市",22.8815,120.3280),
    ("高雄市路竹區","路竹區","高雄市",22.8576,120.2605),
    ("高雄市湖內區","湖內區","高雄市",22.8986,120.2235),
    ("高雄市茄萣區","茄萣區","高雄市",22.8997,120.1911),
    ("高雄市永安區","永安區","高雄市",22.8340,120.2310),
    ("高雄市彌陀區","彌陀區","高雄市",22.7882,120.2475),
    ("高雄市梓官區","梓官區","高雄市",22.7555,120.2616),
    ("高雄市旗山區","旗山區","高雄市",22.8882,120.4818),
    ("高雄市美濃區","美濃區","高雄市",22.8961,120.5434),
    ("高雄市六龜區","六龜區","高雄市",23.0007,120.6261),
    ("高雄市甲仙區","甲仙區","高雄市",23.0845,120.5887),
    ("高雄市杉林區","杉林區","高雄市",23.0521,120.5404),
    ("高雄市內門區","內門區","高雄市",22.9650,120.4716),
    ("高雄市茂林區","茂林區","高雄市",22.9076,120.6617),
    ("高雄市桃源區","桃源區","高雄市",23.1666,120.7369),
    ("高雄市那瑪夏區","那瑪夏區","高雄市",23.2358,120.6700),
    # ── 屏東縣 (33) ──
    ("屏東縣屏東市","屏東市","屏東縣",22.6669,120.4905),
    ("屏東縣三地門鄉","三地門鄉","屏東縣",22.7490,120.6357),
    ("屏東縣霧台鄉","霧台鄉","屏東縣",22.7064,120.6975),
    ("屏東縣瑪家鄉","瑪家鄉","屏東縣",22.7117,120.6097),
    ("屏東縣九如鄉","九如鄉","屏東縣",22.7329,120.5127),
    ("屏東縣里港鄉","里港鄉","屏東縣",22.7808,120.5039),
    ("屏東縣高樹鄉","高樹鄉","屏東縣",22.8275,120.5780),
    ("屏東縣鹽埔鄉","鹽埔鄉","屏東縣",22.7599,120.5428),
    ("屏東縣長治鄉","長治鄉","屏東縣",22.6892,120.5248),
    ("屏東縣麟洛鄉","麟洛鄉","屏東縣",22.6707,120.5420),
    ("屏東縣竹田鄉","竹田鄉","屏東縣",22.5984,120.5380),
    ("屏東縣內埔鄉","內埔鄉","屏東縣",22.6124,120.5701),
    ("屏東縣萬丹鄉","萬丹鄉","屏東縣",22.5843,120.4800),
    ("屏東縣潮州鎮","潮州鎮","屏東縣",22.5485,120.5462),
    ("屏東縣泰武鄉","泰武鄉","屏東縣",22.5947,120.6448),
    ("屏東縣來義鄉","來義鄉","屏東縣",22.5125,120.6340),
    ("屏東縣萬巒鄉","萬巒鄉","屏東縣",22.6004,120.5998),
    ("屏東縣崁頂鄉","崁頂鄉","屏東縣",22.5421,120.4909),
    ("屏東縣新埤鄉","新埤鄉","屏東縣",22.5368,120.5756),
    ("屏東縣南州鄉","南州鄉","屏東縣",22.4858,120.5128),
    ("屏東縣林邊鄉","林邊鄉","屏東縣",22.4390,120.5152),
    ("屏東縣東港鎮","東港鎮","屏東縣",22.4678,120.4610),
    ("屏東縣琉球鄉","琉球鄉","屏東縣",22.3434,120.3777),
    ("屏東縣佳冬鄉","佳冬鄉","屏東縣",22.4178,120.5463),
    ("屏東縣新園鄉","新園鄉","屏東縣",22.5350,120.4317),
    ("屏東縣枋寮鄉","枋寮鄉","屏東縣",22.3739,120.5878),
    ("屏東縣枋山鄉","枋山鄉","屏東縣",22.2699,120.6360),
    ("屏東縣春日鄉","春日鄉","屏東縣",22.4071,120.6576),
    ("屏東縣獅子鄉","獅子鄉","屏東縣",22.3430,120.6817),
    ("屏東縣車城鄉","車城鄉","屏東縣",22.0824,120.7158),
    ("屏東縣牡丹鄉","牡丹鄉","屏東縣",22.1559,120.7556),
    ("屏東縣恆春鎮","恆春鎮","屏東縣",22.0025,120.7430),
    ("屏東縣滿州鄉","滿州鄉","屏東縣",22.0565,120.8382),
    # ── 宜蘭縣 (12) ──
    ("宜蘭縣宜蘭市","宜蘭市","宜蘭縣",24.7567,121.7544),
    ("宜蘭縣頭城鎮","頭城鎮","宜蘭縣",24.8656,121.8214),
    ("宜蘭縣礁溪鄉","礁溪鄉","宜蘭縣",24.8226,121.7712),
    ("宜蘭縣壯圍鄉","壯圍鄉","宜蘭縣",24.7690,121.8248),
    ("宜蘭縣員山鄉","員山鄉","宜蘭縣",24.7436,121.6779),
    ("宜蘭縣羅東鎮","羅東鎮","宜蘭縣",24.6775,121.7676),
    ("宜蘭縣三星鄉","三星鄉","宜蘭縣",24.6587,121.6559),
    ("宜蘭縣大同鄉","大同鄉","宜蘭縣",24.5872,121.5645),
    ("宜蘭縣五結鄉","五結鄉","宜蘭縣",24.6658,121.7955),
    ("宜蘭縣冬山鄉","冬山鄉","宜蘭縣",24.6531,121.8004),
    ("宜蘭縣蘇澳鎮","蘇澳鎮","宜蘭縣",24.5989,121.8478),
    ("宜蘭縣南澳鄉","南澳鄉","宜蘭縣",24.4551,121.8010),
    # ── 花蓮縣 (13) ──
    ("花蓮縣花蓮市","花蓮市","花蓮縣",23.9919,121.6013),
    ("花蓮縣新城鄉","新城鄉","花蓮縣",24.1268,121.6395),
    ("花蓮縣秀林鄉","秀林鄉","花蓮縣",24.1541,121.5694),
    ("花蓮縣吉安鄉","吉安鄉","花蓮縣",23.9700,121.5726),
    ("花蓮縣壽豐鄉","壽豐鄉","花蓮縣",23.8562,121.5234),
    ("花蓮縣鳳林鎮","鳳林鎮","花蓮縣",23.7460,121.4752),
    ("花蓮縣光復鄉","光復鄉","花蓮縣",23.6594,121.4370),
    ("花蓮縣豐濱鄉","豐濱鄉","花蓮縣",23.6060,121.5267),
    ("花蓮縣瑞穗鄉","瑞穗鄉","花蓮縣",23.4977,121.4149),
    ("花蓮縣萬榮鄉","萬榮鄉","花蓮縣",23.6481,121.3805),
    ("花蓮縣玉里鎮","玉里鎮","花蓮縣",23.3362,121.3023),
    ("花蓮縣卓溪鄉","卓溪鄉","花蓮縣",23.3831,121.2494),
    ("花蓮縣富里鄉","富里鄉","花蓮縣",23.1955,121.2563),
    # ── 臺東縣 (16) ──
    ("台東縣台東市","臺東市","臺東縣",22.7583,121.1444),
    ("台東縣綠島鄉","綠島鄉","臺東縣",22.6707,121.4974),
    ("台東縣蘭嶼鄉","蘭嶼鄉","臺東縣",22.0416,121.5500),
    ("台東縣延平鄉","延平鄉","臺東縣",22.9173,121.0710),
    ("台東縣卑南鄉","卑南鄉","臺東縣",22.7195,121.0572),
    ("台東縣鹿野鄉","鹿野鄉","臺東縣",22.9134,121.1634),
    ("台東縣關山鎮","關山鎮","臺東縣",23.0530,121.1669),
    ("台東縣海端鄉","海端鄉","臺東縣",23.1310,121.0413),
    ("台東縣池上鄉","池上鄉","臺東縣",23.1133,121.2247),
    ("台東縣東河鄉","東河鄉","臺東縣",22.9175,121.3047),
    ("台東縣成功鎮","成功鎮","臺東縣",23.0993,121.3702),
    ("台東縣長濱鄉","長濱鄉","臺東縣",23.3305,121.4437),
    ("台東縣太麻里鄉","太麻里鄉","臺東縣",22.6155,121.0360),
    ("台東縣金峰鄉","金峰鄉","臺東縣",22.5694,120.9615),
    ("台東縣大武鄉","大武鄉","臺東縣",22.3470,120.9029),
    ("台東縣達仁鄉","達仁鄉","臺東縣",22.4225,120.8822),
    # ── 澎湖縣 (6) ──
    ("澎湖縣馬公市","馬公市","澎湖縣",23.5708,119.5691),
    ("澎湖縣西嶼鄉","西嶼鄉","澎湖縣",23.6022,119.4940),
    ("澎湖縣望安鄉","望安鄉","澎湖縣",23.3706,119.5023),
    ("澎湖縣七美鄉","七美鄉","澎湖縣",23.2083,119.4347),
    ("澎湖縣白沙鄉","白沙鄉","澎湖縣",23.6715,119.6115),
    ("澎湖縣湖西鄉","湖西鄉","澎湖縣",23.5744,119.6464),
    # ── 金門縣 (6) ──
    ("金門縣金城鎮","金城鎮","金門縣",24.4361,118.3169),
    ("金門縣金湖鎮","金湖鎮","金門縣",24.4534,118.3986),
    ("金門縣金沙鎮","金沙鎮","金門縣",24.4955,118.4393),
    ("金門縣金寧鄉","金寧鄉","金門縣",24.4695,118.3049),
    ("金門縣烈嶼鄉","烈嶼鄉","金門縣",24.4258,118.2343),
    ("金門縣烏坵鄉","烏坵鄉","金門縣",25.0059,119.4649),
    # ── 連江縣 (4) ──
    ("連江縣南竿鄉","南竿鄉","連江縣",26.1583,119.9383),
    ("連江縣北竿鄉","北竿鄉","連江縣",26.2267,120.0033),
    ("連江縣莒光鄉","莒光鄉","連江縣",25.9831,119.9258),
    ("連江縣東引鄉","東引鄉","連江縣",26.3714,120.4949),
]

# 前端可選的預設站點
_PRESET_LOCATIONS = [
    {"label":"桃園國際機場",  "name":"大園區",  "county":"桃園市","lat":25.0780,"lon":121.2320},
    {"label":"台北松山機場",  "name":"松山區",  "county":"臺北市","lat":25.0632,"lon":121.5504},
    {"label":"高雄國際機場",  "name":"小港區",  "county":"高雄市","lat":22.5701,"lon":120.3497},
    {"label":"高鐵南港站",    "name":"南港區",  "county":"臺北市","lat":25.0539,"lon":121.6068},
    {"label":"高鐵台北站",    "name":"中正區",  "county":"臺北市","lat":25.0478,"lon":121.5171},
    {"label":"高鐵板橋站",    "name":"板橋區",  "county":"新北市","lat":25.0118,"lon":121.4648},
    {"label":"高鐵桃園站",    "name":"中壢區",  "county":"桃園市","lat":24.9700,"lon":121.3108},
    {"label":"高鐵新竹站",    "name":"竹北市",  "county":"新竹縣","lat":24.8232,"lon":121.0043},
    {"label":"高鐵苗栗站",    "name":"後龍鎮",  "county":"苗栗縣","lat":24.5962,"lon":120.8014},
    {"label":"高鐵台中站",    "name":"烏日區",  "county":"臺中市","lat":24.1272,"lon":120.6847},
    {"label":"高鐵彰化站",    "name":"彰化市",  "county":"彰化縣","lat":23.9922,"lon":120.5717},
    {"label":"高鐵雲林站",    "name":"虎尾鎮",  "county":"雲林縣","lat":23.7085,"lon":120.4408},
    {"label":"高鐵嘉義站",    "name":"太保市",  "county":"嘉義縣","lat":23.4922,"lon":120.3236},
    {"label":"高鐵台南站",    "name":"歸仁區",  "county":"臺南市","lat":22.9524,"lon":120.2456},
    {"label":"高鐵左營站",    "name":"左營區",  "county":"高雄市","lat":22.6848,"lon":120.2956},
]

def _haversine(la1, lo1, la2, lo2):
    R = 6371.0
    d = lambda a, b: math.radians(b - a)
    dlat, dlon = d(la1, la2), d(lo1, lo2)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(la1))*math.cos(math.radians(la2))*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _nearest_township(lat, lon):
    return min(_TOWNSHIPS, key=lambda t: _haversine(lat, lon, t[3], t[4]))

def _wx_emoji(code):
    try:
        return _WX_EMOJI.get(int(code), "🌡️")
    except Exception:
        return "🌡️"

def _get_el(elements, name):
    """從 WeatherElement 列表取出指定名稱的 Time slots。"""
    for el in elements:
        if (el.get("ElementName") or el.get("elementName") or "") == name:
            return el.get("Time") or el.get("time") or []
    return []

def _ev_get(slot, *keys):
    """從 ElementValue[0] 取出第一個有值的 key。"""
    ev = slot.get("ElementValue") or slot.get("elementValue") or []
    if ev:
        for k in keys:
            v = ev[0].get(k)
            if v is not None:
                return str(v).strip()
    return ""

def _slot_start(slot):
    return slot.get("StartTime") or slot.get("startTime") or slot.get("DataTime") or slot.get("dataTime") or ""

def _fetch_cwa(township_info):
    """回傳解析好的天氣 dict；任何錯誤都不拋出。"""
    display, api_name, county, lat, lon = township_info
    key = api_name
    with _WEATHER_CACHE_LOCK:
        entry = _WEATHER_CACHE.get(key)
        if entry and time.time() - entry[0] < _WEATHER_CACHE_TTL:
            return entry[1]

    api_key  = os.environ.get("CWA_API_KEY", "")
    endpoint = _COUNTY_ENDPOINT.get(county, "F-D0047-089")
    result   = {
        "location": api_name, "display": display, "county": county,
        "lat": lat, "lon": lon,
        "current": {}, "forecast": [], "hourly": [], "alerts": [], "has_alert": False,
        "error": None,
    }

    if not api_key:
        result["error"] = "尚未設定 CWA_API_KEY，請至 CWA 開放資料平臺申請後填入環境變數"
        with _WEATHER_CACHE_LOCK:
            _WEATHER_CACHE[key] = (time.time(), result)
        return result

    # ── 取得鄉鎮預報 ──
    try:
        r = _requests.get(
            f"{_CWA_BASE}/{endpoint}",
            params={"Authorization": api_key},
            timeout=12,
        )
        if not r.ok:
            raise ValueError(f"CWA 回傳 HTTP {r.status_code}：{r.text[:80]}")
        raw = r.json()
        records = raw.get("records", {})
        # 找出 location 列表（兩種可能的 key）
        locs_list = (
            records.get("locations") or records.get("Locations") or
            records.get("Location")  or []
        )
        if isinstance(locs_list, dict):
            locs_list = [locs_list]
        location_item = None
        for locs in locs_list:
            inner = locs.get("location") or locs.get("Location") or []
            for loc in inner:
                if (loc.get("locationName") or loc.get("LocationName") or "") == api_name:
                    location_item = loc
                    break
            if location_item:
                break

        if location_item:
            els = location_item.get("WeatherElement") or location_item.get("weatherElement") or []

            wx_slots   = _get_el(els, "天氣現象")
            t_hi_slots = _get_el(els, "最高溫度")
            t_lo_slots = _get_el(els, "最低溫度")
            t_av_slots = _get_el(els, "平均溫度")
            at_slots   = _get_el(els, "最高體感溫度")
            pop_slots  = _get_el(els, "12小時降雨機率")
            ws_slots   = _get_el(els, "風速")
            rh_slots   = _get_el(els, "平均相對濕度")
            desc_slots = _get_el(els, "天氣預報綜合描述")

            wx_code_s = _ev_get(wx_slots[0],  "WeatherCode")    if wx_slots   else ""
            wx_text   = _ev_get(wx_slots[0],  "Weather")        if wx_slots   else "—"
            wx_code   = int(wx_code_s) if wx_code_s else 0
            temp      = (_ev_get(t_av_slots[0], "Temperature")  if t_av_slots else "") or \
                        (_ev_get(t_hi_slots[0], "MaxTemperature") if t_hi_slots else "") or "—"
            feels     = _ev_get(at_slots[0],  "MaxApparentTemperature") if at_slots  else "—"
            pop       = _ev_get(pop_slots[0], "ProbabilityOfPrecipitation") if pop_slots else "—"
            wind      = _ev_get(ws_slots[0],  "WindSpeed")                         if ws_slots   else "—"
            rh        = _ev_get(rh_slots[0],  "RelativeHumidity", "RH", "Value") if rh_slots   else "—"
            desc      = _ev_get(desc_slots[0],"WeatherDescription")               if desc_slots else ""
            updated   = _slot_start(wx_slots[0]) if wx_slots else ""

            result["current"] = {
                "wx":       wx_text,
                "wx_code":  wx_code,
                "emoji":    _wx_emoji(wx_code),
                "temp":     temp,
                "feels":    feels,
                "pop12h":   pop,
                "wind":     wind,
                "humidity": rh,
                "desc":     desc[:130] if desc else "",
                "updated":  updated[:19].replace("T", " ") if updated else "",
                "alert":    wx_code in _ALERT_WX_CODES,
            }

            # ── 7 天預報（只取今天起往後） ──
            today_str = _dt.now(_tz(_td(hours=8))).strftime("%Y-%m-%d")
            days_seen: list[str] = []
            forecast_map: dict   = {}
            for sl in wx_slots:
                day = _slot_start(sl)[:10]
                if not day or day in forecast_map or day < today_str:
                    continue
                wc = _ev_get(sl, "WeatherCode")
                forecast_map[day] = {
                    "date":    day,
                    "wx":      _ev_get(sl, "Weather"),
                    "wx_code": int(wc) if wc else 0,
                    "emoji":   _wx_emoji(wc),
                    "pop": "", "hi": "—", "lo": "—",
                }
                days_seen.append(day)
                if len(days_seen) >= 7:
                    break

            for sl in pop_slots:
                day = _slot_start(sl)[:10]
                if day in forecast_map and not forecast_map[day]["pop"]:
                    forecast_map[day]["pop"] = _ev_get(sl, "ProbabilityOfPrecipitation")

            for sl in t_hi_slots:
                day = _slot_start(sl)[:10]
                if day not in forecast_map:
                    continue
                v = _ev_get(sl, "MaxTemperature")
                if not v:
                    continue
                cur = forecast_map[day]["hi"]
                try:
                    if cur == "—" or float(v) > float(cur):
                        forecast_map[day]["hi"] = v
                except Exception:
                    forecast_map[day]["hi"] = v

            for sl in t_lo_slots:
                day = _slot_start(sl)[:10]
                if day not in forecast_map:
                    continue
                v = _ev_get(sl, "MinTemperature")
                if not v:
                    continue
                cur = forecast_map[day]["lo"]
                try:
                    if cur == "—" or float(v) < float(cur):
                        forecast_map[day]["lo"] = v
                except Exception:
                    forecast_map[day]["lo"] = v

            result["forecast"] = [forecast_map[d] for d in days_seen]

    except Exception as e:
        result["error"] = f"預報取得失敗：{str(e)[:120]}"

    # ── 逐時預報（odd endpoint，3 小時間距）──
    try:
        ep_num = int(endpoint[-3:]) - 2
        odd_ep = f"F-D0047-{ep_num:03d}"
        ro = _requests.get(
            f"{_CWA_BASE}/{odd_ep}",
            params={"Authorization": api_key},
            timeout=12,
        )
        if ro.ok:
            raw_o = ro.json()
            rec_o = raw_o.get("records", {})
            ll_o  = (rec_o.get("locations") or rec_o.get("Locations") or
                     rec_o.get("Location") or [])
            if isinstance(ll_o, dict):
                ll_o = [ll_o]
            loc_o = None
            for lc in ll_o:
                inner = lc.get("location") or lc.get("Location") or []
                for lci in inner:
                    if (lci.get("locationName") or lci.get("LocationName") or "") == api_name:
                        loc_o = lci
                        break
                if loc_o:
                    break
            if loc_o:
                els_o  = loc_o.get("WeatherElement") or loc_o.get("weatherElement") or []
                h_wx   = _get_el(els_o, "天氣現象")
                h_t    = _get_el(els_o, "溫度")
                h_pop  = _get_el(els_o, "3小時降雨機率")
                t_map   = {_slot_start(s): _ev_get(s, "Temperature") for s in h_t}
                pop_map = {_slot_start(s): _ev_get(s, "ProbabilityOfPrecipitation") for s in h_pop}
                now_ts = _dt.now(_tz(_td(hours=8))).strftime("%Y-%m-%dT%H")
                hourly = []
                for sl in h_wx:
                    ts = _slot_start(sl)
                    if not ts or ts[:13] < now_ts:
                        continue
                    wc = _ev_get(sl, "WeatherCode")
                    hourly.append({
                        "time":    ts[:16],
                        "emoji":   _wx_emoji(wc),
                        "wx_code": int(wc) if wc else 0,
                        "temp":    t_map.get(ts, "—") or "—",
                        "pop":     pop_map.get(ts, "") or "",
                    })
                    if len(hourly) >= 16:
                        break
                result["hourly"] = hourly
    except Exception:
        pass

    # ── 取得特報 ──
    try:
        r2 = _requests.get(
            f"{_CWA_BASE}/W-C0033-001",
            params={"Authorization": api_key, "locationName": county},
            timeout=10,
        )
        if not r2.ok:
            raise ValueError(f"HTTP {r2.status_code}")
        raw2  = r2.json()
        locs2 = raw2.get("records", {}).get("location") or []
        alerts = []
        for loc2 in locs2:
            hazards = loc2.get("hazardConditions", {}).get("hazards") or []
            if isinstance(hazards, dict):
                hazards = [hazards]
            for h in hazards:
                info = h.get("info", {})
                phen = info.get("phenomena", "")
                sig  = info.get("significance", "")
                if phen:
                    alerts.append(f"{phen}{sig}".strip())
        result["alerts"]    = alerts
        result["has_alert"] = len(alerts) > 0
    except Exception:
        pass  # 特報取得失敗不影響主要資料

    with _WEATHER_CACHE_LOCK:
        _WEATHER_CACHE[key] = (time.time(), result)
    return result


@app.route("/weather")
def weather_page():
    districts = [
        {"l": t[0], "n": t[1], "c": t[2], "lat": t[3], "lon": t[4]}
        for t in _TOWNSHIPS
    ]
    return render_template("weather.html", districts=districts)

# ──────────────────────────────────────────────
#  METAR（RCTP 桃園機場）
# ──────────────────────────────────────────────
_METAR_CACHE: dict = {"data": None, "expires_at": 0.0}
_METAR_LOCK  = threading.Lock()

def _parse_metar(raw: str) -> dict:
    out: dict = {}
    try:
        m = _re.search(r'\b\d{2}(\d{2})(\d{2})Z\b', raw)
        if m: out["time_utc"] = f"{m.group(1)}:{m.group(2)}Z"

        m = _re.search(r'\b(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b', raw)
        if m:
            spd_raw = int(m.group(2)); unit = m.group(4)
            spd_ms = round(spd_raw * 0.51444) if unit == "KT" else spd_raw
            gust_ms = None
            if m.group(3):
                gust_ms = round(int(m.group(3)) * 0.51444) if unit == "KT" else int(m.group(3))
            out["wind_dir"] = m.group(1) if m.group(1) == "VRB" else int(m.group(1))
            out["wind_kt"]  = spd_raw
            out["wind_ms"]  = spd_ms
            if gust_ms: out["gust_ms"] = gust_ms

        m = _re.search(r'\b(\d{4})\b', raw)
        if m:
            v = int(m.group(1))
            out["vis_m"]   = v
            out["vis_str"] = ">10km" if v >= 9999 else f"{v}m"

        m = _re.search(r'\b(M?)(\d{2})/(M?)(\d{2})\b', raw)
        if m:
            out["temp"] = int(m.group(2)) * (-1 if m.group(1) else 1)
            out["dew"]  = int(m.group(4)) * (-1 if m.group(3) else 1)

        m = _re.search(r'\bQ(\d{4})\b', raw)
        if m: out["qnh"] = int(m.group(1))

        sky = _re.findall(r'\b(FEW|SCT|BKN|OVC|SKC|CLR|CAVOK)(\d{3})?\b', raw)
        if sky: out["sky"] = " ".join(s[0] + s[1] for s in sky)

        wx = _re.findall(r'(?<!\w)([+-]|VC)?'
                         r'(DZ|RA|SN|SG|GR|GS|PL|UP|BR|FG|FU|VA|DU|SA|HZ|TS|SH|FZ)'
                         r'[A-Z]*', raw)
        if wx: out["wx"] = " ".join("".join(w) for w in wx)

        if "NOSIG" in raw: out["trend"] = "NOSIG"
    except Exception:
        pass
    return out

@app.route("/api/metar")
def api_metar():
    with _METAR_LOCK:
        if _METAR_CACHE["data"] and time.time() < _METAR_CACHE["expires_at"]:
            return jsonify(_METAR_CACHE["data"])
    try:
        r = _requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": "RCTP", "format": "raw", "taf": "false"},
            timeout=8,
        )
        r.raise_for_status()
        raw    = r.text.strip().splitlines()[-1].strip()
        result = {"raw": raw, "ok": True, **_parse_metar(raw)}
        with _METAR_LOCK:
            _METAR_CACHE["data"]       = result
            _METAR_CACHE["expires_at"] = time.time() + 1800
        return jsonify(result)
    except Exception:
        with _METAR_LOCK:
            if _METAR_CACHE["data"]:
                return jsonify({**_METAR_CACHE["data"], "stale": True})
        return jsonify({"ok": False, "error": "METAR 取得失敗"})

@app.route("/api/weather")
def api_weather():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "需要 lat 與 lon 參數"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "座標超出範圍"}), 400

    # 允許直接以 name+county 精確查詢（前端手動選站點時使用）
    name   = request.args.get("name", "").strip()
    county = request.args.get("county", "").strip()
    if name and county:
        township = next(
            (t for t in _TOWNSHIPS if t[1] == name and t[2] == county),
            None
        )
        if not township:
            township = (name, name, county, lat, lon)
    else:
        township = _nearest_township(lat, lon)

    data = _fetch_cwa(township)
    return jsonify(data)


# ══════════════════════════════════════════════
#  AI 飲食熱量追蹤（Gemini Vision + 跨裝置同步）
# ══════════════════════════════════════════════
from datetime import date as _date_cls

# ── KV storage layer (Upstash Redis REST API) ──
# 支援 Vercel KV (KV_REST_API_*) 與 Upstash 直連 (UPSTASH_REDIS_REST_*) 兩種格式
# 若未設定則使用 in-memory fallback（重啟後資料消失，但不會 crash）
_KV_URL   = (
    os.environ.get("KV_REST_API_URL") or
    os.environ.get("UPSTASH_REDIS_REST_URL") or ""
).rstrip("/")
_KV_TOKEN = (
    os.environ.get("KV_REST_API_TOKEN") or
    os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
)
_diet_mem: dict = {}  # in-memory fallback


def _kv_get(key: str):
    if not (_KV_URL and _KV_TOKEN):
        return _diet_mem.get(key)
    try:
        r = _requests.get(
            f"{_KV_URL}/get/{key}",
            headers={"Authorization": f"Bearer {_KV_TOKEN}"},
            timeout=5,
        )
        val = r.json().get("result")
        return _json.loads(val) if val else None
    except Exception:
        return _diet_mem.get(key)


def _kv_set(key: str, value) -> None:
    serialized = _json.dumps(value, ensure_ascii=False)
    if not (_KV_URL and _KV_TOKEN):
        _diet_mem[key] = value
        return
    try:
        _requests.post(
            _KV_URL,
            headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"},
            json=["SET", key, serialized],
            timeout=5,
        )
    except Exception:
        _diet_mem[key] = value  # fallback on network error


def _diet_load(sync_id: str) -> dict:
    data = _kv_get(f"diet:bank:{sync_id}")
    return data if isinstance(data, dict) else {}


def _diet_save(sync_id: str, bank: dict) -> None:
    _kv_set(f"diet:bank:{sync_id}", bank)


def _valid_sync_id(sid: str) -> bool:
    return bool(sid) and len(sid) <= 64 and bool(_re.match(r'^[a-zA-Z0-9_\-]+$', sid))


@app.route("/diet")
def diet():
    return render_template("diet.html")


@app.route("/api/diet/bank/<sync_id>")
def diet_bank_get(sync_id):
    if not _valid_sync_id(sync_id):
        return jsonify({"error": "invalid sync_id"}), 400
    return jsonify(_diet_load(sync_id))


@app.route("/api/diet/record", methods=["POST"])
def diet_record_add():
    data        = request.get_json(force=True) or {}
    sync_id     = str(data.get("sync_id", "")).strip()
    if not _valid_sync_id(sync_id):
        return jsonify({"error": "invalid sync_id"}), 400

    date_str    = str(data.get("date", ""))
    food_name   = str(data.get("food_name", ""))[:200]
    calories    = max(0, int(data.get("calories", 0)))
    macros      = data.get("macros", {})
    carbs       = max(0, int(macros.get("carbs",   0)))
    protein     = max(0, int(macros.get("protein", 0)))
    fat         = max(0, int(macros.get("fat",     0)))
    ts          = int(data.get("ts", 0))
    daily_limit = max(800, int(data.get("daily_limit", 2500)))

    bank = _diet_load(sync_id)
    if date_str not in bank:
        bank[date_str] = {"records": [], "debt": 0}
    day = bank[date_str]

    day["records"].append({
        "food_name": food_name,
        "calories":  calories,
        "macros":    {"carbs": carbs, "protein": protein, "fat": fat},
        "ts":        ts,
    })

    total_cal = sum(r["calories"] for r in day["records"])
    debt      = day.get("debt", 0)
    effective = total_cal + debt

    if effective > daily_limit and not day.get("_cheat_spread"):
        overflow = effective - daily_limit
        per_day  = (overflow + 1) // 2
        base     = _date_cls.fromisoformat(date_str)
        for i in range(1, 3):
            fk = (base + _td(days=i)).isoformat()
            if fk not in bank:
                bank[fk] = {"records": [], "debt": 0}
            bank[fk]["debt"] = bank[fk].get("debt", 0) + per_day
        day["_cheat_spread"] = {"overflow": overflow, "perDay": per_day}

    _diet_save(sync_id, bank)
    return jsonify({"ok": True})


@app.route("/api/diet/day/<sync_id>/<date_str>", methods=["DELETE"])
def diet_day_clear(sync_id, date_str):
    if not _valid_sync_id(sync_id):
        return jsonify({"error": "invalid sync_id"}), 400
    bank = _diet_load(sync_id)
    if date_str in bank:
        bank[date_str]["records"] = []
        bank[date_str].pop("_cheat_spread", None)
    _diet_save(sync_id, bank)
    return jsonify({"ok": True})


@app.route("/api/analyze-food", methods=["POST"])
def analyze_food():
    import base64 as _b64

    user_key   = request.form.get("ai_key", "").strip()
    server_key = os.environ.get("GEMINI_API_KEY", "")
    api_key    = user_key or server_key
    key_source = "user" if user_key else "server"

    if not api_key:
        return jsonify({"error": "請在 ⚙ 設定中輸入 Gemini API 金鑰"}), 503

    image_bytes = None
    mime_type   = "image/jpeg"

    ct = request.content_type or ""
    if "multipart" in ct:
        f = request.files.get("image")
        if not f:
            return jsonify({"error": "缺少 image 欄位"}), 400
        image_bytes = f.read()
        mime_type   = f.mimetype or "image/jpeg"
    else:
        body = request.get_json(silent=True) or {}
        b64  = body.get("image", "")
        if not b64:
            return jsonify({"error": "缺少 image 欄位"}), 400
        if "," in b64:
            header, b64 = b64.split(",", 1)
            if "png"  in header: mime_type = "image/png"
            elif "webp" in header: mime_type = "image/webp"
        mime_type   = body.get("mime_type", mime_type)
        image_bytes = _b64.b64decode(b64)

    if not image_bytes:
        return jsonify({"error": "圖片內容為空"}), 400

    b64_str = _b64.b64encode(image_bytes).decode("utf-8")

    _FOOD_PROMPT = (
        "你是一位專業的運動營養師 AI。分析使用者傳來的食物照片，"
        "嚴格以 JSON 格式回傳，不得輸出任何其他內容。\n"
        '回傳格式：{"food_name":"食物完整名稱","calories":整數,'
        '"macros":{"carbs":整數,"protein":整數,"fat":整數},"comment":"一句30字以內的繁體中文正向評語"}\n'
        "calories 單位大卡，macros 單位公克。"
    )

    # 直接呼叫 Gemini REST API，不依賴 Python SDK
    # 逐一試每個模型，429/404/400 都繼續試下一個，全部失敗才回錯
    _MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-preview-05-20", "gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash-8b")
    r = None
    last_status  = None
    last_err_msg = ""

    for model in _MODELS:
        try:
            resp = _requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json={
                    "contents": [{
                        "parts": [
                            {"text": _FOOD_PROMPT},
                            {"inline_data": {"mime_type": mime_type, "data": b64_str}},
                        ]
                    }],
                    "generationConfig": {"response_mime_type": "application/json"},
                },
                timeout=30,
            )
            if resp.status_code == 200:
                r = resp
                break
            last_status  = resp.status_code
            last_err_msg = (resp.json().get("error") or {}).get("message", resp.text[:300])
            # 不管 429/404/400/其他，都繼續試下一個模型
        except Exception as e:
            last_status  = 503
            last_err_msg = str(e)

    if r is None:
        if last_status == 429:
            err_code = "user_quota_exceeded" if key_source == "user" else "quota_exceeded"
            return jsonify({"error": err_code, "detail": last_err_msg}), 429
        return jsonify({"error": f"AI 分析失敗：{last_err_msg}"}), 500

    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        result = _json.loads(text)
        macros = result.get("macros", {})
        return jsonify({
            "food_name": str(result.get("food_name", "未知食物")),
            "calories":  max(0, int(result.get("calories", 0))),
            "macros": {
                "carbs":   max(0, int(macros.get("carbs",   0))),
                "protein": max(0, int(macros.get("protein", 0))),
                "fat":     max(0, int(macros.get("fat",     0))),
            },
            "comment": str(result.get("comment", "")),
        })
    except _json.JSONDecodeError:
        return jsonify({"error": "AI 回傳格式異常，請再試一次"}), 500
    except Exception as e:
        return jsonify({"error": f"AI 分析失敗：{str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5566))
    print(f"啟動中，請開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
