# ==========================================
# 每日台股自動化 pipeline
#
# 本檔案整併了原本的 DailyStockBot.py 與 DailyStockPush.py。兩者是同一條
# pipeline 的前後兩階段，靠 Google Sheets 的 WATCH_LIST 串接：
#
#   階段 scan：全市場掃描 → 寫入「法人精選監測」與 WATCH_LIST
#   階段 push：讀回 WATCH_LIST → AI 戰略報告 → 寫「全能金流診斷報表」+ 寄信 + LINE
#
# 用法：
#   python DailyStockBot.py                  # 預設 full：掃描 + 推播
#   python DailyStockBot.py --stage scan     # 只跑全市場掃描
#   python DailyStockBot.py --stage push     # 只跑 AI 戰略推播
#   STAGE=push python DailyStockBot.py       # 也可用環境變數指定
# ==========================================
import argparse
import datetime
import json
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
import pandas as pd
import requests
import yfinance as yf
from FinMind.data import DataLoader
from google import genai
from oauth2client.service_account import ServiceAccountCredentials

# ==========================================
# 0. 靜音設定與全域變數
# ==========================================
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# stage -> (是否執行全市場掃描, 是否執行 AI 戰略推播)
STAGES = {
    "full": (True, True),
    "scan": (True, False),
    "push": (False, True),
}

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID") or "U2e9b79c2f71cb2a3db62e5d75254270c"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CURSOR_API_KEY = os.getenv("CURSOR_API_KEY")
AI_PROVIDER = os.getenv("AI_PROVIDER", "").lower().strip()

# Email 設定
MAIL_RECEIVERS = ['bb00lin@gmail.com']
MAIL_USER = os.environ.get('MAIL_USERNAME')
MAIL_PASS = os.environ.get('MAIL_PASSWORD')

# ==========================================
# 修正後的正確模型清單
# ==========================================
MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
CURSOR_MODEL = "composer-2.5"
CURSOR_API_BASE = "https://api.cursor.com/v1"
CURSOR_DASHBOARD_URL = "https://cursor.com/dashboard"
CURSOR_TERMINAL_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}

HAS_GENAI = False
AI_CLIENT = None
ACTIVE_AI_PROVIDER = None
GLOBAL_TOKEN_BILLING = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "api_calls": 0
}

# ==========================================
# 歷史資料長度防護
# 新上市標的（例如剛掛牌的主動式 ETF）歷史天數不足，原本整檔會被靜默丟棄；
# 改成「算得出來的指標照算、算不出來的欄位留空」，並把原因印在 log 上。
# ==========================================
MIN_HISTORY_DAYS = 60       # 觀察名單門檻，與掃描階段 analyze_v14 的 60 天一致
MIN_HISTORY_DAYS_HOLD = 20  # 庫存持股放寬到月線，手動填進 WATCH_LIST 的部位不該消失
NO_DATA_TEXT = "資料不足"


# ==========================================
# 1. 共用工具（原本兩支腳本各有一份）
# ==========================================
def send_line(msg):
    if not LINE_ACCESS_TOKEN: return False
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": msg}]}
    try:
        requests.post(url, headers=headers, json=payload)
        return True
    except Exception as e:
        print(f"⚠️ LINE 推播失敗: {e}")
        return False

def get_line_quota_report():
    """透過 LINE API 自動查詢本月已發送則數與剩餘免費額度"""
    if not LINE_ACCESS_TOKEN:
        return "⚠️ 未設定 LINE Token，無法查詢額度"
    headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    try:
        quota_res = requests.get("https://api.line.me/v2/bot/message/quota", headers=headers).json()
        if quota_res.get("type", "none") == "none":
            return "♾️ 目前 LINE 方案為無限制則數"
        total_limit = quota_res.get("value", 0)

        consumption_res = requests.get("https://api.line.me/v2/bot/message/quota/consumption", headers=headers).json()
        total_consumed = consumption_res.get("totalUsage", 0)

        remaining_quota = total_limit - total_consumed
        alert_tag = "🟢 安全" if remaining_quota > 50 else ("🟡 偏低" if remaining_quota > 15 else "🚨 嚴重不足")
        return (
            f"📊 ── LINE 本月額度診斷 ──\n"
            f"🔹 當月免費總量：{total_limit} 則\n"
            f"🔹 本月已發送量：{total_consumed} 則\n"
            f"🔹 目前剩餘額度：{remaining_quota} 則 [{alert_tag}]"
        )
    except:
        return "⚠️ LINE 額度查詢失敗"

def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    json_key_str = os.environ.get('GOOGLE_SHEETS_JSON')
    if not json_key_str:
        print("❌ 錯誤：找不到 GOOGLE_SHEETS_JSON 環境變數！")
        return None
    try:
        creds_dict = json.loads(json_key_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"❌ 解析金鑰或連線失敗: {e}")
        return None

# ✨ 雙保險後綴智慧對接辨識器 (防止 404 Error)
def get_tw_stock(sid):
    """智慧型代號對接器：根據代碼特徵排序優先級，並透過快速 K 線測試確保成功連線"""
    clean_id = str(sid).strip().upper()
    # 3, 4, 5, 6, 8 開頭通常是上櫃(.TWO)；其他通常是上市(.TW)
    suffixes = [".TWO", ".TW"] if clean_id.startswith(('3', '4', '5', '6', '8')) else [".TW", ".TWO"]
    for suffix in suffixes:
        target = f"{clean_id}{suffix}"
        try:
            hist = yf.Ticker(target).history(period="5d")
            if not hist.empty: return yf.Ticker(target), target
        except:
            continue
    return None, None

def get_inst_stats(sid_clean):
    """一次獲取外資投信連續買超天數，以及近20天合計買超天數，節省 API 配額"""
    try:
        dl = DataLoader()
        start = (datetime.date.today() - datetime.timedelta(days=35)).strftime('%Y-%m-%d')
        df = dl.taiwan_stock_institutional_investors(stock_id=sid_clean, start_date=start)
        if df is None or df.empty: return 0, 0, 0, 0

        def analyze_investor(name):
            d = df[df['name'] == name].sort_values('date', ascending=False).head(20)
            if d.empty: return 0, 0
            streak, buy_days = 0, 0
            for idx, (_, r) in enumerate(d.iterrows()):
                net = r['buy'] - r['sell']
                if net > 0:
                    buy_days += 1
                    if streak == idx: streak += 1
            return streak, buy_days

        fs_streak, fs_days = analyze_investor('Foreign_Investor')
        ss_streak, ss_days = analyze_investor('Investment_Trust')
        return fs_streak, ss_streak, fs_days, ss_days
    except: return 0, 0, 0, 0

_STOCK_INFO_DF = None
_STOCK_INFO_DF_LOADED = False
_STOCK_INFO_MAP = None

def get_taiwan_stock_info_df():
    """台股清單 (FinMind)；掃描與推播兩階段共用，整支程式只抓一次。"""
    global _STOCK_INFO_DF, _STOCK_INFO_DF_LOADED
    if _STOCK_INFO_DF_LOADED:
        return _STOCK_INFO_DF
    _STOCK_INFO_DF_LOADED = True

    dl = DataLoader()
    max_retries = 3
    print("📥 正在下載台股清單 (FinMind)...")
    for attempt in range(max_retries):
        try:
            df = dl.taiwan_stock_info()
            if df is not None and not df.empty:
                print("✅ 台股清單下載成功")
                _STOCK_INFO_DF = df
                return df
            print(f"⚠️ FinMind 回傳空清單 (第 {attempt+1}/{max_retries} 次)")
        except Exception as e:
            print(f"⚠️ FinMind 連線失敗 (第 {attempt+1}/{max_retries} 次): {e}")
        if attempt < max_retries - 1:
            print("⏳ 等待 5 秒後重試...")
            time.sleep(5)

    print("❌ 無法獲取台股清單。")
    return None

