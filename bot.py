from __future__ import annotations
import os
import time
import requests
from urllib.parse import quote
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

DISCOUNT_OPTIONS = {
    "全票":    "",
    "大學生":  "68d9fc7b-7330-44c2-962a-74bc47d2ee8a",
    "早鳥":    "e1b4c4d9-98d7-4c8c-9834-e1d2528750f1",
    "校外教學": "40863ff1-a16c-4da1-8af7-c1f8991627f3",
}

THSR_BASE         = "https://www.thsrc.com.tw"
THSR_TIMETABLE    = f"{THSR_BASE}/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c"


def _tg_notify(bot_token: str, chat_id: str, message: str) -> str | None:
    """回傳 None 代表成功，回傳字串代表錯誤原因。"""
    if not bot_token or not chat_id:
        return "tg_token 或 tg_chat_id 為空"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if not resp.ok:
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
        return None
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:150]}"


_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--hide-scrollbars",
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


def _fmt_train(t: dict) -> str:
    num  = t.get("TrainNumber", "?")
    dep  = t.get("DepartureTime", "?")
    arr  = t.get("DestinationTime", "?")
    dur  = t.get("Duration", "?")
    free = t.get("NonReservedCar", "")
    free_str = f" 自由座:{free}" if free else ""
    discs = t.get("Discount", [])
    disc_str = " | ".join(f"{d.get('Name','')} {d.get('Value','')}" for d in discs if d.get("Name"))
    disc_str = f" [{disc_str}]" if disc_str else ""
    return f"・{num}  {dep}→{arr}（{dur}）{free_str}{disc_str}"


def _get_search_url(config: dict) -> tuple[str, str | None]:
    """用 requests 直接呼叫 /TimeTable/Encrypt，取得預填搜尋 URL。
    Returns (url, error_or_None)."""
    tf = config["time_from"]
    time_str = "00:00" if tf == "0000" else f"{tf[:2]}:{tf[2:]}"
    plain = (
        f"?startStation={config['origin_code']}"
        f"&endStation={config['dest_code']}"
        f"&typesofticket=tot-1"
        f"&outWardDate={config['date']}"
        f"&outWardTime={time_str}"
        f"&returnDate=&returnTime="
        f"&offer={config.get('discount', '')}"
    )
    fallback = "https://irs.thsrc.com.tw/IMINT/?locale=tw"
    try:
        resp = requests.post(
            f"{THSR_BASE}/TimeTable/Encrypt",
            data={"plainText": plain},
            headers={
                "Referer": THSR_TIMETABLE,
                "Origin": THSR_BASE,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            },
            timeout=10,
        )
        data = resp.json()
        cipher = data.get("cipherText", "")
        if cipher:
            return f"{THSR_TIMETABLE}?search={quote(cipher, safe='+/=')}", None
        return fallback, f"HTTP {resp.status_code} cipherText 為空: {str(data)[:100]}"
    except Exception as e:
        return fallback, f"{type(e).__name__}: {str(e)[:150]}"


_BOOKING_ID_SELECTORS = [
    "#idNumber", "input[name='idNumber']", "input[name='TempOrderIdNumber']",
    "input[name*='IdNumber']", "input[placeholder*='身分證']",
]
_BOOKING_PHONE_SELECTORS = [
    "#mobilePhone", "input[name='mobilePhone']", "input[name='TempMobilePhone']",
    "input[name*='Mobile']", "input[name*='Phone']",
    "input[placeholder*='手機']", "input[placeholder*='電話']",
]
_BOOKING_EMAIL_SELECTORS = [
    "#email", "input[name='email']", "input[name='TempContactEmail']",
    "input[type='email']", "input[name*='Email']",
]

def _fill_field(page, selectors: list, value: str) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=2000)
            el.fill(value)
            return True
        except Exception:
            continue
    return False


def _screenshot(page) -> bytes:
    """截取可視區域 JPEG（避免 full_page PNG 超過 Telegram 10MB 限制）。"""
    return page.screenshot(type="jpeg", quality=80)


