#!/usr/bin/env python3
"""從 passport-index-dataset 建 visa-matrix.json 與 visa-countries.json。
用法：
  python3 scripts/build_visa_matrix.py           # 建檔
  python3 scripts/build_visa_matrix.py --selftest # 只跑純函式自測
"""
from __future__ import annotations
import csv, io, json, os, sys, urllib.request

CSV_URL = "https://raw.githubusercontent.com/ilyankou/passport-index-dataset/master/passport-index-tidy-iso2.csv"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORT_CODES = os.path.join(ROOT, "static", "airport_codes.json")
OUT_MATRIX = os.path.join(ROOT, "static", "visa-matrix.json")
OUT_COUNTRIES = os.path.join(ROOT, "static", "visa-countries.json")

# 簽證頁顯示名覆寫：本頁全站慣用「台灣」而非官方全名
DISPLAY_OVERRIDE = {"TW": "台灣"}

def normalize(req: str):
    """把資料集原值轉成 (狀態碼, 天數|None)。同國或空值回傳 None。"""
    r = req.strip().lower()
    if r in ("", "-1"):
        return None
    if r.isdigit():
        return ("f", int(r))          # 數字 = 免簽 N 天
    return {
        "visa free":        ("f", None),
        "visa on arrival":  ("o", None),
        "e-visa":           ("e", None),
        "eta":              ("a", None),
        "visa required":    ("r", None),
        "no admission":     ("n", None),
    }.get(r)

def flag_emoji(code: str) -> str:
    """ISO2 代碼 → 國旗 emoji（區域指示符號組合）。"""
    code = code.strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)

def build_matrix(rows):
    """rows: iterable of (passport, dest, requirement) → 巢狀 dict。"""
    matrix = {}
    for passport, dest, req in rows:
        norm = normalize(req)
        if norm is None:
            continue
        s, d = norm
        entry = {"s": s}
        if d is not None:
            entry["d"] = d
        matrix.setdefault(passport, {})[dest] = entry
    return matrix

def _selftest():
    assert normalize("90") == ("f", 90)
    assert normalize("visa free") == ("f", None)
    assert normalize("visa on arrival") == ("o", None)
    assert normalize("e-visa") == ("e", None)
    assert normalize("eta") == ("a", None)
    assert normalize("visa required") == ("r", None)
    assert normalize("no admission") == ("n", None)
    assert normalize("-1") is None
    assert normalize("") is None
    assert flag_emoji("TW") == "🇹🇼"
    assert flag_emoji("JP") == "🇯🇵"
    assert flag_emoji("XX?") == "🏳️"
    m = build_matrix([("TW", "JP", "90"), ("TW", "US", "eta"), ("TW", "TW", "-1")])
    assert m == {"TW": {"JP": {"s": "f", "d": 90}, "US": {"s": "a"}}}
    print("selftest OK")

def main():
    with urllib.request.urlopen(CSV_URL, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert header == ["Passport", "Destination", "Requirement"], header
    matrix = build_matrix(reader)

    # 收集矩陣中出現的所有代碼（護照 + 目的地）
    codes = set(matrix.keys())
    for dests in matrix.values():
        codes.update(dests.keys())

    # 中文名取自現有 airport_codes.json 的 countries（253 筆）
    with open(AIRPORT_CODES, encoding="utf-8") as f:
        ac = json.load(f)
    name_map = {c["code"]: c["name_zh"] for c in ac.get("countries", [])}
    # 簽證頁專用顯示名覆寫（不動共用的 airport_codes.json，避免影響其他頁面）
    name_map.update(DISPLAY_OVERRIDE)
    missing = sorted(c for c in codes if c not in name_map)
    if missing:
        print(f"[warn] 這些代碼沒有中文名，將以代碼顯示: {missing}")

    countries = [
        {"code": c, "name_zh": name_map.get(c, c), "flag": flag_emoji(c)}
        for c in sorted(codes)
    ]

    with open(OUT_MATRIX, "w", encoding="utf-8") as f:
        json.dump(matrix, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT_COUNTRIES, "w", encoding="utf-8") as f:
        json.dump(countries, f, ensure_ascii=False, separators=(",", ":"))
    print(f"寫入 {OUT_MATRIX}（{len(matrix)} 本護照）")
    print(f"寫入 {OUT_COUNTRIES}（{len(countries)} 國）")

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