def get_stock_info_map():
    """{股票代號: (名稱, 產業別)}；推播階段用來補產業資料，FinMind 失效時回空字典。"""
    global _STOCK_INFO_MAP
    if _STOCK_INFO_MAP is not None:
        return _STOCK_INFO_MAP
    df = get_taiwan_stock_info_df()
    if df is None or df.empty:
        _STOCK_INFO_MAP = {}
    else:
        _STOCK_INFO_MAP = {
            str(row['stock_id']): (row['stock_name'], row['industry_category'])
            for _, row in df.iterrows()
        }
    return _STOCK_INFO_MAP

def safe_ma(close, window, offset=-1):
    """均線；資料不足就回 None，避免 NaN 汙染後續比較與格式化。"""
    if len(close) < window + abs(offset) - 1: return None
    value = close.rolling(window).mean().iloc[offset]
    return None if pd.isna(value) else round(float(value), 2)

def safe_return(close, lookback):
    """lookback 個交易日前到今天的報酬率；歷史不夠長回 None（原本 iloc[-121] 會 IndexError）。"""
    if len(close) <= lookback: return None
    base = close.iloc[-(lookback + 1)]
    if pd.isna(base) or base == 0: return None
    return (close.iloc[-1] / base) - 1

def safe_rsi(close, window=14):
    if len(close) < window + 1: return None
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window).mean().iloc[-1]
    loss = (-delta.where(delta < 0, 0)).rolling(window).mean().iloc[-1]
    if pd.isna(gain) or pd.isna(loss): return None
    if loss == 0: return 50.0
    return round(100 - (100 / (1 + (gain / loss))), 1)

def fmt_value(value):
    """AI prompt 與文字欄位用：缺值顯示為「資料不足」而不是 None。"""
    return NO_DATA_TEXT if value is None else value

def fmt_pct(value):
    return NO_DATA_TEXT if value is None else f"{value:+.1f}%"

def fmt_ratio(value):
    return NO_DATA_TEXT if value is None else f"{value:.2%}"

def sheet_value(value):
    """Google Sheets 數值欄位：缺值留空白，不要寫入 None 或 nan。"""
    return "" if value is None else value


# ==========================================
# 2. AI 引擎（Gemini / Cursor 二選一）
# ==========================================
def _resolve_ai_provider():
    provider = AI_PROVIDER
    if provider in ("gemini", "cursor"):
        return provider
    if GEMINI_API_KEY:
        return "gemini"
    if CURSOR_API_KEY:
        return "cursor"
    return None

def _check_gemini_health():
    global HAS_GENAI, AI_CLIENT, ACTIVE_AI_PROVIDER
    if not GEMINI_API_KEY:
        print("⚠️ 警告: 未設定 GEMINI_API_KEY")
        return False
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        for model_name in MODEL_CANDIDATES:
            try:
                response = client.models.generate_content(model=model_name, contents="Hi")
                if response and response.text:
                    print(f"✅ Gemini 測試成功！將使用模型: {model_name}")
                    HAS_GENAI = True
                    AI_CLIENT = client
                    ACTIVE_AI_PROVIDER = "gemini"
                    return True
            except Exception:
                continue
        print("❌ 失敗: 所有 Gemini 候選模型皆無法連線。")
        return False
    except Exception:
        return False

def _check_cursor_health():
    global HAS_GENAI, ACTIVE_AI_PROVIDER
    if not CURSOR_API_KEY:
        print("⚠️ 警告: 未設定 CURSOR_API_KEY")
        return False
    print(f"✅ Cursor API Key 已設定，將使用模型: {CURSOR_MODEL}")
    HAS_GENAI = True
    ACTIVE_AI_PROVIDER = "cursor"
    return True

def check_ai_health():
    """AI 自我診斷；只有推播階段會用到 AI，所以由 run_push() 呼叫而非 import 時執行。"""
    global HAS_GENAI, ACTIVE_AI_PROVIDER

    enable_ai_env = str(os.getenv("ENABLE_AI", "true")).lower() == "true"
    if not enable_ai_env:
        print("⏸️ [系統提示] 依據手動執行設定，本次工作不啟動 AI 服務，以節省 Token。")
        HAS_GENAI = False
        ACTIVE_AI_PROVIDER = None
        return

    provider = _resolve_ai_provider()
    if not provider:
        print("⚠️ 警告: 未指定 AI_PROVIDER，且未設定任何 AI API Key")
        HAS_GENAI = False
        ACTIVE_AI_PROVIDER = None
        return

    print(f"🤖 正在進行 AI 模型連線測試 (提供者: {provider})...")
    if provider == "cursor":
        _check_cursor_health()
    else:
        _check_gemini_health()

def call_cursor_prompt(prompt, timeout=300):
    if not CURSOR_API_KEY:
        return None
    auth = (CURSOR_API_KEY, "")
    deadline = time.time() + timeout
    try:
        create_resp = requests.post(
            f"{CURSOR_API_BASE}/agents",
            auth=auth,
            json={"prompt": {"text": prompt}, "model": {"id": CURSOR_MODEL}},
            timeout=30,
        )
        if create_resp.status_code >= 400:
            print(f"❌ Cursor API 建立失敗 ({create_resp.status_code}): {create_resp.text[:300]}")
            return None

        payload = create_resp.json()
        agent_id = payload.get("agent", {}).get("id")
        run_id = payload.get("run", {}).get("id")
        if not agent_id or not run_id:
            print("❌ Cursor API 回傳缺少 agent/run ID")
            return None

        status = "CREATING"
        run_data = {}
        while time.time() < deadline:
            run_resp = requests.get(
                f"{CURSOR_API_BASE}/agents/{agent_id}/runs/{run_id}",
                auth=auth,
                timeout=30,
            )
            if run_resp.status_code >= 400:
                print(f"❌ Cursor API 查詢失敗 ({run_resp.status_code}): {run_resp.text[:300]}")
                return None

            run_data = run_resp.json()
            status = str(run_data.get("status", "")).upper()
            if status in CURSOR_TERMINAL_STATUSES:
                break
            time.sleep(2)

        if status not in CURSOR_TERMINAL_STATUSES:
            print(f"❌ Cursor API 逾時 (run: {run_id})")
            return None
        if status != "FINISHED":
            print(f"❌ Cursor Agent 執行失敗 ({status}, run: {run_id})")
            return None

        result_text = (run_data.get("result") or "").strip()
        if not result_text:
            print("❌ Cursor API 回傳空白內容")
            return None

        GLOBAL_TOKEN_BILLING["api_calls"] += 1
        return result_text
    except requests.RequestException as e:
        print(f"❌ Cursor API 網路錯誤: {e}")
        return None

def generate_ai_content(prompt, preserve_newlines=False):
    if not HAS_GENAI:
        return None
    if ACTIVE_AI_PROVIDER == "cursor":
        return call_cursor_prompt(prompt)
    if ACTIVE_AI_PROVIDER == "gemini" and AI_CLIENT:
        for model_name in MODEL_CANDIDATES:
            try:
                response = AI_CLIENT.models.generate_content(model=model_name, contents=prompt)
                record_token_usage(response)
                if not response or not response.text:
                    continue
                text = response.text.strip()
                return text if preserve_newlines else text.replace('\n', ' ').strip()
            except Exception:
                time.sleep(1)
                continue
    return None