_FIND_BOOKING_JS = """() => {
    // 從主要內容區塊找訂票連結，排除 header/nav/footer
    const els = Array.from(document.querySelectorAll('a, button'));
    const excluded = el => el.closest('header,nav,footer,.header,.footer,.nav,.navbar');
    const candidates = els.filter(el => {
        if (excluded(el)) return false;
        const text = el.textContent.trim();
        const href = el.href || '';
        const oc   = el.getAttribute('onclick') || '';
        return text === '訂票' || text === '立即訂票' ||
               href.includes('IMINT') || oc.includes('IMINT');
    });
    if (candidates.length === 0) return null;
    const el = candidates[0];
    return {
        href:    el.href    || '',
        onclick: (el.getAttribute('onclick') || '').slice(0, 200),
        text:    el.textContent.trim(),
        tag:     el.tagName,
    };
}"""

_FILL_PASS_JS = """(data) => {
    // 在 IMINT 乘客資料頁直接用 JS 填入，繞過 Playwright selector 不確定性
    const set = (sel, val) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const nativeInput = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
        nativeInput.set.call(el, val);
        el.dispatchEvent(new Event('input',  {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        return true;
    };
    const results = {};
    results.id    = ['#idNumber','input[name=idNumber]'].some(s => set(s, data.id));
    results.phone = ['#mobilePhone','input[name=mobilePhone]','#phone','input[name=phone]']
                      .some(s => set(s, data.phone));
    results.email = data.email
        ? ['#email','input[name=email]','input[type=email]'].some(s => set(s, data.email))
        : true;
    return results;
}"""


def _book_ticket(browser, config: dict, log_fn=None) -> tuple[bytes | None, str | None]:
    """
    thsrc.com.tw cipher URL 找訂票連結 → 拿到 IMINT URL → 前往填乘客資料。
    回傳 (screenshot_jpeg_bytes, error_or_None)。
    """
    def _step(msg: str):
        if log_fn:
            log_fn(f"[訂票] {msg}")

    def _dump_fields(pg) -> str:
        try:
            return pg.evaluate(
                "() => Array.from(document.querySelectorAll('input,select'))"
                ".map(el=>el.id+'|'+el.name+'|'+el.type).join(' / ')"
            )[:600]
        except Exception:
            return ""

    page = _setup_page(browser)
    imint_page = None
    try:
        # ── Step 1：前往時刻表，找訂票連結 URL ──
        _step("前往高鐵時刻表...")
        cipher_url, _ = _get_search_url(config)
        page.goto(cipher_url, timeout=30000, wait_until="domcontentloaded")

        try:
            agree = page.locator("button:has-text('我同意')").first
            agree.wait_for(state="visible", timeout=4000)
            agree.click()
            _step("已關閉 Cookie 視窗")
            time.sleep(1)
        except Exception:
            pass

        time.sleep(3)  # 等 JS 渲染班次結果

        ss = _screenshot(page)

        _step("用 JS 尋找主內容訂票連結...")
        booking_info = page.evaluate(_FIND_BOOKING_JS)

        if not booking_info:
            fields = _dump_fields(page)
            return ss, f"找不到訂票連結\n頁面元素: {fields[:400]}"

        _step(f"找到: {booking_info}")

        # ── Step 2：前往 IMINT 訂票頁（用 commit 避免 domcontentloaded 逾時） ──
        href = booking_info.get("href", "")
        if not href or "IMINT" not in href:
            return ss, f"訂票連結格式不符: {booking_info}"

        _step("前往 IMINT 訂票頁...")
        # 開新分頁（不受目前頁面資源封鎖影響）
        imint_page = browser.new_page(viewport={"width": 1280, "height": 720})
        imint_page.goto(href, timeout=60000, wait_until="commit")
        time.sleep(3)

        # ── Step 3：填乘客資料 ──
        fields_raw = _dump_fields(imint_page)
        _step(f"IMINT 欄位: {fields_raw[:200]}")

        result = imint_page.evaluate(_FILL_PASS_JS, {
            "id":    config.get("id_number", ""),
            "phone": config.get("phone", ""),
            "email": config.get("email", ""),
        })
        _step(f"填入結果: {result}")

        ss = _screenshot(imint_page)
        if not result.get("id") and not result.get("phone"):
            return ss, f"欄位填入失敗\nIMINT 欄位: {fields_raw[:400]}"
        _step("完成！等你確認後付款")
        return ss, None

    except Exception as e:
        try:
            pg = imint_page or page
            ss = _screenshot(pg)
        except Exception:
            ss = None
        return ss, f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        for pg in [page, imint_page]:
            if pg:
                try:
                    pg.close()
                except Exception:
                    pass


