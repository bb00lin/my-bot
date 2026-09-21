#!/usr/bin/env python3

"""離線驗證 DailyStockPush.py 的歷史資料長度防護（不需憑證、不連外網）。

重現的問題：WATCH_LIST 裡四檔手動填的庫存 ETF，只有 00997A 出現在
「全能金流診斷報表」，另外三檔靜默消失。原因是 fetch_pro_metrics 的
`if len(df_hist) < 120: return None` 會把上市未滿約半年的標的整檔丟掉，
而且三條略過路徑都沒有任何 log。

本腳本用四檔的真實交易日數（yfinance period="8mo" 實測）驗證修復結果：
    00403A 主動統一升級50   103 日
    00409A 主動復華全球50    21 日
    009824 群益美國科技巨頭  70 日
    00997A 主動群益美國增長 122 日

DailyStockPush 在 module 層就會呼叫 FinMind 抓台股清單，所以先把需要網路的
第三方套件換成假模組，再用假環境變數 import。
"""

from __future__ import annotations

import io
import os
import sys
import types
from contextlib import redirect_stdout

import pandas as pd


PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"== {title}")
    print("=" * 70)


# ==========================================
# 離線假模組
# ==========================================
def install_offline_modules() -> None:
    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = lambda *a, **k: None
    sys.modules["yfinance"] = fake_yf

    finmind = types.ModuleType("FinMind")
    finmind_data = types.ModuleType("FinMind.data")

    class DataLoader:
        def taiwan_stock_info(self):
            return pd.DataFrame()

        def taiwan_stock_institutional_investors(self, **kwargs):
            return pd.DataFrame()

    finmind_data.DataLoader = DataLoader
    finmind.data = finmind_data
    sys.modules["FinMind"] = finmind
    sys.modules["FinMind.data"] = finmind_data

    gspread = types.ModuleType("gspread")
    gspread.authorize = lambda creds: None
    sys.modules["gspread"] = gspread

    oauth = types.ModuleType("oauth2client")
    oauth_sa = types.ModuleType("oauth2client.service_account")

    class ServiceAccountCredentials:
        @staticmethod
        def from_json_keyfile_dict(*a, **k):
            return None

    oauth_sa.ServiceAccountCredentials = ServiceAccountCredentials
    oauth.service_account = oauth_sa
    sys.modules["oauth2client"] = oauth
    sys.modules["oauth2client.service_account"] = oauth_sa

    google_mod = sys.modules.get("google") or types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = lambda **k: None
    google_mod.genai = genai
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai


install_offline_modules()
os.environ["ENABLE_AI"] = "false"  # 避免 import 時去測 AI 連線
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("CURSOR_API_KEY", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import DailyStockPush as m  # noqa: E402


# ==========================================
# 測試用資料
# ==========================================
def make_frame(days: int, start_price: float = 10.0, trend: float = 0.002,
               volume: int = 1_000_000) -> pd.DataFrame:
    """做出 days 個交易日、緩步上漲的 OHLCV，用來模擬不同上市長度的標的。"""
    idx = pd.bdate_range("2026-01-05", periods=days)
    close = pd.Series([start_price * ((1 + trend) ** i) for i in range(days)], index=idx, dtype=float)
    return pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.010,
            "Low": close * 0.990,
            "Close": close,
            "Volume": pd.Series([volume] * days, index=idx, dtype=float),
        },
        index=idx,
    )


class FakeTicker:
    def __init__(self, frame: pd.DataFrame, raises: bool = False):
        self._frame = frame
        self._raises = raises
        self.info = {"profitMargins": 0.1, "dividendYield": 0.03, "sector": "ETF"}

    def history(self, period=None):
        if self._raises:
            raise RuntimeError("yfinance 連線中斷")
        return self._frame


def patch_sources(frame: pd.DataFrame, raises: bool = False) -> None:
    m.get_tw_stock = lambda sid: (FakeTicker(frame, raises=raises), f"{sid}.TW")
    m.get_inst_stats = lambda pure_id: (0, 0, 0, 0)


def fetch(sid: str, name: str, days: int, is_hold: bool,
          raises: bool = False) -> tuple[dict | None, str]:
    """跑一次 fetch_pro_metrics，同時把它印出來的 log 攔下來檢查。"""
    patch_sources(make_frame(days), raises=raises)
    stock_data = {"sid": sid, "name": name, "is_hold": is_hold, "cost": 10.0, "skip_ai": False}
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        res = m.fetch_pro_metrics(stock_data)
    return res, buffer.getvalue()


