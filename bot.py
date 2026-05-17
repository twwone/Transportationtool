import os
import time
import requests as http_requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

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

THSR_URL = "https://www.thsrc.com.tw/tw/TimeTable/SearchByStation"


def _line_notify(token: str, message: str):
    if not token:
        return
    try:
        http_requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {token}"},
            data={"message": message},
            timeout=10,
        )
    except Exception:
        pass


def _build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    # Railway / Linux：使用系統 chromium
    chromium_path = "/usr/bin/chromium"
    if os.path.exists(chromium_path):
        options.binary_location = chromium_path
        service = Service("/usr/bin/chromedriver")
    else:
        # 本機 macOS：用 webdriver-manager
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


class THSRBot:
    def __init__(self, config: dict, status_callback):
        self.origin      = config["origin"]
        self.destination = config["destination"]
        self.date        = config["date"]        # 格式: 2026/05/17
        self.time_val    = config["time"]        # 格式: "0700"
        self.seat_type   = config["seat_type"]   # "1"=標準 "2"=商務
        self.adult       = int(config["adult"])
        self.interval    = int(config.get("interval", 30))
        self.line_token  = config.get("line_token") or os.environ.get("LINE_NOTIFY_TOKEN", "")
        self.callback    = status_callback
        self.running     = False
        self.driver: webdriver.Chrome | None = None

    def stop(self):
        self.running = False

    def _log(self, msg: str, status: str = "running", found: bool = False):
        self.callback(status, msg, found)

    def _check_once(self) -> tuple[int, str | None]:
        try:
            self.driver.get(THSR_URL)
            wait = WebDriverWait(self.driver, 15)

            wait.until(EC.presence_of_element_located((By.ID, "selectStartStation")))
            Select(self.driver.find_element(By.ID, "selectStartStation")).select_by_value(
                STATIONS[self.origin]
            )
            Select(self.driver.find_element(By.ID, "selectDestinationStation")).select_by_value(
                STATIONS[self.destination]
            )

            date_el = self.driver.find_element(By.ID, "trainConDate")
            self.driver.execute_script(
                "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
                date_el, self.date,
            )

            Select(self.driver.find_element(By.ID, "trainConTime")).select_by_value(self.time_val)

            for radio in self.driver.find_elements(By.CSS_SELECTOR, "input[name='seatCon:seatRadioGroup']"):
                if radio.get_attribute("value") == self.seat_type:
                    radio.click()
                    break

            Select(
                self.driver.find_element(By.CSS_SELECTOR, "select[name='ticketPanel:rows:0:ticketAmount']")
            ).select_by_value(str(self.adult))

            self.driver.find_element(By.ID, "btnSubmit").click()
            time.sleep(4)

            # 找可點擊的訂票按鈕
            btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a.result-item-btn:not(.disabled), input.btn-booking[type='submit']:not([disabled])",
            )
            if not btns:
                btns = [
                    el for el in self.driver.find_elements(
                        By.XPATH, "//*[contains(text(),'立即訂票') or contains(text(),'選擇')]"
                    )
                    if el.is_displayed() and el.is_enabled()
                ]

            return len(btns), None

        except Exception as e:
            return -1, str(e)

    def run(self):
        self.running = True
        self._log("啟動瀏覽器...")

        try:
            self.driver = _build_driver()
        except Exception as e:
            self._log(f"瀏覽器啟動失敗: {e}", "error")
            self.running = False
            return

        attempt = 0
        try:
            while self.running:
                attempt += 1
                self._log(f"第 {attempt} 次查詢 {self.origin}→{self.destination} {self.date}...")

                count, err = self._check_once()

                if err:
                    self._log(f"查詢錯誤: {err}", "error")
                    time.sleep(5)
                    continue

                if count > 0:
                    msg = f"\n🚄 高鐵放票通知\n{self.origin}→{self.destination}\n{self.date}\n找到 {count} 個可訂班次，趕快去搶！"
                    self._log(f"找到 {count} 個可訂班次！", "found", found=True)
                    _line_notify(self.line_token, msg)
                    break
                else:
                    self._log(f"第 {attempt} 次：無可用座位，{self.interval} 秒後再試...")
                    for _ in range(self.interval):
                        if not self.running:
                            break
                        time.sleep(1)
        finally:
            self.running = False
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