def record_token_usage(response):
    try:
        if response and hasattr(response, 'usage_metadata') and response.usage_metadata:
            meta = response.usage_metadata
            GLOBAL_TOKEN_BILLING["prompt_tokens"] += meta.prompt_token_count
            GLOBAL_TOKEN_BILLING["completion_tokens"] += meta.candidates_token_count
            GLOBAL_TOKEN_BILLING["total_tokens"] += meta.total_token_count
            GLOBAL_TOKEN_BILLING["api_calls"] += 1
    except: pass

def get_ai_provider_label():
    if ACTIVE_AI_PROVIDER == "cursor":
        return "Cursor API"
    if ACTIVE_AI_PROVIDER == "gemini":
        return "Gemini API"
    return "未啟用 AI"

def get_cost_display_for_sheet(twd_cost):
    if ACTIVE_AI_PROVIDER == "cursor":
        return CURSOR_DASHBOARD_URL
    return f"NT$ {twd_cost} 元"

def get_ai_cost_report_html():
    provider_label = get_ai_provider_label()
    if ACTIVE_AI_PROVIDER == "cursor":
        return (
            f"<p><b>【{provider_label} 帳單】</b><br>"
            f"- AI 呼叫總次數：<span style='color:#d9480f;'>{GLOBAL_TOKEN_BILLING['api_calls']}</span><br>"
            f"- 帳單查詢：<a href='{CURSOR_DASHBOARD_URL}'>{CURSOR_DASHBOARD_URL}</a></p>"
        )
    return (
        f"<p><b>【{provider_label} 帳單】</b><br>"
        f"- 消耗總 Tokens：<span style='color:#d9480f;'>{GLOBAL_TOKEN_BILLING['total_tokens']:,}</span><br>"
        f"- 預估台幣費用：<span style='color:#c92a2a;'><b>NT$ {calculate_twd_cost()} 元</b></span></p>"
    )

def calculate_twd_cost():
    # Gemini Flash 級距參考費率 (USD/百萬 tokens)：輸入 0.075、輸出 0.30；匯率 32.5 TWD/USD
    USD_PER_M_INPUT, USD_PER_M_OUTPUT, FX_USD_TO_TWD = 0.075, 0.30, 32.5
    usd_cost = ((GLOBAL_TOKEN_BILLING["prompt_tokens"] / 1_000_000) * USD_PER_M_INPUT) + ((GLOBAL_TOKEN_BILLING["completion_tokens"] / 1_000_000) * USD_PER_M_OUTPUT)
    return round(usd_cost * FX_USD_TO_TWD, 4)


# ==========================================
# 3. 階段 scan：全市場掃描
# ==========================================
def sync_institutional_sheet(data_list):
    """將結果寫入 '法人精選監測' 報表，具備自動擴增行數與高亮最新資料功能"""
    try:
        client = get_gspread_client()
        if not client: return None
        spreadsheet = client.open("法人精選監測")
        sheet = spreadsheet.get_worksheet(0)

        current_rows = sheet.row_count
        existing_data_rows = len(sheet.get_all_values())
        needed_rows = existing_data_rows + len(data_list)

        if needed_rows >= current_rows:
            add_rows = len(data_list) + 100
            sheet.add_rows(add_rows)
            print(f"⚡ 偵測到[法人精選監測]容量不足！已自動擴增 {add_rows} 行空間。")

        # 清除舊資料的顏色 (重置為白色)
        sheet.format(f"A2:K{max(2000, current_rows)}", {
            "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
        })
        print("🔄 已清除[法人精選監測]舊資料的高亮顏色")

        sheet.append_rows(data_list, value_input_option='USER_ENTERED')
        print(f"✅ 成功同步 {len(data_list)} 筆數據至 '法人精選監測'")

        # 高亮最新資料
        start_row = existing_data_rows + 1
        end_row = existing_data_rows + len(data_list)
        sheet.format(f"A{start_row}:K{end_row}", {
            "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.82}
        })
        print(f"💛 已將最新的第 {start_row} 到 {end_row} 行標示為高亮黃色！")
        return spreadsheet.url
    except Exception as e:
        print(f"⚠️ '法人精選監測' 同步與高亮失敗: {e}")
        return None

def update_watch_list_sheet(recommended_stocks, name_map):
    """將推薦標的匯入 'WATCH_LIST'、自動檢查容量、清除舊底色，並高亮今日最新加入的潛力股"""
    try:
        client = get_gspread_client()
        if not client: return None
        spreadsheet = client.open("WATCH_LIST")
        try: sheet = spreadsheet.worksheet("WATCH_LIST")
        except: sheet = spreadsheet.get_worksheet(0)

        all_values = sheet.get_all_values()
        existing_ids = set()
        existing_data_rows = len(all_values)
        current_rows = sheet.row_count

        print(f"🔍 正在檢查 {existing_data_rows-1} 筆現有庫存名稱...")
        for idx, row in enumerate(all_values):
            if idx == 0 or not row: continue
            sid = str(row[0]).strip()
            current_name = str(row[1]).strip() if len(row) > 1 else ""
            existing_ids.add(sid)
            if sid in name_map:
                correct_name = name_map[sid]
                if not current_name or current_name != correct_name:
                    try:
                        sheet.update_cell(idx + 1, 2, correct_name)
                        print(f"🔄 更新股票名稱 ({sid}): '{current_name}' -> '{correct_name}'")
                    except Exception as e: print(f"⚠️ 更新名稱失敗 ({sid}): {e}")

        # 全面清除 WATCH_LIST 中舊資料的底色 (A 到 G 欄)
        sheet.format(f"A2:G{max(2000, current_rows)}", {
            "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
        })
        print("🔄 已清除[WATCH_LIST]舊資料的高亮顏色")

        if recommended_stocks:
            new_rows = []
            tw_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            now_str = tw_time.strftime('%Y-%m-%d %H:%M')
            print(f"📋 準備將 {len(recommended_stocks)} 檔潛力股匯入 WATCH_LIST...")

            for stock in recommended_stocks:
                sid = str(stock['id']).strip()
                name = name_map.get(sid, stock['name'])
                reason = stock['reason']
                if sid not in existing_ids:
                    new_rows.append([sid, name, "", "", "", reason, now_str])
                    existing_ids.add(sid)

            if new_rows:
                if existing_data_rows + len(new_rows) >= current_rows:
                    add_rows = len(new_rows) + 50
                    sheet.add_rows(add_rows)
                    print(f"⚡ 偵測到[WATCH_LIST]容量不足！已自動擴增 {add_rows} 行空間。")

                sheet.append_rows(new_rows, value_input_option='RAW')
                print(f"✅ 已將 {len(new_rows)} 檔新標的加入 'WATCH_LIST'")

                # 將今天全新寫入的這幾行精準高亮 (A 到 G 欄)
                start_row = existing_data_rows + 1
                end_row = existing_data_rows + len(new_rows)
                sheet.format(f"A{start_row}:G{end_row}", {
                    "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.82}
                })
                print(f"💛 已將最新的第 {start_row} 到 {end_row} 行標示為高亮黃色！")
            else: print("ℹ️ 推薦標的已存在於 WATCH_LIST，無新增項目。")
        else: print("ℹ️ 今日無新推薦標的。")
        return spreadsheet.url
    except Exception as e:
        print(f"⚠️ 更新 WATCH_LIST 與高亮失敗: {e}")
        return None

def calculate_indicators(df):
    close = df['Close']
    low_min = df['Low'].rolling(window=9).min()
    high_max = df['High'].rolling(window=9).max()
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rsi = 100 - (100 / (1 + (gain / loss)))
    rsv = (close - low_min) / (high_max - low_min) * 100
    k = rsv.ewm(com=2).mean()
    d = k.ewm(com=2).mean()
    return rsi, k, d