# ==========================================
# 1. 四檔庫存 ETF 都要進報表
# ==========================================
WATCH_LIST_ETFS = [
    ("00403A", "主動統一升級50", 103),
    ("00409A", "主動復華全球50", 21),
    ("009824", "群益美國科技巨頭", 70),
    ("00997A", "主動群益美國增長", 122),
]


def verify_watch_list_etfs() -> None:
    hr("1. WATCH_LIST 四檔庫存 ETF（真實交易日數）")

    for sid, name, days in WATCH_LIST_ETFS:
        res, _ = fetch(sid, name, days, is_hold=True)
        check(f"{sid} {name}（{days} 個交易日）有進報表", res is not None)
        if res is None:
            continue
        check(f"{sid} 名稱與庫存狀態正確",
              res["name"] == name and res["is_hold"] is True, f"name={res['name']}")
        check(f"{sid} 現價有值", res["p"] is not None)
        check(f"{sid} 記錄了實際交易日數 {days}", res.get("days") == days, f"days={res.get('days')}")

        # 季線要 60 天、6 個月報酬要 121 天；不足的欄位留空但不影響整列
        expect_ma60 = days >= 60
        check(f"{sid} 季線 MA60 {'有值' if expect_ma60 else '留空'}",
              (res["ma60"] is not None) == expect_ma60, f"ma60={res['ma60']}")
        expect_m6 = days >= 121
        check(f"{sid} 6M 漲幅 {'有值' if expect_m6 else '留空'}",
              (res["m6"] is not None) == expect_m6, f"m6={res['m6']}")

        if not expect_ma60:
            check(f"{sid} 乖離欄位顯示「{m.NO_DATA_TEXT}」而不是 nan",
                  res["bias_str"] == m.NO_DATA_TEXT, res["bias_str"])
            check(f"{sid} 訊號欄標注新上市", "🆕上市僅" in res["ma_alert"], res["ma_alert"])


def verify_thresholds() -> None:
    hr("2. 門檻與降級行為")

    res, log = fetch("00403A", "主動統一升級50", 103, is_hold=False)
    check("觀察股 103 日可通過（門檻與 DailyStockBot.py 選股的 60 天一致）", res is not None)

    res, log = fetch("1234", "測試觀察股", 40, is_hold=False)
    check("觀察股 40 日未達 60 天門檻，仍會被略過", res is None)
    check("略過觀察股時會印出原因與天數",
          "略過" in log and "40 個交易日" in log and "60" in log, log.strip())

    res, log = fetch("1234", "測試庫存股", 40, is_hold=True)
    check("同一檔標為庫存就放寬到 20 天門檻，不會消失", res is not None)

    res, log = fetch("1234", "測試庫存股", 10, is_hold=True)
    check("庫存股 10 日連月線都算不出來，仍會被略過", res is None)
    check("略過庫存股時 log 會標明是庫存門檻",
          "庫存" in log and "20" in log, log.strip())

    # 原本 `len < 120` 放行、`iloc[-121]` 卻需要 121 筆，剛好 120 日會 IndexError 後被裸 except 吞掉
    res, log = fetch("1234", "剛好120日", 120, is_hold=False)
    check("剛好 120 個交易日不再觸發 IndexError（原本的 off-by-one）", res is not None)
    check("剛好 120 日時 6M 漲幅留空", res is not None and res["m6"] is None,
          f"m6={res['m6'] if res else None}")
    check("剛好 120 日不應出現任何錯誤 log", "略過" not in log, log.strip())

    res, _ = fetch("1234", "剛好121日", 121, is_hold=False)
    check("121 個交易日 6M 漲幅有值", res is not None and res["m6"] is not None)


def verify_skip_logging() -> None:
    hr("3. 略過路徑都要留下 log")

    m.get_tw_stock = lambda sid: (None, None)
    m.get_inst_stats = lambda pure_id: (0, 0, 0, 0)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        res = m.fetch_pro_metrics({"sid": "9999", "name": "查不到的股",
                                   "is_hold": True, "cost": 0, "skip_ai": False})
    log = buffer.getvalue()
    check("yfinance 查不到報價時回 None", res is None)
    check("查不到報價會印出 .TW/.TWO 都失敗的原因",
          "略過" in log and "查不到" in log, log.strip())

    res, log = fetch("1234", "抓取失敗股", 150, is_hold=True, raises=True)
    check("抓取過程拋出例外時回 None", res is None)
    check("例外不再被裸 except 吞掉，會印出型別與訊息",
          "略過" in log and "RuntimeError" in log, log.strip())


