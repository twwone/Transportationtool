from __future__ import annotations
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
import requests as _requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

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
    now = time.time()
    with _tdx_lock:
        if _tdx_cache["token"] and now < _tdx_cache["expires_at"] - 60:
            return _tdx_cache["token"]
    # 鎖釋放後再發 HTTP，避免 token 刷新期間阻塞所有 API 呼叫
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
        with _tdx_lock:
            _tdx_cache["token"]      = token
            _tdx_cache["expires_at"] = now + data.get("expires_in", 300)
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
        _meta_cache["expires_at"] = time.time() + _META_TTL
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
    today = _dt.now(_tz(_td(hours=8))).strftime("%Y-%m-%d")

    def _get(direction: str, time_field: str) -> tuple:
        try:
            r = _requests.get(
                f"{_TDX_FIDS}/{direction}/TPE",
                headers=headers,
                params={"$format": "JSON", "$top": 1000},
                timeout=10,
            )
            r.raise_for_status()
            body = r.json()
            data = body if isinstance(body, list) else body.get("data", [])
            filtered = [f for f in data if f.get("FlightDate", "") == today]
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


@app.route("/share")
def share():
    return render_template("share.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5566))
    print(f"啟動中，請開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