def analyze_v14(sid, name):
    """核心三軌策略過濾篩選引擎"""
    try:
        # 🚀 呼叫雙保險對接引擎，徹底阻斷 404 找不到股票的錯誤
        s, full_id = get_tw_stock(sid)
        if not s:
            return None, None, None

        i = s.info
        m = i.get('grossMargins', 0) or 0
        e = i.get('trailingEps', 0) or 0
        if m < 0.10 or e <= 0: return None, None, None

        df = s.history(period="1y")
        if len(df) < MIN_HISTORY_DAYS: return None, None, None

        cp = df.iloc[-1]['Close']
        ma5 = df['Close'].rolling(5).mean().iloc[-1]
        ma20 = df['Close'].rolling(20).mean().iloc[-1]

        ma60_series = df['Close'].rolling(60).mean()
        ma60 = ma60_series.iloc[-1]
        ma60_prev = ma60_series.iloc[-2] if len(ma60_series) > 1 else ma60

        rsi_series, k_series, d_series = calculate_indicators(df)
        rsi_val = rsi_series.iloc[-1]
        k_val = k_series.iloc[-1]

        vol_today = df.iloc[-1]['Volume']
        vol_avg = df['Volume'].iloc[-11:-1].mean()
        vol_ratio = vol_today / vol_avg if vol_avg > 0 else 0

        bias_5 = ((cp - ma5) / ma5) * 100
        status_label = "✅安全"
        if bias_5 > 7 or rsi_val > 75 or k_val > 85: status_label = "⚠️過熱"

        pure_id = str(sid).strip()
        fs_streak, ss_streak, fs_days, ss_days = get_inst_stats(pure_id)

        # 1. 短線穩健策略條件
        is_stable = ((ss_streak >= 2 or fs_streak >= 3) and (vol_ratio > 1.2) and (50 <= rsi_val <= 75) and (k_val <= 80) and cp > ma60)
        # 2. 短線強勢飆股策略條件
        is_aggressive = ((ss_streak >= 1 or fs_streak >= 2) and (vol_ratio > 2.5) and (rsi_val > 60) and (cp > ma5) and cp > ma60)
        # 3. 🌊 長線主升浪飆股策略條件（20日區間法人合計吸籌 >= 12天、季線上揚，無RSI天花板）
        is_long_term_trend = (cp > ma20 and cp > ma60 and ma60 > ma60_prev and (fs_days + ss_days >= 12) and vol_ratio > 1.0)

        if ((fs_streak >= 2 or ss_streak >= 1) and cp > ma60 and vol_ratio > 1.1) or is_long_term_trend:
            type_tag = "🔍法人掃貨"
            if ss_streak >= 2: type_tag = "🌟投信認養"
            if is_long_term_trend and not (is_stable or is_aggressive): type_tag = "🚀長線飆股"

            tw_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
            sheet_data = [tw_today, pure_id, name, type_tag, fs_streak, ss_streak, round(vol_ratio, 2), status_label, round(rsi_val, 1), round(k_val, 1), cp]

            recommendation = None
            if is_stable:
                reason = f"🛡️AI穩健: {type_tag} (量{vol_ratio:.1f}x/RSI{rsi_val:.0f})"
                recommendation = {'id': pure_id, 'name': name, 'reason': reason}
            elif is_aggressive:
                reason = f"🚀AI飆股: 爆量攻擊 (量{vol_ratio:.1f}x/外{fs_streak}投{ss_streak})"
                recommendation = {'id': pure_id, 'name': name, 'reason': reason}
            elif is_long_term_trend:
                reason = f"🌊AI長線飆股: 主力大週期鎖籌碼 (量{vol_ratio:.1f}x/季線上揚)"
                recommendation = {'id': pure_id, 'name': name, 'reason': reason}

            return None, sheet_data, recommendation
    except: return None, None, None
    return None, None, None

def run_scan():
    """全市場無死角掃描：篩選標的 → 寫入法人精選監測與 WATCH_LIST → LINE 摘要"""
    stock_df = get_taiwan_stock_info_df()
    if stock_df is None:
        print("❌ 掃描階段終止：沒有台股清單無法篩選。")
        return

    name_map = dict(zip(stock_df['stock_id'], stock_df['stock_name']))

    # 🔓 全面掃描全市場 1700+ 檔標的
    targets = stock_df[stock_df['stock_id'].str.len() == 4]

    sheet_results, watch_list_candidates, seen_ids = [], [], set()
    print(f"🚀 啟動全市場【短線雙軌策略 ＋ 長線浪潮飆股】全面大掃描 (共 {len(targets)} 檔)...")

    for _, row in targets.iterrows():
        sid = row['stock_id']
        if sid in seen_ids: continue
        seen_ids.add(sid)

        # 🚀 直接傳入純股票代號，讓內部的智慧型雙保險對接器處理
        _, s_res, rec_obj = analyze_v14(sid, row['stock_name'])
        if s_res: sheet_results.append(s_res)
        if rec_obj: watch_list_candidates.append(rec_obj)
        time.sleep(0.4)

    monitor_sheet_url = "無法獲取連結"
    if sheet_results:
        real_url = sync_institutional_sheet(sheet_results)
        if real_url: monitor_sheet_url = real_url

    watch_list_url = "無法獲取連結"
    real_watch_url = update_watch_list_sheet(watch_list_candidates, name_map)
    if real_watch_url: watch_list_url = real_watch_url

    line_quota_report = get_line_quota_report()
    tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')

    msg = (f"🔍 【{tw_date} 全市場量化選股雷達掃描完成】\n\n"
           f"今日台股全市場 1700+ 檔篩選已順利結束！\n"
           f"📈 共篩選出 {len(sheet_results)} 檔符合法人多頭/長線飆股標的，並已自動過濾更新潛力股至您的雲端觀察名單。\n\n"
           f"🔗 點擊查看法人精選監測：\n{monitor_sheet_url}\n\n"
           f"📋 點擊查看最新 WATCH_LIST：\n{watch_list_url}\n\n"
           f"{line_quota_report}")

    send_line(msg)
    print("✅ 雙報表自動化控制 + 全市場雙保險長線飆股監測部署成功！")


