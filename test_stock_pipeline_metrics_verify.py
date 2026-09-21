#!/usr/bin/env python3

"""離線驗證合併後 DailyStockBot.py 的階段切換與歷史資料長度防護（不需憑證、不連外網）。

重現的問題：WATCH_LIST 裡四檔手動填的庫存 ETF，只有 00997A 出現在
「全能金流診斷報表」，另外三檔靜默消失。原因是 fetch_pro_metrics 的
`if len(df_hist) < 120: return None` 會把上市未滿約半年的標的整檔丟掉，
而且三條略過路徑都沒有任何 log。

本腳本用四檔的真實交易日數（yfinance period="8mo" 實測）驗證修復結果：
    00403A 主動統一升級50   103 日
    00409A 主動復華全球50    21 日
    009824 群益美國科技巨頭  70 日
    00997A 主動群益美國增長 122 日

同時驗證 DailyStockPush.py 併入後的 --stage 路由：scan / push / full 各自
只跑該跑的階段。需要網路的第三方套件一律換成假模組，再用假環境變數 import。
"""

from __future__ import annotations

import io
import os
import sys
import types
from contextlib import redirect_stderr, redirect_stdout

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
os.environ["ENABLE_AI"] = "false"  # run_push() 的 AI 連線測試一律關閉
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("CURSOR_API_KEY", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import DailyStockBot as m  # noqa: E402


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


def verify_stage_routing() -> None:
    hr("5. --stage 階段路由（DailyStockPush.py 併入後）")

    calls = []
    original_scan, original_push = m.run_scan, m.run_push
    m.run_scan = lambda: calls.append("scan")
    m.run_push = lambda: calls.append("push")

    def run_main(argv, stage_env=None):
        """跑一次 main()，回傳 (實際執行的階段, log)。"""
        calls.clear()
        if stage_env is None:
            os.environ.pop("STAGE", None)
        else:
            os.environ["STAGE"] = stage_env
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            m.main(argv)
        return list(calls), buffer.getvalue()

    try:
        for argv, expected in (
            (["--stage", "full"], ["scan", "push"]),
            (["--stage", "scan"], ["scan"]),
            (["--stage", "push"], ["push"]),
            ([], ["scan", "push"]),
        ):
            label = " ".join(argv) or "(未指定 --stage)"
            got, log = run_main(argv)
            check(f"`{label}` 執行 {expected}", got == expected, f"實際={got}")
            check(f"`{label}` 會把階段印在 log 上", "執行階段" in log, log.strip())

        check("完整流程一定是先掃描後推播", run_main(["--stage", "full"])[0] == ["scan", "push"])

        # 排程觸發時 workflow 傳進來的是空字串，必須視為「使用預設值」而不是錯誤
        got, _ = run_main([], stage_env="")
        check("STAGE 為空字串時跑完整 pipeline", got == ["scan", "push"], f"實際={got}")

        got, _ = run_main([], stage_env="push")
        check("STAGE=push 只跑推播階段", got == ["push"], f"實際={got}")

        got, _ = run_main([], stage_env=" PUSH ")
        check("STAGE 的大小寫與前後空白容錯", got == ["push"], f"實際={got}")

        got, _ = run_main(["--stage", "scan"], stage_env="push")
        check("--stage 參數優先於 STAGE 環境變數", got == ["scan"], f"實際={got}")

        for bad_source, argv, stage_env in (
            ("STAGE 環境變數", [], "deploy"),
            ("--stage 參數", ["--stage", "deploy"], None),
        ):
            try:
                run_main(argv, stage_env=stage_env)
                ok = False
                detail = "沒有拋出 SystemExit"
            except SystemExit as exc:
                ok = exc.code not in (0, None)
                detail = f"exit={exc.code}"
            check(f"{bad_source} 值非法時以非 0 exit code 結束", ok, detail)
    finally:
        m.run_scan, m.run_push = original_scan, original_push
        os.environ.pop("STAGE", None)


def _gspread_numericise(value):
    """複製 gspread.utils.numericise 對純數字字串的行為（'009824' -> 9824）。"""
    if not isinstance(value, str):
        return value
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


class FakeWorksheet:
    def __init__(self, header, rows):
        self.header = header
        self.rows = rows
        self.received_kwargs = None

    def get_all_records(self, **kwargs):
        # 模擬 gspread：預設把純數字字串轉成 int，除非 numericise_ignore=['all']
        self.received_kwargs = kwargs
        rows = self.rows
        if kwargs.get("numericise_ignore") != ["all"]:
            rows = [[_gspread_numericise(v) for v in row] for row in rows]
        return [dict(zip(self.header, row)) for row in rows]

    def col_values(self, index):
        return []


class FakeSpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def worksheet(self, title):
        if title == "WATCH_LIST":
            return self._worksheet
        raise KeyError(title)  # AI_Blacklist 不存在時要能繼續

    def get_worksheet(self, index):
        return self._worksheet


class FakeClient:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def open(self, title):
        return FakeSpreadsheet(self._worksheet)


def verify_watch_list_reading() -> None:
    hr("6. WATCH_LIST 讀取：代號的前導零不能掉")

    check(
        "前提：gspread 預設會把 '009824' 轉成 9824（本次 bug 的成因）",
        _gspread_numericise("009824") == 9824,
    )
    check(
        "前提：含字母的 '00403A' 不會被轉換，所以只有純數字代號受害",
        _gspread_numericise("00403A") == "00403A",
    )
    check(
        "前提：轉換過的 9824 無法事後補零（和真實 4 碼股票 9824 無法區分）",
        m.normalize_stock_id("9824") == "9824",
    )

    header = ['股票代號', '股票名稱', '我的庫存倉位', '平均成本', '股數', '推薦理由', '日期']
    rows = [
        ['00403A', '主動統一升級50', 'Y', '10', '10000', '', ''],
        ['00409A', '主動復華全球50', 'Y', '10', '20000', '', ''],
        ['009824', '群益美國科技巨頭', 'Y', '10', '50000', '', ''],
        ['00997A', '主動群益美國增長', 'Y', '10', '50000', '', ''],
        ['2330', '台積電', '', '', '', 'AI穩健', '2026-09-21'],
        ['#6165', '浪凡', '', '', '', 'AI穩健', '2026-09-21'],
    ]
    worksheet = FakeWorksheet(header, rows)
    original_client = m.get_gspread_client
    m.get_gspread_client = lambda: FakeClient(worksheet)
    try:
        with redirect_stdout(io.StringIO()):
            watch = m.get_watch_list_from_sheet()
    finally:
        m.get_gspread_client = original_client

    check("讀到全部 6 列", len(watch) == 6, f"實際={len(watch)}")
    check(
        "有關閉 gspread 的數值轉換",
        worksheet.received_kwargs == {"numericise_ignore": ["all"]},
        f"實際傳入={worksheet.received_kwargs}",
    )

    by_name = {row['name']: row for row in watch}
    check(
        "009824 群益美國科技巨頭 的代號保留前導零",
        by_name.get('群益美國科技巨頭', {}).get('sid') == '009824',
        str(by_name.get('群益美國科技巨頭')),
    )
    for name, sid in (('主動統一升級50', '00403A'), ('主動復華全球50', '00409A'),
                      ('主動群益美國增長', '00997A'), ('台積電', '2330')):
        check(f"{sid} {name} 代號正確", by_name.get(name, {}).get('sid') == sid, str(by_name.get(name)))

    check("四檔庫存都標記為 is_hold",
          sum(1 for r in watch if r['is_hold']) == 4,
          f"實際={sum(1 for r in watch if r['is_hold'])}")
    check("平均成本以字串傳入仍解析為數字",
          by_name.get('群益美國科技巨頭', {}).get('cost') == 10.0,
          str(by_name.get('群益美國科技巨頭', {}).get('cost')))
    check("觀察股沒填成本時以 0 計算", by_name.get('台積電', {}).get('cost') == 0.0)
    check("'#' 前綴仍會關閉該檔的 AI 分析",
          by_name.get('浪凡', {}).get('skip_ai') is True
          and by_name.get('浪凡', {}).get('sid') == '6165',
          str(by_name.get('浪凡')))

    # 儲存格真的被存成數字時，補零邏輯仍是最後一道防線
    for raw, expected in (('009824', '009824'), ('00403A', '00403A'), ('2330', '2330'),
                          ('50', '0050'), ('878', '00878')):
        check(f"normalize_stock_id({raw!r}) == {expected!r}",
              m.normalize_stock_id(raw) == expected, m.normalize_stock_id(raw))

    for raw, expected in ((None, 0.0), ('', 0.0), ('10', 10.0), (10, 10.0),
                          ('1,234.5', 1234.5), ('NT$10', 10.0), ('abc', 0.0)):
        with redirect_stdout(io.StringIO()):
            got = m.parse_cost(raw)
        check(f"parse_cost({raw!r}) == {expected}", got == expected, str(got))


class FakeCostSheet:
    """模擬 Google Sheets 的格線上限：超出現有欄數的寫入會被擋掉。"""

    def __init__(self, header, col_count):
        self.header = list(header)
        self.col_count = col_count
        self.appended = []
        self.resized_to = None
        self.formatted = []

    def _guard_columns(self, needed, what):
        if needed > self.col_count:
            raise RuntimeError(f"Range ({what}) exceeds grid limits. Max columns: {self.col_count}")

    @staticmethod
    def _range_width(range_name):
        end = range_name.split(":")[-1]
        letters = "".join(c for c in end if c.isalpha()).upper()
        return ord(letters[-1]) - ord("A") + 1 if letters else 0

    def row_values(self, index):
        row = list(self.header)
        while row and row[-1] == "":
            row.pop()
        return row

    def update(self, *args, **kwargs):
        if args:
            raise AssertionError("gspread 6 的 update() 第一個位置參數是 values，必須改用關鍵字")
        range_name = kwargs["range_name"]
        self._guard_columns(self._range_width(range_name), range_name)
        self.header = list(kwargs["values"][0])

    def update_cell(self, row, col, value):
        self._guard_columns(col, f"cell({row},{col})")
        while len(self.header) < col:
            self.header.append("")
        self.header[col - 1] = value

    def append_row(self, values, value_input_option=None):
        self._guard_columns(len(values), "append_row")
        self.appended.append(list(values))

    def resize(self, rows=None, cols=None):
        if cols:
            self.col_count = cols
            self.resized_to = cols

    def format(self, range_name, fmt):
        self._guard_columns(self._range_width(range_name), range_name)
        self.formatted.append(range_name)


class FakeCostSpreadsheet:
    def __init__(self, cost_sheet=None):
        self._cost_sheet = cost_sheet
        self.added = None

    def worksheet(self, title):
        if self._cost_sheet is None:
            raise KeyError(title)
        return self._cost_sheet

    def add_worksheet(self, title, rows, cols):
        self.added = {"title": title, "rows": rows, "cols": cols}
        self._cost_sheet = FakeCostSheet([], col_count=cols)
        return self._cost_sheet


LEGACY_COST_HEADERS = [
    '執行時間', 'AI 呼叫總次數', '輸入 Token (Prompt)',
    '輸出 Token (Completion)', '總 Token 消耗', '預估台幣費用 (TWD)',
]


def verify_cost_sheet_logging() -> None:
    hr("7. Token與費用統計 分頁寫入")

    check("標題列常數包含『AI 提供者』",
          m.COST_SHEET_HEADERS[:6] == LEGACY_COST_HEADERS
          and m.COST_SHEET_HEADERS[6] == 'AI 提供者',
          str(m.COST_SHEET_HEADERS))

    # 舊分頁是舊版 add_worksheet(cols=6) 建的，只有 6 欄
    sheet = FakeCostSheet(LEGACY_COST_HEADERS, col_count=6)
    check("前提：6 欄的分頁寫第 7 欄會被格線上限擋下（本次 bug 的成因）",
          _raises_grid_limit(lambda: sheet.update_cell(1, 7, 'AI 提供者')))

    spreadsheet = FakeCostSpreadsheet(sheet)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        m.log_execution_cost_to_sheets(spreadsheet, "2026-09-21 18:05", 0.0)
    log = buffer.getvalue()

    check("會先把舊分頁擴欄再寫入", sheet.resized_to == 7, f"resized_to={sheet.resized_to}")
    check("標題列補上『AI 提供者』", sheet.header == m.COST_SHEET_HEADERS, str(sheet.header))
    check("成功寫入 1 筆成本紀錄", len(sheet.appended) == 1, f"appended={sheet.appended}")
    if sheet.appended:
        row = sheet.appended[0]
        check("成本紀錄有 7 個欄位", len(row) == 7, str(row))
        check("第一欄是執行時間", row[0] == "2026-09-21 18:05", str(row[0]))
        check("最後一欄是 AI 提供者標籤", row[6] == m.get_ai_provider_label(), str(row[6]))
    check("寫入成功會印出確認訊息", "✅" in log and m.COST_SHEET_TITLE in log, log.strip())

    # 已經是 7 欄且標題正確時不該重複擴欄或改標題
    ready = FakeCostSheet(m.COST_SHEET_HEADERS, col_count=7)
    with redirect_stdout(io.StringIO()):
        m.log_execution_cost_to_sheets(FakeCostSpreadsheet(ready), "2026-09-21 18:10", 1.5)
    check("標題已正確時不重複擴欄", ready.resized_to is None, f"resized_to={ready.resized_to}")
    check("標題已正確時仍寫入紀錄", len(ready.appended) == 1, f"appended={ready.appended}")

    # 分頁不存在時要能自己建立，且一次就開滿欄數
    fresh = FakeCostSpreadsheet(None)
    with redirect_stdout(io.StringIO()):
        m.log_execution_cost_to_sheets(fresh, "2026-09-21 18:15", 0.0)
    check("分頁不存在時會自動建立並開滿 7 欄",
          fresh.added == {"title": m.COST_SHEET_TITLE, "rows": 1000, "cols": 7},
          str(fresh.added))
    check("新建分頁會寫入標題列與紀錄",
          fresh._cost_sheet.appended[0] == m.COST_SHEET_HEADERS
          and len(fresh._cost_sheet.appended) == 2,
          str(fresh._cost_sheet.appended))

    # 失敗不能再被 except: pass 吞掉
    broken = FakeCostSheet(m.COST_SHEET_HEADERS, col_count=7)

    def blow_up(*args, **kwargs):
        raise RuntimeError("Quota exceeded for quota metric 'Write requests'")

    broken.append_row = blow_up
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        m.log_execution_cost_to_sheets(FakeCostSpreadsheet(broken), "2026-09-21 18:20", 0.0)
    log = buffer.getvalue()
    check("寫入失敗會印出原因（原本是 except: pass 靜默吞掉）",
          "⚠️" in log and "Quota exceeded" in log, log.strip())
    check("寫入失敗不會讓整個推播階段中斷", True)


def _raises_grid_limit(fn):
    try:
        fn()
        return False
    except RuntimeError as exc:
        return "exceeds grid limits" in str(exc)


def main() -> int:
    verify_watch_list_etfs()
    verify_thresholds()
    verify_skip_logging()
    verify_none_safe_consumers()
    verify_stage_routing()
    verify_watch_list_reading()
    verify_cost_sheet_logging()

    hr("驗證結果")
    print(f"PASS: {PASS}    FAIL: {FAIL}")
    if FAIL:
        print("❌ 有項目未通過，請先修正再 push。")
        return 1
    print("✅ 全部通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
