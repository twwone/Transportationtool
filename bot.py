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


_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--hide-scrollbars",
    "--metrics-recording-only",
    "--mute-audio",
    "--safebrowsing-disable-auto-update",
]

# 封鎖不必要的資源類型（圖片、字體、媒體），大幅減少頁面記憶體用量
_BLOCK_TYPES = {"image", "media", "font", "stylesheet", "other"}

_SEARCH_JS = """
(params) => new Promise((resolve, reject) => {
    $.ajax({
        url: '/TimeTable/Search',
        type: 'POST',
        data: params,
        dataType: 'json'
    }).done(resolve).fail((xhr) => reject(String(xhr.status)));
})
"""


def _setup_page(browser):
    """建立並設定一個固定分頁（封鎖圖片/字體/媒體）。"""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in _BLOCK_TYPES
        else route.continue_(),
    )
    return page


def _query_with_page(page, config: dict) -> tuple[int, str | None]:
    """在已存在的分頁上執行查詢，查完導回空白頁釋放記憶體。"""
    try:
        page.goto(THSR_TIMETABLE, timeout=30000, wait_until="domcontentloaded")

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
    finally:
        # 查完導回空白頁，釋放 THSR 頁面資源但保留分頁
        try:
            page.goto("about:blank", timeout=5000, wait_until="commit")
        except Exception:
            pass

    if not result or not result.get("success"):
        return -1, f"API 回傳失敗：{result}"

    trains = (
        result.get("data", {})
              .get("DepartureTable", {})
              .get("TrainItem", [])
    )
    return len(trains), None


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

    def _launch_browser(self, p):
        return p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)

    def run(self):
        self.running = True
        self._found  = False
        attempt = 0
        with sync_playwright() as p:
            browser = self._launch_browser(p)
            page    = _setup_page(browser)
            try:
                while self.running:
                    attempt += 1
                    self._log(
                        f"第 {attempt} 次查詢 {self.origin}→{self.destination} "
                        f"{self.config['date']}..."
                    )

                    try:
                        # browser crash 時重建 browser 和 page
                        if not browser.is_connected():
                            self._log("Browser 斷線，重新啟動...")
                            try:
                                browser.close()
                            except Exception:
                                pass
                            browser = self._launch_browser(p)
                            page    = _setup_page(browser)

                        count, err = _query_with_page(page, self.config)

                    except PWTimeout:
                        err   = "查詢逾時（30 秒）"
                        count = -1
                    except Exception as e:
                        err   = str(e)[:200]
                        count = -1
                        # page 或 browser 異常：重建兩者
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser = self._launch_browser(p)
                        page    = _setup_page(browser)

                    if err:
                        self._log(f"查詢錯誤，稍後重試: {err}", "running")
                        self._interruptible_sleep(10)
                        continue

                    if count > 0:
                        self._found = True
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
                        self._log(
                            f"第 {attempt} 次：無班次，{self.interval} 秒後再試..."
                        )
                        self._interruptible_sleep(self.interval)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                self.running = False
                if not self._found:
                    self.callback("stopped", "機器人已停止")
