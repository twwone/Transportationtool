import os
import time
import threading
from flask import Flask, render_template, request, jsonify
from bot import THSRBot, STATIONS, TIME_OPTIONS

app = Flask(__name__)

_bot: THSRBot | None = None
_thread: threading.Thread | None = None
_status = {
    "running": False,
    "status":  "idle",
    "message": "尚未啟動",
    "found":   False,
    "log":     [],
}


def _update(status: str, message: str, found: bool = False):
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
        has_line_token=bool(os.environ.get("LINE_NOTIFY_TOKEN")),
    )


@app.route("/api/start", methods=["POST"])
def start():
    global _bot, _thread

    if _status["running"]:
        return jsonify({"error": "機器人已在執行中"}), 400

    data = request.json or {}
    for f in ["origin", "destination", "date", "time", "seat_type", "adult"]:
        if not data.get(f):
            return jsonify({"error": f"缺少欄位: {f}"}), 400

    if data["origin"] == data["destination"]:
        return jsonify({"error": "出發站與到達站不能相同"}), 400

    _status.update({"running": True, "status": "running",
                    "message": "啟動中...", "found": False, "log": []})

    config = {
        "origin":      data["origin"],
        "destination": data["destination"],
        "date":        data["date"].replace("-", "/"),
        "time":        TIME_OPTIONS.get(data["time"], "0000"),
        "seat_type":   data["seat_type"],
        "adult":       data["adult"],
        "interval":    data.get("interval", 30),
        "line_token":  data.get("line_token", ""),
    }

    _bot = THSRBot(config, _update)
    _thread = threading.Thread(target=_bot.run, daemon=True)
    _thread.start()
    return jsonify({"success": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    if _bot:
        _bot.stop()
    _status.update({"running": False, "status": "stopped", "message": "使用者手動停止"})
    return jsonify({"success": True})


@app.route("/api/status")
def status():
    return jsonify(_status)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5566))
    print(f"啟動中，請開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
