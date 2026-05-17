from __future__ import annotations
import os
import time
import requests

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

THSR_BASE     = "https://www.thsrc.com.tw"
THSR_SEARCH   = f"{THSR_BASE}/TimeTable/Search"
THSR_TIMETABLE_PAGE = f"{THSR_BASE}/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


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


def _check_availability(session: requests.Session, config: dict) -> tuple[int, str | None]:
    try:
        # 造訪時刻表頁面取得 session cookie
        session.get(THSR_TIMETABLE_PAGE, timeout=15)

        # POST 新版 JSON 查詢 API
        form_data = {
            "SearchType":        "S",
            "Lang":              "TW",
            "StartStation":      config["origin_code"],
            "EndStation":        config["dest_code"],
            "OutWardSearchDate": config["date"],
            "OutWardSearchTime": config["time_val"],
            "ReturnSearchDate":  "",
            "ReturnSearchTime":  "",
            "DiscountType":      "",
        }
        resp = session.post(
            THSR_SEARCH,
            data=form_data,
            headers={
                "Referer":           THSR_TIMETABLE_PAGE,
                "X-Requested-With":  "XMLHttpRequest",
                "Accept":            "application/json, text/javascript, */*; q=0.01",
                "Content-Type":      "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=20,
        )

        if resp.status_code == 405:
            return -1, (
                "查詢 API 被 CDN 封鎖（405）。\n"
                "請確認是否從台灣 IP 執行，或改用在地部署。"
            )

        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            return -1, f"API 回傳失敗：{data}"

        trains = (
            data.get("data", {})
                .get("DepartureTable", {})
                .get("TrainItem", [])
        )
        return len(trains), None

    except requests.exceptions.JSONDecodeError:
        return -1, "API 回傳格式非 JSON，網站可能改版"
    except requests.RequestException as e:
        return -1, f"網路錯誤: {e}"
    except Exception as e:
        return -1, str(e)[:150]


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
        session = requests.Session()
        session.headers.update(HEADERS)

        attempt = 0
        try:
            while self.running:
                attempt += 1
                self._log(f"第 {attempt} 次查詢 {self.origin}→{self.destination} {self.config['date']}...")

                count, err = _check_availability(session, self.config)

                if err:
                    self._log(f"查詢錯誤，稍後重試: {err}", "running")
                    # 被 WAF 封鎖時等久一點，避免被視為攻擊
                    wait = 30 if "405" in (err or "") else 5
                    self._interruptible_sleep(wait)
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
                    self._log(f"第 {attempt} 次：無班次結果，{self.interval} 秒後再試...")
                    self._interruptible_sleep(self.interval)
        finally:
            self.running = False