# ==========================================
# 4. 階段 push：AI 戰略體檢與推播
# ==========================================
def sync_diagnostic_sheet(data_list):
    """將個股診斷結果寫入 '全能金流診斷報表' 主表並高亮最新資料"""
    try:
        client = get_gspread_client()
        if not client: return None
        spreadsheet = client.open("全能金流診斷報表")
        sheet = spreadsheet.get_worksheet(0)
        existing_data_rows = len(sheet.get_all_values())
        if existing_data_rows + len(data_list) >= sheet.row_count:
            sheet.add_rows(len(data_list) + 100)
        sheet.format(f"A2:V{max(2000, sheet.row_count)}", {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}})
        sheet.append_rows(data_list, value_input_option='USER_ENTERED')
        sheet.format(f"A{existing_data_rows + 1}:V{existing_data_rows + len(data_list)}", {"backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.82}})
        return spreadsheet.url
    except Exception as e:
        print(f"⚠️ '全能金流診斷報表' 同步失敗: {e}")
        return None

def log_execution_cost_to_sheets(spreadsheet, current_time, twd_cost):
    try:
        base_headers = [
            '執行時間', 'AI 呼叫總次數', '輸入 Token (Prompt)',
            '輸出 Token (Completion)', '總 Token 消耗', '預估台幣費用 (TWD)',
        ]
        try:
            cost_sheet = spreadsheet.worksheet("Token與費用統計")
        except Exception:
            cost_sheet = spreadsheet.add_worksheet(title="Token與費用統計", rows=1000, cols=7)
            cost_sheet.append_row(base_headers + ['AI 提供者'])
            cost_sheet.format(
                "A1:G1",
                {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}, "horizontalAlignment": "CENTER"},
            )
        else:
            headers = cost_sheet.row_values(1)
            if len(headers) < 6:
                cost_sheet.update("A1:F1", [base_headers])
            if len(headers) < 7 or (len(headers) >= 7 and headers[6] != 'AI 提供者'):
                cost_sheet.update_cell(1, 7, 'AI 提供者')

        provider_label = get_ai_provider_label()
        cost_display = get_cost_display_for_sheet(twd_cost)
        cost_sheet.append_row(
            [
                current_time,
                GLOBAL_TOKEN_BILLING["api_calls"],
                GLOBAL_TOKEN_BILLING["prompt_tokens"],
                GLOBAL_TOKEN_BILLING["completion_tokens"],
                GLOBAL_TOKEN_BILLING["total_tokens"],
                cost_display,
                provider_label,
            ],
            value_input_option='USER_ENTERED',
        )
    except Exception:
        pass

def get_watch_list_from_sheet():
    try:
        client = get_gspread_client()
        if not client: return []

        spreadsheet = client.open("WATCH_LIST")

        # 1. 讀取全域黑名單 (使用者手動維護)
        blacklist = []
        try:
            black_sheet = spreadsheet.worksheet("AI_Blacklist")
            blacklist = [str(val).strip() for val in black_sheet.col_values(1) if str(val).strip()]
        except:
            pass

        # 2. 讀取主清單
        try: sheet = spreadsheet.worksheet("WATCH_LIST")
        except: sheet = spreadsheet.get_worksheet(0)

        watch_data = []
        for row in sheet.get_all_records():
            raw_sid = str(row.get('股票代號', '')).strip()
            raw_name = str(row.get('股票名稱', row.get('名稱', ''))).strip()
            if not raw_sid: continue

            # 處理手動關閉 AI 標籤 '#' (兼容舊功能)
            has_hash_tag = False
            if raw_sid.startswith('#'):
                has_hash_tag = True
                raw_sid = raw_sid.replace('#', '').strip()

            sid = "00" + raw_sid if len(raw_sid) == 3 else (raw_sid.zfill(4) if len(raw_sid) < 4 else raw_sid)
            is_hold = str(row.get('我的庫存倉位', '')).strip().upper() == 'Y'
            cost = row.get('平均成本', 0)

            # 🛡️ 核心防禦：自動比對黑名單或帶有 # 號
            skip_ai = has_hash_tag or (raw_sid in blacklist) or (sid in blacklist)

            watch_data.append({
                'sid': sid,
                'name': raw_name,
                'is_hold': is_hold,
                'cost': float(cost) if cost else 0,
                'skip_ai': skip_ai
            })
        return watch_data
    except Exception as e:
        print(f"❌ 讀取 WATCH_LIST 發生錯誤: {e}")
        return []

def get_vol_status_str(ratio):
    if ratio >= 2.0: return f"🔥突破爆量({ratio:.1f}x)"
    elif ratio > 1.2: return f"📈溫和出量({ratio:.1f}x)"
    elif ratio < 0.7: return f"⚠️窒息量縮({ratio:.1f}x)"
    else: return f"☁️量平({ratio:.1f}x)"

def check_ma_status(p, ma5, ma10, ma20, ma60):
    alerts = []
    THRESHOLD = 0.015
    if ma5:
        gap = (p - ma5) / ma5
        if 0 < gap <= THRESHOLD: alerts.append(f"⚡回測5日線(剩{gap:.1%})")
        elif -THRESHOLD <= gap < 0: alerts.append(f"⚠️跌破5日線({gap:.1%})")
    if ma20:
        gap = (p - ma20) / ma20
        if 0 < gap <= THRESHOLD: alerts.append(f"🛡️回測月線(剩{gap:.1%})")
        elif -THRESHOLD <= gap < 0: alerts.append(f"☠️跌破月線({gap:.1%})")
    if ma60 and abs((p - ma60) / ma60) > 0.15:
        alerts.append("🔥乖離過大" if p > ma60 else "❄️嚴重超跌")
    return " | ".join(alerts) if alerts else ""

def check_golden_entry(df_hist):
    try:
        if len(df_hist) < 65: return False, ""
        latest, prev = df_hist.iloc[-1], df_hist.iloc[-2]
        close, ma20, ma60 = latest['Close'], df_hist['Close'].rolling(20).mean().iloc[-1], df_hist['Close'].rolling(60).mean().iloc[-1]
        if not (close > ma20 and ma20 > ma60): return False, "非多頭趨勢"
        past_4_days = df_hist.iloc[-5:-1]
        drop_days = sum(1 for i in range(len(past_4_days)) if past_4_days.iloc[i]['Close'] < past_4_days.iloc[i]['Open'] or past_4_days.iloc[i]['Close'] < past_4_days.iloc[i-1]['Close'])
        if drop_days < 2: return False, "無明顯回檔"
        if not (close > latest['Open'] and close > prev['Close']): return False, "今日未轉強"
        vol_ma5 = df_hist['Volume'].iloc[-6:-1].mean()
        if not (prev['Volume'] < vol_ma5) and latest['Volume'] < prev['Volume']: return False, "攻擊量不足"
        return True, "🔥黃金買點:量縮回後買上漲"
    except: return False, ""

def get_limit_up_potential(r):
    score = 0
    reasons = []
    ma5, ma10, ma20 = r.get('ma5'), r.get('ma10'), r.get('ma20')
    if None not in (ma5, ma10, ma20) and r['p'] > ma5 and ma5 > ma10 and ma10 > ma20: score += 30; reasons.append("🔥均線多頭發散")
    if r['ss'] > 0: score += 30; reasons.append("🏦投信點火")
    elif r['fs'] >= 3: score += 20; reasons.append("💰外資連買")
    if r['vol_r'] >= 1.8: score += 20; reasons.append("📈出量攻擊")
    if r['d1'] is not None and r['d1'] > 0.03: score += 20; reasons.append("🚀長紅棒")
    return score, " | ".join(reasons)

def get_ai_strategy(data):
    if data.get('skip_ai'): return "⏸️ 已手動關閉 AI 分析"
    if not HAS_GENAI: return "AI 服務暫停"
    # Cursor Agent 每次呼叫耗時長，個股分析改由技術指標呈現，AI 集中用於戰略總結
    if ACTIVE_AI_PROVIDER == "cursor":
        return f"📋 {data.get('hint', '持續追蹤')} | {data.get('ma_alert') or '詳見戰略總結報告'}"

    profit_info = "目前無庫存，純觀察"
    if data['is_hold']:
        roi = ((data['p'] - data['cost']) / data['cost']) * 100
        profit_info = f"🔴庫存持有中 (成本:{data['cost']} | 現價:{data['p']} | 損益:{roi:+.2f}%)"
    prompt = f"針對個股 {data['name']} ({data['id']}) 進行短線診斷。現價：{data['p']}，5日線: {fmt_value(data['ma5'])}，20日線: {fmt_value(data['ma20'])}。{profit_info}。請給出約 80 字操作建議與明確防守價。"
    result = generate_ai_content(prompt)
    return result if result else "AI 連線忙碌中"

def fetch_pro_metrics(stock_data):
    """行情數據抓取核心：算得出來的指標照算，資料不足的欄位留空並記錄原因"""
    sid, passed_name, is_hold, cost = stock_data['sid'], stock_data['name'], stock_data['is_hold'], stock_data['cost']
    label = f"{sid} {passed_name}".strip()
    stock, full_id = get_tw_stock(sid)
    if not stock:
        print(f"⚠️ 略過 {label}：yfinance 的 .TW 與 .TWO 都查不到報價")
        return None
    try:
        df_hist = stock.history(period="8mo")
        days = len(df_hist)
        min_days = MIN_HISTORY_DAYS_HOLD if is_hold else MIN_HISTORY_DAYS
        if days < min_days:
            scope = "庫存" if is_hold else "觀察"
            print(f"⚠️ 略過 {label}：8 個月內只有 {days} 個交易日，未達{scope}門檻 {min_days} 天")
            return None
        if days < MIN_HISTORY_DAYS:
            print(f"🆕 {label} 為新上市標的（僅 {days} 個交易日），季線與長天期欄位將留空")

        info = stock.info
        close = df_hist['Close']
        latest = df_hist.iloc[-1]
        curr_p, curr_vol = latest['Close'], latest['Volume']
        today_amount = (curr_vol * curr_p) / 100_000_000

        clean_rsi = safe_rsi(close)

        # 取得均線
        ma5 = safe_ma(close, 5)
        ma10 = safe_ma(close, 10)
        ma20 = safe_ma(close, 20)
        ma60 = safe_ma(close, 60)

        # 昨日均線
        ma5_prev = safe_ma(close, 5, offset=-2)
        ma20_prev = safe_ma(close, 20, offset=-2)
        ma60_prev = safe_ma(close, 60, offset=-2)

        bias_60 = ((curr_p - ma60) / ma60) * 100 if ma60 else None
        bias_20 = ((curr_p - ma20) / ma20) * 100 if ma20 else None

        ma_alert_str = check_ma_status(curr_p, ma5, ma10, ma20, ma60)
        if days < MIN_HISTORY_DAYS:
            new_listing_tag = f"🆕上市僅{days}日"
            ma_alert_str = f"{ma_alert_str} | {new_listing_tag}" if ma_alert_str else new_listing_tag
        is_golden, golden_msg = check_golden_entry(df_hist)
        raw_yield = info.get('dividendYield', 0) or 0

        vol_ma5_val = df_hist['Volume'].iloc[-6:-1].mean()
        vol_ratio = curr_vol / vol_ma5_val if vol_ma5_val > 0 else 0
        pure_id = ''.join(filter(str.isdigit, sid))

        # 籌碼引擎
        fs_streak, ss_streak, fs_days, ss_days = get_inst_stats(pure_id)

        # 🚀【引擎 A】底部主力潛伏區
        is_incubation = (bias_20 is not None and abs(bias_20) <= 3.0) and (fs_streak >= 3 or ss_streak >= 3) and (1.0 <= vol_ratio <= 1.6)

        # 🚀【引擎 B】均線初升第一根
        is_first_golden_cross = None not in (ma5, ma20, ma5_prev, ma20_prev) and (ma5_prev <= ma20_prev) and (ma5 > ma20) and (curr_p > latest['Open'])

        # 🚀【引擎 C】盤中動能即時雷達
        d1_change = safe_return(close, 1)
        is_intraday_breakout = (d1_change is not None and d1_change > 0.025) and (vol_ratio > 2.0)

        score = 5
        if (info.get('profitMargins', 0) or 0) > 0: score += 1
        if ma60 is not None and curr_p > ma60: score += 1
        if 0.02 < raw_yield < 0.12: score += 1
        if clean_rsi is not None and 45 < clean_rsi < 68: score += 1
        if fs_streak >= 2 or ss_streak >= 1: score += 1
        if is_golden or is_incubation or is_first_golden_cross: score += 3

        # 加入 yfinance 備用產業資料，防止 FinMind 失效
        map_name, industry = get_stock_info_map().get(str(sid), (sid, "其他/ETF"))
        if not industry or industry == "其他/ETF":
            industry = info.get('sector', info.get('industry', '其他/ETF'))
        final_stock_name = passed_name if passed_name else map_name
        market_label = '櫃' if '.TWO' in full_id else '市'

        vol_today_lots = int(curr_vol / 1000) if not pd.isna(curr_vol) else 0
        vol_ma5_lots = int(vol_ma5_val / 1000) if not pd.isna(vol_ma5_val) else 0

        # 長線大妖股
        is_long_term_trend = (None not in (ma20, ma60, ma60_prev) and curr_p > ma20 and curr_p > ma60 and ma60 > ma60_prev and (fs_days + ss_days >= 12) and vol_ratio > 1.0)

        res = {
            "id": f"{sid}{market_label}", "name": final_stock_name, "score": score, "rsi": clean_rsi, "industry": industry,
            "vol_r": round(vol_ratio, 1), "p": round(curr_p, 2), "yield": raw_yield, "amt_t": round(today_amount, 1),
            "d1": d1_change, "d5": safe_return(close, 5),
            "m1": safe_return(close, 20), "m6": safe_return(close, 120),
            "is_hold": is_hold, "cost": cost, "bias_str": fmt_pct(bias_60), "bias_20_str": fmt_pct(bias_20),
            "vol_str": get_vol_status_str(vol_ratio),
            "fs": fs_streak, "ss": ss_streak, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma_alert": ma_alert_str,
            "is_golden": is_golden, "golden_msg": golden_msg,
            "v_today": vol_today_lots, "v_ma5": vol_ma5_lots,
            "days": days,
            "is_long_term": is_long_term_trend,
            "is_incubation": is_incubation,
            "is_first_golden_cross": is_first_golden_cross,
            "is_intraday_breakout": is_intraday_breakout,
            "skip_ai": stock_data.get('skip_ai', False)
        }

        if (bias_60 is not None and bias_60 > 15) or (clean_rsi is not None and clean_rsi > 75): res["risk"] = "🚨高檔過熱"
        elif ma20 is not None and curr_p < ma20: res["risk"] = "⚠️破線警戒"
        else: res["risk"] = "🟢正常"

        if None not in (ma5, ma10, ma20, ma60) and ma5 > ma10 and ma10 > ma20 and ma20 > ma60: res["trend"] = "📈強勢多頭"
        elif ma60 is not None and curr_p < ma60: res["trend"] = "📉空頭修正"
        else: res["trend"] = "☁️區間震盪"

        if is_long_term_trend: res["hint"] = "🌊長線起漲"
        elif is_golden: res["hint"] = "🔥黃金買點"
        elif is_intraday_breakout: res["hint"] = "⚡動能爆發"
        elif is_first_golden_cross: res["hint"] = "✨均線突破"
        elif is_incubation: res["hint"] = "🌱主力潛伏"
        elif score >= 8: res["hint"] = "🚀強勢進攻"
        elif days < MIN_HISTORY_DAYS: res["hint"] = "🆕新上市觀察"
        else: res["hint"] = "👀持續追蹤"

        res['ai_strategy'] = get_ai_strategy(res)
        return res
    except Exception as e:
        print(f"⚠️ 略過 {label}：指標計算失敗 ({type(e).__name__}: {e})")
        return None

# ==========================================
# ✨ 全域戰略報告生成器
# ==========================================
def generate_and_save_summary(data_list, report_time_str):
    if not HAS_GENAI: return "本次報告未包含 AI 總結"

    inventory_txt, watchlist_txt = "", ""
    golden_candidates, limit_up_candidates_txt, long_term_candidates_txt = "", "", ""
    incubation_txt, first_golden_cross_txt, intraday_breakout_txt = "", "", ""

    for r in data_list:
        if r.get('skip_ai'): continue

        try:
            stock_info = (
                f"- {r['name']}({r['id']}) | 現價:{r['p']} | 分數:{r['score']} | "
                f"MA5:{fmt_value(r['ma5'])} | MA10:{fmt_value(r['ma10'])} | MA20:{fmt_value(r['ma20'])} | MA60:{fmt_value(r['ma60'])} | "
                f"日漲跌:{fmt_ratio(r['d1'])} | 外資:{r['fs']}d 投信:{r['ss']}d | "
                f"今日量:{r.get('v_today',0)}張 (量比:{r['vol_r']}x) | 訊號:{r['ma_alert']}\n"
            )
            if r['is_hold']: inventory_txt += stock_info
            else: watchlist_txt += stock_info

            if r['is_golden']: golden_candidates += f"- {r['name']}({r['id']}) [今日成交:{r.get('v_today',0)}張]: {r['golden_msg']} (防守MA20: {fmt_value(r['ma20'])})\n"

            if r.get('is_long_term'):
                long_term_candidates_txt += f"- {r['name']}({r['id']}) [今日成交:{r.get('v_today',0)}張]: 🌊主力大週期鎖籌碼 (量{r['vol_r']}x) | 防守: {fmt_value(r['ma20'])}\n"

            limit_up_score, limit_up_reason = get_limit_up_potential(r)
            if limit_up_score >= 60:
                limit_up_candidates_txt += f"- {r['name']}({r['id']}) [今日成交:{r.get('v_today',0)}張]: 潛力分{limit_up_score} ({limit_up_reason}) | 外:{r['fs']}d 投:{r['ss']}d\n"

            if r.get('is_incubation'):
                incubation_txt += f"- {r['name']}({r['id']}) [今日成交:{r.get('v_today',0)}張]: 籌碼連買(外{r['fs']}投{r['ss']}) | 乖離月線僅{r['bias_20_str']} | 量能溫和{r['vol_r']}x\n"

            if r.get('is_first_golden_cross'):
                first_golden_cross_txt += f"- {r['name']}({r['id']}) [今日成交:{r.get('v_today',0)}張]: MA5({fmt_value(r['ma5'])}) 剛穿越 MA20({fmt_value(r['ma20'])}) 第一天 | 今日漲幅:{fmt_ratio(r['d1'])}\n"

            if r.get('is_intraday_breakout'):
                intraday_breakout_txt += f"- {r['name']}({r['id']}) [今日成交:{r.get('v_today',0)}張]: ⚡動能異動！漲幅達{fmt_ratio(r['d1'])} | 量暴增{r['vol_r']}x\n"

        except Exception as e:
            print(f"⚠️ 戰略總結略過 {r.get('name')}({r.get('id')})：{type(e).__name__}: {e}")
            continue

    if not incubation_txt: incubation_txt = "今日無符合 [底部潛伏] 標準之標的。"
    if not first_golden_cross_txt: first_golden_cross_txt = "今日無符合 [黃金交叉第一根] 之標的。"
    if not intraday_breakout_txt: intraday_breakout_txt = "今日無符合 [動能爆發] 之標的。"
    if not limit_up_candidates_txt: limit_up_candidates_txt = "今日無明顯漲停特徵股。"
    if not long_term_candidates_txt: long_term_candidates_txt = "今日無符合長線主升浪標準之標的。"

    prompt = f"""
    角色：你是頂尖、冷酷、極度重視風險管理的台股短線與波段量化操盤總監。
    任務：根據今日技術數據，撰寫極度精準、具備絕對數據顆粒度(必須寫出實際價格與張數)的【戰略總結報告】。
    
    【最新市場數據庫】
    【🌱 引擎A：底部主力潛伏區 (提早1~3天卡位)】
    {incubation_txt}
    【✨ 引擎B：均線初升第一根 (MA5剛上穿MA20)】
    {first_golden_cross_txt}
    【⚡ 引擎C：動能即時爆發雷達】
    {intraday_breakout_txt}
    【🔥 今日黃金進場公式篩選】
    {golden_candidates}
    【🚀 今日漲停潛力股獵殺 (已經噴發之強勢股)】
    {limit_up_candidates_txt}
    【🌊 長線主升浪大妖股】
    {long_term_candidates_txt}
    
    【❌ 鐵律：違反直接扣薪 ❌】：
    1. 報告前段請依序精簡列出上述各大分類的標的狀態。
    2. ✨【★ 明日券商 APP 智慧單下單精確設定】：
        深度交叉比對上述所有引擎數據。
        【優先級】：AI 總監必須「優先」從【引擎A】、【引擎B】、【引擎C】與【黃金公式】中挑選 A 與 B 級標的，以達到「買在起漲點」的目的；已噴發的強勢股盡量安排在 C 級。
        你必須依據個股位階，將挑選出的標的嚴格分類為 A、B、C 三種等級，並必須維持這三個等級標題的輸出！
        
        【🚨 關鍵流動性與防漏空缺鐵律】：
        - 必須在下單設定內明確標示【今日實際成交量】。
        - 如果推薦的股票今日成交量【小於 500 張】，必須在標題一字不漏強制加上：
          "⚠️ [冷門股防範：注意此股今日成交量低於500張，流動性極差，請嚴格控管資金或改採零股少量試單！]"
        - 若某等級無符合標的，請在該等級標題下方強制輸出一行宣示文字：「今日無符合 [該等級名稱] 之推薦標的，嚴格控管資金風險。」
        
    ==========【等級 A 專屬模板 (底部潛伏/黃金交叉)】==========
    🎯 獵殺目標：[股票名稱] (代號) - ✨ 特選：低位階尚未起飛股 [今日成交: XX張] 
    - 📊 進場邏輯深度解析：
      【流動性檢視】：今日成交量為 [張數]張 (對比5日均量 [張數]張)。
      1. 【提早卡位】：符合引擎A或B，主力剛開始吸籌或均線剛交叉。
      2. 【位階安全防禦】：股價距離月線極近，防守容易。
    - 實戰設定步驟：
      1. 觸發條件設定：當股價小於或等於 [MA5 + 0.1] 時。
      2. 下單動作設定：以「限價 [MA5]」買入。
      3. 終極安全帶（停損設定）：收盤跌破 MA20: [MA20] 立刻砍出。
      
    ==========【等級 B 專屬模板 (動能爆發/回測買點)】==========
    🎯 獵殺目標：[股票名稱] (代號) - ⚡ 衝刺：初升段爆發/量縮回測股 [今日成交: XX張] 
    - 📊 進場邏輯深度解析：
      【流動性檢視】：今日成交量放大至 [張數]張。
      1. 【動能確認】：符合引擎C 或 黃金公式，有明確攻擊量或完美的量縮回測。
    - 實戰設定步驟：(同上，以MA5買進，跌破MA20停損)

    ==========【等級 C 專屬模板 (長線/強勢追擊)】==========
    🎯 獵殺目標：[股票名稱] (代號) - 🌊 破浪：長線主升浪大妖股 [今日成交: XX張] 
    - 📊 進場邏輯深度解析：
      【流動性檢視】：今日成交量為 [張數]張。
      1. 【大人鎖碼護航】：過去20日法人強勢吸籌，季線向上發散，無視短線指標過熱。
    - 實戰設定步驟：(強勢股不輕易拉回，逢 MA5 或 MA10 買進，跌破 MA20 停損)

    請嚴格依照以上章節輸出（繁體中文），並保留所有特殊符號。
    """

    result = generate_ai_content(prompt, preserve_newlines=True)
    return result if result else "AI 生成總結報告失敗"

def send_email(subject, body):
    if not MAIL_USER or not MAIL_PASS: return
    msg = MIMEMultipart(); msg['From'] = MAIL_USER; msg['To'] = ", ".join(MAIL_RECEIVERS); msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587); server.starttls(); server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg); server.quit()
        print("✅ 郵件發送成功")
    except: print("❌ 郵件失敗")

