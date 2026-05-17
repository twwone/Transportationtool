import os
import time
import requests
from bs4 import BeautifulSoup

STATIONS = {
    "南港": "0990",
    "台北": "1000",
    "板橋": "1010",
    "桃園": "1020",
    "新竹": "1030",
    "苗栗": "1035",
    "台中": "1040",
    "彰化": "1043",
    "雲林": "1047",
    "嘉義": "1050",
    "台南": "1060",
    "左營": "1070",
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

THSR_SEARCH_URL = "https://www.thsrc.com.tw/tw/TimeTable/SearchByStation"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
        # Step 1: GET 首頁取得 cookies 和隱藏欄位
        resp = session.get(THSR_SEARCH_URL, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        form = soup.find("form")
        if not form:
            return -1, "找不到搜尋表單，高鐵網站可能改版"

        # 取出所有隱藏欄位（Wicket 框架需要）
        form_data: dict[str, str] = {}
        for inp in form.find_all("input", type="hidden"):
            name = inp.get("name", "")
            if name:
                form_data[name] = inp.get("value", "")

        # 填入搜尋條件
        form_data.update({
            "selectStartStation":               config["origin_code"],
            "selectDestinationStation":          config["dest_code"],
            "trainConDate":                      config["date"],
            "trainConTime":                      config["time_val"],
            "seatCon:seatRadioGroup":            config["seat_type"],
            "ticketPanel:rows:0:ticketAmount":   str(config["adult"]),
            "ticketPanel:rows:1:ticketAmount":   "0",
            "ticketPanel:rows:2:ticketAmount":   "0",
            "ticketPanel:rows:3:ticketAmount":   "0",
            "ticketPanel:rows:4:ticketAmount":   "0",
        })

        # 找送出按鈕的 name
        btn = form.find("input", id="btnSubmit") or form.find("button", id="btnSubmit")
        if btn and btn.get("name"):
            form_data[btn["name"]] = btn.get("value", "submit")

        # Step 2: POST 搜尋
        action = form.get("action", THSR_SEARCH_URL)
        if action and not action.startswith("http"):
            action = "https://www.thsrc.com.tw" + action

        resp = session.post(
            action or THSR_SEARCH_URL,
            data=form_data,
            headers={"Referer": THSR_SEARCH_URL,
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Step 3: 判斷有無可訂班次
        # 方法一：找可點擊的訂票連結（非 disabled）
        available = 0
        for a in soup.select("a"):
            classes = " ".join(a.get("class", []))
            text = a.get_text(strip=True)
            if ("disabled" not in classes) and any(kw in text for kw in ["立即訂票", "選擇", "訂票"]):
                available += 1

        # 方法二：找含「售完」的班次，用總班次數扣掉
        if available == 0:
            all_rows = soup.select("tr.rich-table-row, .result-item, [class*='train']")
            sold_out = len(soup.find_all(string=lambda t: t and ("售完" in t or "額滿" in t)))
            if all_rows and len(all_rows) > sold_out:
                available = len(all_rows) - sold_out

        # 方法三：頁面有無「查無班次」訊息
        no_result_keywords = ["查無班次", "查無資料", "無搜尋結果", "沒有符合"]
        page_text = soup.get_text()
        if any(kw in page_text for kw in no_result_keywords):
            return 0, None

        return available, None

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
                    self._interruptible_sleep(5)
                    continue

                if count > 0:
                    msg = (
                        f"🚄 高鐵放票通知\n"
                        f"{self.origin}→{self.destination}\n"
                        f"{self.config['date']}\n"
                        f"找到 {count} 個可訂班次，趕快去搶！"
                    )
                    self._log(f"找到 {count} 個可訂班次！", "found", found=True)
                    _tg_notify(self.tg_token, self.tg_chat_id, msg)
                    break
                else:
                    self._log(f"第 {attempt} 次：無可用座位，{self.interval} 秒後再試...")
                    self._interruptible_sleep(self.interval)
        finally:
            self.running = False
