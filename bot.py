from __future__ import annotations
import os
import time
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

STATIONS = {
    "南港": "NanGang",
    "台北": "TaiPei",
    "板橋": "BanQiao",
    "桃園": "TaoYuan",
    "新竹": "XinZhu",
    "苗栗": "MiaoLi",
    "台中": "TaiZhong",
    "彰化": "ZhangHua",
    "雲林": "YunLin",
    "嘉義": "JiaYi",
    "台南": "TaiNan",
    "左營": "ZuoYing",
}

TIME_OPTIONS = {
    "不限":   "0000",
    "06:00": "0600", "06:30": "0630",
    "07:00": "0700", "07:30": "0730",
    "08:00": "0800", "08:30": "0830",
    "09:00": "0900", "09:30": "0930",
    "10:00": "1000", "10:30": "1030",
    "11:00": "1100", "11:30": "1130",
    "12:00": "1200", "12:30": "1230",
    "13:00": "1300", "13:30": "1330",
    "14:00": "1400", "14:30": "1430",
    "15:00": "1500", "15:30": "1530",
    "16:00": "1600", "16:30": "1630",
    "17:00": "1700", "17:30": "1730",
    "18:00": "1800", "18:30": "1830",
    "19:00": "1900", "19:30": "1930",
    "20:00": "2000", "20:30": "2030",
    "21:00": "2100", "21:30": "2130",
    "22:00": "2200", "23:00": "2300",
}

THSR_BASE         = "https://www.thsrc.com.tw"
THSR_TIMETABLE    = f"{THSR_BASE}/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c"


def _tg_notify(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass


_SEARCH_JS = """
(params) => new Promise((resolve, reject) => {
    $.ajax({
        url: '/TimeTable/Search',
        type: 'POST',
        data: params,
        dataType: 'json'
    }).done(resolve).fail((xhr) => reject(xhr.status + ' ' + xhr.statusText));
})
"""


def _check_availability(config: dict) -> tuple[int, str | None]:
    """在 Playwright Chrome 頁面的 JS 環境內呼叫高鐵 Search API，繞過 CDN WAF。"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            page = browser.new_page()

            # 攔截 /TimeTable/Search 回應（用來確認 HTTP 狀態）
            status_holder: list[int] = []

            def handle_response(resp):
                if "/TimeTable/Search" in resp.url:
                    status_holder.append(resp.status)

            page.on("response", handle_response)

            # 載入時刻表頁（讓 jQuery 和 session cookie 就位）
            page.goto(THSR_TIMETABLE, timeout=30000, wait_until="domcontentloaded")

            # 透過頁面 JS 環境執行 jQuery AJAX（同源，不受 CDN WAF 攔截）
            t = config["time_val"]
            time_str = "00:00" if t == "0000" else f"{t[:2]}:{t[2:]}"

            result = page.evaluate(_SEARCH_JS, {
                "SearchType":        "S",
                "Lang":              "TW",
                "StartStation":      config["origin_code"],
                "EndStation":        config["dest_code"],
                "OutWardSearchDate": config["date"],
                "OutWardSearchTime": time_str,
                "ReturnSearchDate":  "",
                "ReturnSearchTime":  "",
                "DiscountType":      "",
            })

            browser.close()

        if not result or not result.get("success"):
            return -1, f"API 回傳失敗：{result}"

        trains = (
            result.get("data", {})
                  .get("DepartureTable", {})
                  .get("TrainItem", [])
        )
        return len(trains), None

    except PWTimeout:
        return -1, "Playwright 逾時（頁面載入超過 30 秒）"
    except Exception as e:
        return -1, str(e)[:200]


class THSRBot:
    def __init__(self, config: dict, status_callback):
        self.config = {
            "origin_code": STATIONS[config["origin"]],
            "dest_code":   STATIONS[config["destination"]],
            "date":        config["date"],
            "time_val":    config["time"],
            "seat_type":   config["seat_type"],
            "adult":       int(config["adult"]),
        }
        self.origin      = config["origin"]
        self.destination = config["destination"]
        self.interval    = int(config.get("interval", 30))
        self.tg_token    = config.get("tg_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat_id  = config.get("tg_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.callback    = status_callback
        self.running     = False

    def stop(self):
        self.running = False

    def _interruptible_sleep(self, seconds: int):
        for _ in range(seconds):
            if not self.running:
                break
            time.sleep(1)

    def _log(self, msg: str, status: str = "running", found: bool = False):
        self.callback(status, msg, found)

    def run(self):
        self.running = True
        attempt = 0
        try:
            while self.running:
                attempt += 1
                self._log(f"第 {attempt} 次查詢 {self.origin}→{self.destination} {self.config['date']}...")

                count, err = _check_availability(self.config)

                if err:
                    self._log(f"查詢錯誤，稍後重試: {err}", "running")
                    self._interruptible_sleep(10)
                    continue

                if count > 0:
                    msg = (
                        f"高鐵放票通知\n"
                        f"{self.origin}→{self.destination}\n"
                        f"{self.config['date']}\n"
                        f"找到 {count} 個可搭班次，趕快去搶！"
                    )
                    self._log(f"找到 {count} 個可搭班次！", "found", found=True)
                    _tg_notify(self.tg_token, self.tg_chat_id, msg)
                    break
                else:
                    self._log(f"第 {attempt} 次：無班次，{self.interval} 秒後再試...")
                    self._interruptible_sleep(self.interval)
        finally:
            self.running = False