def run_push():
    """讀 WATCH_LIST → 個股診斷 → AI 戰略報告 → 寫報表 + 寄信 + LINE 推播"""
    check_ai_health()

    current_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
    watch_data_list = get_watch_list_from_sheet()
    if not watch_data_list: return
    print(f"📋 WATCH_LIST 共 {len(watch_data_list)} 檔待分析（庫存 {sum(1 for s in watch_data_list if s['is_hold'])} 檔）")

    results_line, results_sheet = [], []
    for idx, stock_data in enumerate(watch_data_list):
        res = fetch_pro_metrics(stock_data)
        if res:
            results_line.append(res)
            results_sheet.append([current_time, res['id'], res['name'], "📦庫存" if res['is_hold'] else "👀觀察", res['score'], sheet_value(res['rsi']), res['industry'], res['bias_str'], res['vol_str'], res['fs'], res['ss'], res['p'], res['yield'], res['amt_t'], sheet_value(res['d1']), sheet_value(res['d5']), sheet_value(res['m1']), sheet_value(res['m6']), res['risk'], res['trend'], res['hint'], res['ai_strategy']])
        if idx < len(watch_data_list) - 1: time.sleep(2.0)

    skipped = len(watch_data_list) - len(results_line)
    print(f"📊 成功納入報表 {len(results_line)} 檔"
          + (f"，略過 {skipped} 檔（原因見上方 ⚠️ 訊息）" if skipped else ""))

    if results_line:
        time.sleep(10)
        summary_text = generate_and_save_summary(results_line, current_time)

        report_sheet_url = sync_diagnostic_sheet(results_sheet)
        if not report_sheet_url:
            report_sheet_url = "無法動態獲取連結，請至 Google Drive 查閱"

        twd_cost = calculate_twd_cost()
        line_quota_report = get_line_quota_report()
        provider_label = get_ai_provider_label()

        print("\n==========================================")
        print("💰 本次代碼工作 AI 運作成本結算報告")
        print(f"🔹 執行時間：{current_time}")
        print(f"🔹 AI 提供者：{provider_label}")
        print(f"🔹 AI API 呼叫總次數：{GLOBAL_TOKEN_BILLING['api_calls']} 次")
        if ACTIVE_AI_PROVIDER == "cursor":
            print(f"🔹 帳單查詢：{CURSOR_DASHBOARD_URL}")
        else:
            print(f"🔹 總消耗 Tokens：{GLOBAL_TOKEN_BILLING['total_tokens']:,}")
            print(f"🔹 預估本次花費台幣：NT$ {twd_cost} 元")
        print("==========================================\n")

        try:
            client = get_gspread_client()
            if client:
                spreadsheet = client.open("全能金流診斷報表")
                log_execution_cost_to_sheets(spreadsheet, current_time, twd_cost)

                try: s_sheet = spreadsheet.worksheet(current_time); s_sheet.clear()
                except: s_sheet = spreadsheet.add_worksheet(title=current_time, rows=150, cols=10)

                lines_list = [[line] for line in summary_text.split('\n')]
                s_sheet.update(values=lines_list, range_name='A1')

                body_requests = []
                for row_idx in range(1, len(lines_list) + 1):
                    body_requests.append({"mergeCells": {"range": {"sheetId": s_sheet.id, "startRowIndex": row_idx - 1, "endRowIndex": row_idx, "startColumnIndex": 0, "endColumnIndex": 5}, "mergeType": "MERGE_ROWS"}})
                body_requests.append({"updateDimensionProperties": {"range": {"sheetId": s_sheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 5}, "properties": {"pixelSize": 140}, "fields": "pixelSize"}})

                if body_requests: spreadsheet.batch_update({"requests": body_requests})
                s_sheet.format("A1:E150", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP", "textFormat": {"fontSize": 10, "fontFamily": "Microsoft JhengHei"}})
        except Exception as e: print(f"⚠️ 建立圖2排版戰略分頁失敗: {e}")

        line_quota_html = line_quota_report.replace('\n', '<br>')
        cost_report_html = f"<div style='background-color:#fff9db; padding:15px; border-left:5px solid #fcc419; margin-top:20px; font-family:sans-serif;'><h3 style='margin-top:0; color:#e67e22;'>💰 今日運作成本診斷報告</h3><p><b>【雲端主報表連結】</b><br>- 🔗 <a href='{report_sheet_url}'>點擊前往查看數據報表</a></p>{get_ai_cost_report_html()}<p style='margin-bottom:0;'><b>【LINE Bot 免費額度】</b><br>{line_quota_html}</p></div>"

        email_body = f"<html><body><h2>📊 {current_time} 提前攔截戰略報告</h2><pre style='font-family:sans-serif; white-space:pre-wrap;'>{summary_text}</pre><hr>{cost_report_html}</body></html>"
        send_email(f"[{current_time}] 台股 AI 初升段戰報 (附成本與 LINE 額度)", email_body)

        if ACTIVE_AI_PROVIDER == "cursor":
            ai_bill_text = (
                f"── 💸 今日 AI 帳單明細 ──\n"
                f"🔹 AI 提供者：{provider_label}\n"
                f"🔹 AI 呼叫次數：{GLOBAL_TOKEN_BILLING['api_calls']}\n"
                f"💰 帳單查詢：{CURSOR_DASHBOARD_URL}"
            )
        else:
            ai_bill_text = f"── 💸 今日 AI 帳單明細 ──\n🔹 AI 提供者：{provider_label}\n🔹 總消耗 Tokens：{GLOBAL_TOKEN_BILLING['total_tokens']:,}\n💰 今日預估費用：NT$ {twd_cost} 元"
        line_msg = f"📊 【{current_time} 戰略報告已更新】\n\n全新【提前攔截初升段】引擎已發動！AI 總監已為您優先從底部潛伏與剛突破的標的中進行精選。\n\n🔗 點擊直達雲端主報表：\n{report_sheet_url}\n\n{ai_bill_text}\n\n{line_quota_report}"
        if send_line(line_msg):
            print("✅ 終極完全體【初升段攔截雷達】已全面部署成功！")


# ==========================================
# 5. 主程式執行區塊
# ==========================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="每日台股自動化 pipeline（全市場掃描 + AI 戰略推播）")
    parser.add_argument(
        "--stage",
        choices=sorted(STAGES),
        default=None,
        help="full = 掃描+推播（預設）/ scan = 僅全市場掃描 / push = 僅 AI 戰略推播",
    )
    args = parser.parse_args(argv)
    if args.stage is None:
        # 未指定時改讀 STAGE 環境變數；排程觸發時為空字串，視為使用預設值
        env_stage = os.getenv("STAGE", "").strip().lower()
        if env_stage and env_stage not in STAGES:
            parser.error(f"環境變數 STAGE 的值無效：{env_stage}（可用：{', '.join(sorted(STAGES))}）")
        args.stage = env_stage or "full"
    return args

def main(argv=None):
    args = parse_args(argv)
    do_scan, do_push = STAGES[args.stage]
    print(f"🚀 執行階段 {args.stage}：全市場掃描={do_scan} / AI 戰略推播={do_push}")

    if do_scan:
        run_scan()
    if do_push:
        run_push()

if __name__ == "__main__":
    main()
