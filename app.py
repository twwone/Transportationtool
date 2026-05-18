from __future__ import annotations
import os
import time
import threading
from flask import Flask, render_template, request, jsonify, redirect as flask_redirect
from bot import THSRBot, STATIONS, TIME_OPTIONS, DISCOUNT_OPTIONS

app = Flask(__name__)


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


@app.route("/")
def index():
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
        # bot crash 時 thread 已死但旗標可能卡在 True，需允許重新啟動
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
    # 等舊 thread 真正結束，避免重啟時新舊 bot 同時寫 _status
    if _thread is not None:
        _thread.join(timeout=3)
    return jsonify({"success": True})


@app.route("/api/status")
def status():
    return jsonify(_status)


@app.route("/api/go")
def booking_go():
    """即時生成 cipher 並直接 redirect 到高鐵時刻表預填頁（點擊後自動觸發查詢）。"""
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


@app.route("/api/test_encrypt")
def test_encrypt():
    from bot import _get_search_url, STATIONS
    config = {
        "origin_code": STATIONS.get(request.args.get("origin", "台北"), "TaiPei"),
        "dest_code":   STATIONS.get(request.args.get("dest",   "左營"), "ZuoYing"),
        "date":        request.args.get("date", "2026/06/20"),
        "time_from":   request.args.get("time", "1000"),
        "discount":    "",
    }
    url, err = _get_search_url(config)
    return jsonify({"url": url, "error": err})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5566))
    print(f"啟動中，請開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