def verify_none_safe_consumers() -> None:
    hr("4. 下游計算與文字輸出要能吃缺值")

    short, _ = fetch("00409A", "主動復華全球50", 21, is_hold=True)
    check("前置條件：短天期樣本取得成功", short is not None)
    if not short:
        return

    check("21 個交易日仍算得出 MA5/MA10/MA20",
          None not in (short["ma5"], short["ma10"], short["ma20"]),
          f"ma5={short['ma5']} ma10={short['ma10']} ma20={short['ma20']}")
    check("21 個交易日的季線為缺值", short["ma60"] is None)
    check("季線缺值時不會誤判長線主升浪", short["is_long_term"] is False)
    check("季線缺值時趨勢欄不是空頭修正", short["trend"] != "📉空頭修正", short["trend"])
    check("風險評級仍有值", short["risk"] in ("🚨高檔過熱", "⚠️破線警戒", "🟢正常"), short["risk"])

    # 均線與日漲跌都缺值的極端情況：原本任一比較都會拋 TypeError
    no_ma = dict(short, ma10=None, ma20=None, ma60=None, d1=None)
    score, reason = m.get_limit_up_potential(no_ma)
    check("漲停潛力評分遇到缺值均線不會拋出 TypeError", isinstance(score, int), f"score={score}")
    check("均線缺值時不會誤判為均線多頭發散", "均線多頭發散" not in reason, reason)
    check("日漲跌缺值時不會誤判為長紅棒", "長紅棒" not in reason, reason)

    alert = m.check_ma_status(short["p"], short["ma5"], short["ma10"], None, None)
    check("均線警示函式可接受 MA20/MA60 缺值", isinstance(alert, str), alert)

    check("數值欄位缺值寫入 Sheets 時轉成空白", m.sheet_value(None) == "")
    check("數值欄位有值時原樣寫入", m.sheet_value(1.23) == 1.23)
    check("百分比文字欄位缺值顯示「資料不足」", m.fmt_pct(None) == m.NO_DATA_TEXT)
    check("百分比文字欄位有值時維持原格式", m.fmt_pct(-2.7) == "-2.7%")
    check("比率文字欄位缺值顯示「資料不足」", m.fmt_ratio(None) == m.NO_DATA_TEXT)
    check("比率文字欄位有值時維持原格式", m.fmt_ratio(0.0123) == "1.23%")

    # 戰略總結每檔都包在 try/except 裡，過去 f-string 遇到 None 會格式化失敗後靜默 continue，
    # 導致符合引擎條件的標的整檔從報告消失。
    candidate = dict(short, ma60=None, d1=None, is_first_golden_cross=True)
    captured = {}

    def fake_ai(prompt, preserve_newlines=False):
        captured["prompt"] = prompt
        return prompt

    original_has_genai, original_generate = m.HAS_GENAI, m.generate_ai_content
    m.HAS_GENAI = True
    m.generate_ai_content = fake_ai
    try:
        summary = m.generate_and_save_summary([candidate], "2026-09-21 15:24")
    finally:
        m.HAS_GENAI = original_has_genai
        m.generate_ai_content = original_generate

    check("戰略總結有送出 prompt", "prompt" in captured)
    check("缺值標的仍留在戰略總結的引擎B 區塊", candidate["name"] in summary)
    check("戰略總結不會出現裸的 None", "None" not in summary,
          next((ln for ln in summary.splitlines() if "None" in ln), ""))
    check("戰略總結用「資料不足」表示缺值", m.NO_DATA_TEXT in summary)


def main() -> int:
    verify_watch_list_etfs()
    verify_thresholds()
    verify_skip_logging()
    verify_none_safe_consumers()

    hr("驗證結果")
    print(f"PASS: {PASS}    FAIL: {FAIL}")
    if FAIL:
        print("❌ 有項目未通過，請先修正再 push。")
        return 1
    print("✅ 全部通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