def _tg_send_photo(bot_token: str, chat_id: str, photo_bytes: bytes, caption: str) -> str | None:
    if not bot_token or not chat_id:
        return "tg_token 或 tg_chat_id 為空"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("booking.jpg", photo_bytes, "image/jpeg")},
            timeout=30,
        )
        if not resp.ok:
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
        return None
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:150]}"


def _query_with_browser(browser, config: dict) -> tuple[list, str | None]:
    """每次查詢建立新分頁，查完直接 close()，避免殘留分頁狀態造成 TargetClosedError。"""
    page = _setup_page(browser)
    try:
        page.goto(THSR_TIMETABLE, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_function("typeof $ !== 'undefined'", timeout=15000)

        t = config["time_from"]
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
            "DiscountType":      config.get("discount", ""),
        })
    finally:
        try:
            page.close()
        except Exception:
            pass

    if not result or not result.get("success"):
        return [], f"API 回傳失敗：{result}"

    all_trains = (
        result.get("data", {})
              .get("DepartureTable", {})
              .get("TrainItem", [])
    )
    time_to = config.get("time_to", "2359")
    filtered = [
        tr for tr in all_trains
        if tr.get("DepartureTime", "").replace(":", "") <= time_to
    ]
    return filtered, None


class THSRBot:
    def __init__(self, config: dict, status_callback):
        self.config = {
            "origin_code": STATIONS[config["origin"]],
            "dest_code":   STATIONS[config["destination"]],
            "date":        config["date"],
            "time_from":   config["time"],
            "time_to":     config.get("time_to", "2359"),
            "seat_type":   config["seat_type"],
            "adult":       int(config["adult"]),
            "discount":    config.get("discount", ""),
            "id_number":   config.get("id_number", ""),
            "phone":       config.get("phone", ""),
            "email":       config.get("email", ""),
        }
        self.origin      = config["origin"]
        self.destination = config["destination"]
        self.interval    = int(config.get("interval", 30))
        self.tg_token    = config.get("tg_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat_id  = config.get("tg_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.base_url    = config.get("base_url", "").rstrip("/")
        self.callback    = status_callback
        self.running     = False
        self._found      = False

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
        self._found  = False  # reset per-run
        attempt = 0

        while self.running and not self._found:
            try:
                with sync_playwright() as p:
                    browser = self._launch_browser(p)
                    try:
                        while self.running and not self._found:
                            attempt += 1
                            tf, tt = self.config["time_from"], self.config["time_to"]
                            from_s = "不限" if tf == "0000" else f"{tf[:2]}:{tf[2:]}"
                            to_s   = "不限" if tt == "2359" else f"{tt[:2]}:{tt[2:]}"
                            self._log(
                                f"第 {attempt} 次查詢 {self.origin}→{self.destination} "
                                f"{self.config['date']} {from_s}-{to_s}..."
                            )

                            err    = None
                            trains = []
                            try:
                                if not browser.is_connected():
                                    self._log("Browser 斷線，重新啟動瀏覽器...")
                                    try:
                                        browser.close()
                                    except Exception:
                                        pass
                                    browser = self._launch_browser(p)

                                trains, err = _query_with_browser(browser, self.config)

                            except PWTimeout:
                                err = "查詢逾時（30 秒）"
                            except Exception as e:
                                err = str(e)[:200]
                                try:
                                    browser.close()
                                except Exception:
                                    pass
                                browser = self._launch_browser(p)

                            if err:
                                self._log(f"查詢錯誤，稍後重試: {err}", "running")
                                self._interruptible_sleep(10)
                                continue

                            if trains:
                                self._found    = True
                                seat_label  = "商務" if self.config.get("seat_type") == "2" else "標準"
                                adult       = self.config.get("adult", 1)
                                tf, tt      = self.config["time_from"], self.config["time_to"]
                                from_str    = "不限" if tf == "0000" else f"{tf[:2]}:{tf[2:]}"
                                to_str      = "不限" if tt == "2359" else f"{tt[:2]}:{tt[2:]}"
                                disc_name   = next(
                                    (k for k, v in DISCOUNT_OPTIONS.items() if v == self.config.get("discount", "")),
                                    "全票"
                                )
                                lines = "\n".join(_fmt_train(t) for t in trains[:3])
                                if len(trains) > 3:
                                    lines += f"\n...另有 {len(trains)-3} 班"
                                if self.base_url:
                                    booking_url = (
                                        f"{self.base_url}/api/go"
                                        f"?o={self.config['origin_code']}"
                                        f"&d={self.config['dest_code']}"
                                        f"&dt={quote(self.config['date'], safe='')}"
                                        f"&t={self.config['time_from']}"
                                        f"&tt={self.config.get('time_to', '2359')}"
                                        f"&dis={quote(self.config.get('discount', ''), safe='')}"
                                        f"&st={self.config.get('seat_type', '1')}"
                                        f"&adult={self.config.get('adult', 1)}"
                                    )
                                else:
                                    booking_url, enc_err = _get_search_url(self.config)
                                    if enc_err:
                                        self._log(f"[Encrypt 失敗] {enc_err}", "running")
                                msg = (
                                    f"高鐵放票通知！\n"
                                    f"{self.origin} → {self.destination}｜{self.config['date']}\n"
                                    f"時段：{from_str} - {to_str}｜{seat_label}廂 {adult} 張｜{disc_name}\n"
                                    f"\n找到 {len(trains)} 班：\n{lines}\n"
                                    f"\n立即訂票：\n{booking_url}"
                                )
                                # Telegram 上限 4096 字元
                                if len(msg) > 4000:
                                    msg = msg[:3980] + "\n...(訊息已截斷)"
                                self._log(f"找到 {len(trains)} 班可搭車次！", "found", found=True)
                                has_tg = bool(self.tg_token and self.tg_chat_id)
                                self._log(f"[TG] 準備送出通知 token={'有' if self.tg_token else '無'} chat_id={'有' if self.tg_chat_id else '無'}")
                                tg_err = _tg_notify(self.tg_token, self.tg_chat_id, msg)
                                if tg_err:
                                    self._log(f"[TG] 送出失敗: {tg_err}", "running")
                                elif has_tg:
                                    self._log("[TG] 通知已送出")

                                # 自動填入乘客資料
                                if self.config.get("id_number"):
                                    self._log("開始自動填入乘客資料...")
                                    ss, book_err = _book_ticket(browser, self.config, self._log)
                                    if ss and has_tg:
                                        caption = (
                                            "✅ 乘客資料已自動填入，請確認截圖後完成付款"
                                            if not book_err
                                            else f"⚠️ 自動填入中斷：{book_err}\n請參考截圖手動繼續"
                                        )
                                        photo_err = _tg_send_photo(self.tg_token, self.tg_chat_id, ss, caption)
                                        if photo_err:
                                            self._log(f"[TG] 截圖傳送失敗: {photo_err}")
                                        else:
                                            self._log("[TG] 訂票截圖已傳送")
                                    if book_err:
                                        self._log(f"[訂票] {book_err}")
                            else:
                                self._log(
                                    f"第 {attempt} 次：時段內無班次，{self.interval} 秒後再試..."
                                )
                                self._interruptible_sleep(self.interval)
                    finally:
                        try:
                            browser.close()
                        except Exception:
                            pass

            except Exception as e:
                # sync_playwright() context 本身異常，整個重啟
                if not self.running:
                    break
                self._log(f"Playwright 環境異常，5 秒後重啟: {str(e)[:100]}", "running")
                self._interruptible_sleep(5)

        self.running = False
        if not self._found:
            self.callback("stopped", "機器人已停止")
