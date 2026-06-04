import os, yfinance as yf, pandas as pd, requests, time, datetime, sys
import gspread
import logging
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from oauth2client.service_account import ServiceAccountCredentials
from FinMind.data import DataLoader

# ==========================================
# 0. 靜音設定與全域變數
# ==========================================
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = "U2e9b79c2f71cb2a3db62e5d75254270c"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Email 設定
MAIL_RECEIVERS = ['bb00lin@gmail.com']
MAIL_USER = os.environ.get('MAIL_USERNAME')
MAIL_PASS = os.environ.get('MAIL_PASSWORD')

# ==========================================
# 修正後的正確模型清單 (完美對應 google-genai SDK 規範)
# ==========================================
MODEL_CANDIDATES = [
    "gemini-2.5-flash",      # 🚀 目前最新、速度最快的首選模型
    "gemini-2.0-flash",      # 修正：部分環境需寫成完整代號（如 "gemini-2.0-flash-exp" 或由新版自動對接）
    "gemini-1.5-flash",
]

# 全域變數
HAS_GENAI = False
AI_CLIENT = None

# 全局 Token 統計記帳本（統計單次代碼工作的所有累積消耗）
GLOBAL_TOKEN_BILLING = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "api_calls": 0
}

# ==========================================
# [啟動檢查] AI 自我診斷（已加入詳細錯誤排查）
# ==========================================
def check_ai_health():
    global HAS_GENAI, AI_CLIENT
    print("🤖 正在進行 AI 模型連線測試 (使用 google-genai SDK)...")
    if not GEMINI_API_KEY:
        print("⚠️ 警告: 未設定 GEMINI_API_KEY 環境變數，將跳過 AI 功能。")
        HAS_GENAI = False
        return

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        for model_name in MODEL_CANDIDATES:
            try:
                response = client.models.generate_content(model=model_name, contents="Hi")
                if response and response.text:
                    print(f"✅ AI 測試成功！本次代碼工作將使用模型: {model_name}")
                    HAS_GENAI = True
                    AI_CLIENT = client
                    return
            except Exception as model_err: 
                print(f"❌ 模型 {model_name} 測試失敗，原因: {model_err}")
                continue
        print("❌ 失敗: 所有候選模型皆無法連線。將以「無 AI 模式」繼續執行。")
        HAS_GENAI = False
    except Exception as e:
        print(f"❌ AI 初始化錯誤: {e}")
        HAS_GENAI = False

check_ai_health()

# ==========================================
# LINE 官方帳號免費發送額度查詢
# ==========================================
def get_line_quota_report():
    if not LINE_ACCESS_TOKEN:
        return "⚠️ 未設定 LINE Token，無法查詢額度"
    headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    try:
        quota_url = "https://api.line.me/v2/bot/message/quota"
        quota_res = requests.get(quota_url, headers=headers).json()
        value_type = quota_res.get("type", "none")
        if value_type == "none":
            return "♾️ 目前 LINE 方案為無限制則數"
        total_limit = quota_res.get("value", 0)
        
        consumption_url = "https://api.line.me/v2/bot/message/quota/consumption"
        consumption_res = requests.get(consumption_url, headers=headers).json()
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

# ==========================================
# Token 費用換算邏輯
# ==========================================
def calculate_twd_cost():
    USD_PER_M_INPUT = 0.075   
    USD_PER_M_OUTPUT = 0.30   
    FX_USD_TO_TWD = 32.5      
    p_tokens = GLOBAL_TOKEN_BILLING["prompt_tokens"]
    c_tokens = GLOBAL_TOKEN_BILLING["completion_tokens"]
    usd_cost = ((p_tokens / 1_000_000) * USD_PER_M_INPUT) + ((c_tokens / 1_000_000) * USD_PER_M_OUTPUT)
    return round(usd_cost * FX_USD_TO_TWD, 4)

def record_token_usage(response):
    try:
        if response and hasattr(response, 'usage_metadata') and response.usage_metadata:
            meta = response.usage_metadata
            GLOBAL_TOKEN_BILLING["prompt_tokens"] += meta.prompt_token_count
            GLOBAL_TOKEN_BILLING["completion_tokens"] += meta.candidates_token_count
            GLOBAL_TOKEN_BILLING["total_tokens"] += meta.total_token_count
            GLOBAL_TOKEN_BILLING["api_calls"] += 1
    except: pass

# ==========================================
# Google Sheets 核心資料庫 (防爆、去色與高亮)
# ==========================================
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    json_key_str = os.environ.get('GOOGLE_SHEETS_JSON')
    if not json_key_str: return None
    try:
        creds_dict = json.loads(json_key_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except: return None

def sync_to_sheets(data_list):
    try:
        client = get_gspread_client()
        if not client: return None
        spreadsheet = client.open("全能金流診斷報表")
        sheet = spreadsheet.get_worksheet(0)
        
        current_rows = sheet.row_count       
        existing_data_rows = len(sheet.get_all_values())  
        needed_rows = existing_data_rows + len(data_list)
        
        if needed_rows >= current_rows:
            add_rows = len(data_list) + 100  
            sheet.add_rows(add_rows)
            print(f"⚡ 偵測到[全能金流診斷報表]容量不足！已自動擴增 {add_rows} 行空間。")

        sheet.format(f"A2:V{max(2000, current_rows)}", {
            "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
        })
        print("🔄 已清除主報表舊資料的高亮顏色")

        sheet.append_rows(data_list, value_input_option='USER_ENTERED')
        print(f"✅ 成功同步 {len(data_list)} 筆數據至主報表")

        start_row = existing_data_rows + 1
        end_row = existing_data_rows + len(data_list)
        
        sheet.format(f"A{start_row}:V{end_row}", {
            "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.82}
        })
        print(f"💛 已將最新的第 {start_row} 到 {end_row} 行標示為高亮黃色！")
        
        return spreadsheet.url  
    except Exception as e: 
        print(f"⚠️ 主報表同步與高亮修正失敗: {e}")
        return None

def log_execution_cost_to_sheets(spreadsheet, current_time, twd_cost):
    try:
        try:
            cost_sheet = spreadsheet.worksheet("Token與費用統計")
        except:
            cost_sheet = spreadsheet.add_worksheet(title="Token與費用統計", rows=1000, cols=6)
            cost_sheet.append_row(['執行時間', 'AI 呼叫總次數', '輸入 Token (Prompt)', '輸出 Token (Completion)', '總 Token 消耗', '預估台幣費用 (TWD)'])
            cost_sheet.format("A1:F1", {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                "horizontalAlignment": "CENTER"
            })
        
        row_data = [
            current_time,
            GLOBAL_TOKEN_BILLING["api_calls"],
            GLOBAL_TOKEN_BILLING["prompt_tokens"],
            GLOBAL_TOKEN_BILLING["completion_tokens"],
            GLOBAL_TOKEN_BILLING["total_tokens"],
            f"NT$ {twd_cost} 元"
        ]
        cost_sheet.append_row(row_data, value_input_option='USER_ENTERED')
        print("📊 本次工作總計花費已成功記錄至試算表 [Token與費用統計] 獨立分頁！")
    except Exception as e:
        print(f"⚠️ 記錄工作成本至試算表失敗: {e}")

# ==========================================
# 🚀 產業字典：加入 3 次重試機制，防範 FinMind 斷線
# ==========================================
def get_global_stock_info():
    """帶有 3 次強力重試機制的產業字典抓取器，避免 FinMind 罷工導致產業變空白"""
    dl = DataLoader()
    max_retries = 3
    for attempt in range(max_retries):
        try:
            df = dl.taiwan_stock_info()
            if df is not None and not df.empty:
                return {str(row['stock_id']): (row['stock_name'], row['industry_category']) for _, row in df.iterrows()}
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"⏳ 產業字典獲取失敗，2秒後重試... ({attempt+1}/{max_retries})")
                time.sleep(2)
            else:
                print("⚠️ 無法獲取產業字典，將暫時使用預設值。")
    return {}

STOCK_INFO_MAP = get_global_stock_info()

# ==========================================
# 🚀 讀取名單：直接從 Google Sheets 抓取真實中文名稱
# ==========================================
def get_watch_list_from_sheet():
    try:
        client = get_gspread_client()
        if not client: return []
        try: sheet = client.open("WATCH_LIST").worksheet("WATCH_LIST")
        except: sheet = client.open("WATCH_LIST").get_worksheet(0)
        records = sheet.get_all_records()
        watch_data = []
        for row in records:
            raw_sid = str(row.get('股票代號', '')).strip()
            
            # ✨ 關鍵修改：優先從試算表直接抓名字，防範 API 抓不到
            raw_name = str(row.get('股票名稱', row.get('名稱', ''))).strip()
            
            if not raw_sid: continue
            if raw_sid.isdigit():
                sid = "00" + raw_sid if len(raw_sid) == 3 else (raw_sid.zfill(4) if len(raw_sid) < 4 else raw_sid)
            else: sid = raw_sid
            
            is_hold = str(row.get('我的庫存倉位', '')).strip().upper() == 'Y'
            cost = row.get('平均成本', 0)
            if cost == '': cost = 0
            
            # ✨ 將抓到的名字包裝進去傳遞給後續流程
            watch_data.append({'sid': sid, 'name': raw_name, 'is_hold': is_hold, 'cost': float(cost)})
        return watch_data
    except: return []

# ==========================================
# 輔助數據運算 (技術指標與籌碼)
# ==========================================
def get_streak_only(sid_clean):
    try:
        dl = DataLoader()
        start = (datetime.date.today() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        clean_id = ''.join(filter(str.isdigit, str(sid_clean)))
        df = dl.taiwan_stock_institutional_investors(stock_id=clean_id, start_date=start)
        if df is None or df.empty: return 0, 0
        def count_s(name):
            d = df[df['name'] == name].sort_values('date', ascending=False)
            c = 0
            for _, r in d.iterrows():
                if (r['buy'] - r['sell']) > 0: c += 1
                else: break
            return c
        return count_s('Foreign_Investor'), count_s('Investment_Trust')
    except: return 0, 0

def get_vol_status_str(ratio):
    if ratio > 1.8: return f"🔥爆量({ratio:.1f}x)"
    elif ratio > 1.2: return f"📈溫和({ratio:.1f}x)"
    elif ratio < 0.7: return f"⚠️縮量({ratio:.1f}x)"
    else: return f"☁️量平({ratio:.1f}x)"

def check_ma_status(p, ma5, ma10, ma20, ma60):
    alerts = []
    THRESHOLD = 0.015 
    if ma5 > 0:
        gap_ma5 = (p - ma5) / ma5
        if 0 < gap_ma5 <= THRESHOLD: alerts.append(f"⚡回測5日線(剩{gap_ma5:.1%})")
        elif -THRESHOLD <= gap_ma5 < 0: alerts.append(f"⚠️跌破5日線({gap_ma5:.1%})")
    if ma20 > 0:
        gap_ma20 = (p - ma20) / ma20
        if 0 < gap_ma20 <= THRESHOLD: alerts.append(f"🛡️回測月線(剩{gap_ma20:.1%})")
        elif -THRESHOLD <= gap_ma20 < 0: alerts.append(f"☠️跌破月線({gap_ma20:.1%})")
    if ma60 > 0:
        gap_ma60 = (p - ma60) / ma60
        if abs(gap_ma60) > 0.15: 
            alerts.append("🔥乖離過大" if gap_ma60 > 0 else "❄️嚴重超跌")
    return " | ".join(alerts) if alerts else ""

def check_golden_entry(df_hist):
    try:
        if len(df_hist) < 65: return False, ""
        latest, prev = df_hist.iloc[-1], df_hist.iloc[-2]
        close = latest['Close']
        ma20 = df_hist['Close'].rolling(20).mean().iloc[-1]
        ma60 = df_hist['Close'].rolling(60).mean().iloc[-1]
        if not (close > ma20 and ma20 > ma60): return False, "非多頭趨勢"
        past_4_days = df_hist.iloc[-5:-1]
        drop_days = 0
        for i in range(len(past_4_days)):
            if past_4_days.iloc[i]['Close'] < past_4_days.iloc[i]['Open'] or past_4_days.iloc[i]['Close'] < past_4_days.iloc[i-1]['Close']: drop_days += 1
        if drop_days < 2: return False, "無明顯回檔"
        if not (close > latest['Open'] and close > prev['Close']): return False, "今日未轉強"
        vol_ma5 = df_hist['Volume'].iloc[-6:-1].mean()
        if not (prev['Volume'] < vol_ma5) and latest['Volume'] < prev['Volume']: return False, "攻擊量不足"
        return True, "🔥黃金買點:量縮回後買上漲"
    except: return False, ""

def get_limit_up_potential(r):
    score = 0
    reasons = []
    if r['p'] > r['ma5'] and r['ma5'] > r['ma10'] and r['ma10'] > r['ma20']:
        score += 30; reasons.append("🔥均線多頭發散")
    if r['ss'] > 0: score += 30; reasons.append("🏦投信點火")
    elif r['fs'] >= 3: score += 20; reasons.append("💰外資連買")
    if r['vol_r'] >= 1.8: score += 20; reasons.append("📈出量攻擊")
    if r['d1'] > 0.03: score += 20; reasons.append("🚀長紅棒")
    return score, " | ".join(reasons)

# ==========================================
# 4. AI 策略層 (個股診斷)
# ==========================================
def get_gemini_strategy(data):
    if not HAS_GENAI or not AI_CLIENT: return "AI 服務暫停"
    profit_info = "目前無庫存，純觀察"
    if data['is_hold']:
        roi = ((data['p'] - data['cost']) / data['cost']) * 100
        profit_info = f"🔴庫存持有中 (成本:{data['cost']} | 現價:{data['p']} | 損益:{roi:+.2f}%)"

    prompt = f"針對個股 {data['name']} ({data['id']}) 進行短線診斷。現價：{data['p']}，5日線: {data['ma5']}，20日線: {data['ma20']}。{profit_info}。請給出約 80 字操作建議與明確防守價。"

    for model_name in MODEL_CANDIDATES:
        try:
            response = AI_CLIENT.models.generate_content(model=model_name, contents=prompt)
            record_token_usage(response)  
            return response.text.replace('\n', ' ').strip()
        except:
            time.sleep(1)
            continue
    return "AI 連線忙碌中"

# ==========================================
# 5. ✨ 全域戰略報告生成器 (自動三階分級 + 絕對數據顆粒度)
# ==========================================
def generate_and_save_summary(data_list, report_time_str):
    if not HAS_GENAI or not AI_CLIENT: return "本次報告未包含 AI 總結"
    
    inventory_txt = ""
    watchlist_txt = ""
    golden_candidates = ""
    limit_up_candidates_txt = ""
    
    for r in data_list:
        try:
            stock_info = (
                f"- {r['name']}({r['id']}) | 現價:{r['p']} | 分數:{r['score']} | "
                f"MA5:{r['ma5']} | MA10:{r['ma10']} | MA20:{r['ma20']} | MA60:{r['ma60']} | "
                f"日漲跌:{r['d1']:.2%} | 外資:{r['fs']}d 投信:{r['ss']}d | "
                f"今日成交量:{r.get('v_today',0)}張 (5日均量:{r.get('v_ma5',0)}張, {r['vol_str']}) | "
                f"均線訊號:{r['ma_alert']} | AI個股策略:{r['ai_strategy'][:40]}...\n"
            )
            if r['is_hold']: inventory_txt += stock_info
            else: watchlist_txt += stock_info
                
            if r['is_golden']:
                golden_candidates += f"- {r['name']}({r['id']}): {r['golden_msg']} (防守MA20: {r['ma20']})\n"

            limit_up_score, limit_up_reason = get_limit_up_potential(r)
            if limit_up_score >= 60:
                limit_up_candidates_txt += (
                    f"- {r['name']}({r['id']}): 潛力分{limit_up_score} ({limit_up_reason}) | "
                    f"籌碼:投信{r['ss']}天 外資{r['fs']}天\n"
                )
        except: continue

    if not golden_candidates: golden_candidates = "今日無符合標準之標的。"
    if not limit_up_candidates_txt: limit_up_candidates_txt = "今日無明顯漲停特徵股。"

    prompt = f"""
    角色：你是頂尖、冷酷、極度重視風險管理的台股短線量化操盤總監。
    任務：根據今日技術數據，撰寫極度精準、具備絕對數據顆粒度(必須寫出實際價格與張數)的【戰略總結報告】。
    
    【最新市場數據庫 (內含今日各均線價格與成交張數)】
    【庫存倉位】:
    {inventory_txt}
    【自選觀察】:
    {watchlist_txt}
    【🔥 今日黃金進場公式篩選】
    {golden_candidates}
    【🚀 今日漲停潛力股獵殺名單】
    {limit_up_candidates_txt}
    
    【❌ 鐵律：違反直接扣薪 ❌】：
    1. 第一章至第五章請維持精簡，分類明確，必須包含具體價格數字。
    2. ✨【★ 明日券商 APP 智慧單下單精確設定】：
       深度交叉比對第二章觀察潛力股與第四章黃金公式清單。挑選 1~2 檔最優標的。
       你必須依據個股位階，將其嚴格分類為以下兩種等級。你必須維持這兩個等級標題的輸出！
       
       【🚨 關鍵流動性與防漏空缺鐵律】：
       - 必須在下單設定內明確標示【今日實際成交量】。
       - 如果被推薦的股票今日成交量【小於 500 張】，你必須在標題與進場解析最前方，一字不漏地強制加上紅字警告：
         "⚠️ [冷門股防範：注意此股今日成交量低於500張，流動性極差，請嚴格控管資金或改採零股少量試單！]"
       - 為了防範操盤手誤以為程式漏掉，若今日盤面無符合「低位階」的標的，請在【等級 A】下方強制輸出一行宣示文字：「今日無符合 [等級 A：低位階尚未起飛股] 之推薦標的，嚴格控管資金風險。」
       - 同理，若無符合「中位階」的標的，請在【等級 B】下方強制輸出：「今日無符合 [等級 B：中位階主升起飛股] 之推薦標的，嚴格控管資金風險。」
       
    ==========【等級 A 專屬模板】==========
    (適用條件：MA5 貼近 MA20，日漲跌 0%~3%，量縮。若今日無符合標的，則直接以此標題下接一行：今日無符合 [等級 A：低位階尚未起飛股] 之推薦標的，嚴格控管資金風險。)
    🎯 獵殺目標：[股票名稱] (代號) - ✨ 特選：低位階尚未起飛股 [今日成交: XX張] (若低於500張需在此加上⚠️冷門股警告)
    - 📊 進場邏輯深度解析 (黃金公式大數據拆解)：
      【流動性檢視】：今日實際成交量為 [實際今日張數]張 (對比5日均量 [實際5日均量]張)。(若低於500張需在此加註冷門股風險提示)
      1. 【大趨勢保護】：現價 ([實際現價]元) 位於 MA20 月線 ([實際MA20]元) 與 MA60 季線 ([實際MA60]元) 之上，呈現多頭排列。
      2. 【洗盤籌碼沉澱】：今日成交量為 [實際今日張數]張，顯示籌碼洗盤沉澱，賣壓枯竭。
      3. 【位階安全防禦】：今日漲跌幅為 [實際日漲跌幅]%，股價剛好回測至 MA5 ([實際MA5]元) 附近，屬於安全且尚未起飛的甜蜜點。
    - 精確進場區間：AI 總監提示「回測 MA5 ([實際MA5]元) 附近進場」
    - APP 實戰設定步驟：
      1. 開啟手機券商 APP，選擇「長效期智慧單」。
      2. 觸發條件設定：當股價小於或等於 [實際MA5 + 0.1元] 時。
      3. 下單動作設定：以「限價 [實際MA5元]」買入 1 張。
      4. 終極安全帶（停損設定）：智慧停損單設定「當股價收盤跌破 MA20: [實際MA20元]」立刻市價砍出。
      
    ==========【等級 B 專屬模板】==========
    (適用條件：MA5>MA10>MA20，日漲跌 3%~6%，溫和出量。若今日無符合標的，則直接以此標題下接一行：今日無符合 [等級 B：中位階主升起飛股] 之推薦標的，嚴格控管資金風險。)
    🎯 獵殺目標：[股票名稱] (代號) - ⚡ 衝刺：中位階主升起飛股 [今日成交: XX張] (若低於500張需在此加上⚠️冷門股警告)
    - 📊 進場邏輯深度解析 (黃金公式大數據拆解)：
      【流動性檢視】：今日實際成交量為 [實際今日張數]張 (為5日均量 [實際5日均量]張的倍數放大)。(若低於500張需在此加註冷門股風險提示)
      1. 【多頭發散攻勢】：現價 ([實際現價]元) 呈現 MA5 ([實際MA5]元) > MA10 ([實際MA10]元) > MA20 ([實際MA20]元) 的強勢發散排列，動能強勁。
      2. 【主力追價表態】：今日成交量放大至 [實際今日張數]張，資金持續推升滾量。
      3. 【位階波段評估】：今日上漲 [實際日漲跌幅]%，代表股價已成功脫離底部並展開主升段衝刺，但尚未進入超買過熱區。
    - 精確進場區間：AI 總監提示「回測 MA5 ([實際MA5]元) 附近進場」
    - APP 實戰設定步驟：
      1. 開啟手機券商 APP，選擇「長效期智慧單」。
      2. 觸發條件設定：當股價小於或等於 [實際MA5 + 0.1元] 時。
      3. 下單動作設定：以「限價 [實際MA5元]」買入 1 張.
      4. 終極安全帶（停損設定）：智慧停損單設定「當股價收盤跌破 MA20: [實際MA20元]」立刻市價砍出。

    請嚴格依照六個章節直接輸出（繁體中文）：
    ### 1. 庫存持股總體檢
    ### 2. 觀察名單潛力股
    ### 3. 總結操作建議
    ### 4. 黃金進場公式 (每日必檢)
    ### 5. 🎯 漲停潛力股獵殺 (AI預測)
    ### ★ 明日券商 APP 智慧單下單精確設定
    (此處必須同時保留 A 與 B 等級之標題區塊，若該等級無標的則依鐵律明示宣示文字)
    """

    for model_name in MODEL_CANDIDATES:
        try:
            response = AI_CLIENT.models.generate_content(model=model_name, contents=prompt)
            record_token_usage(response)  
            return response.text
        except:
            time.sleep(2)
            continue
    return "AI 生成總結報告失敗"

# ==========================================
# 6. 行情數據抓取核心
# ==========================================
def fetch_pro_metrics(stock_data):
    # ✨ 關鍵修改：從傳進來的 stock_data 中解包出我們優先獲取的真實股票名稱
    sid, passed_name, is_hold, cost = stock_data['sid'], stock_data['name'], stock_data['is_hold'], stock_data['cost']
    stock, full_id = get_tw_stock(sid)
    if not stock: return None
    try:
        df_hist = stock.history(period="8mo")
        if len(df_hist) < 120: return None
        info = stock.info
        latest = df_hist.iloc[-1]
        curr_p, curr_vol = latest['Close'], latest['Volume']
        today_amount = (curr_vol * curr_p) / 100_000_000
        
        delta = df_hist['Close'].diff()
        gain, loss = delta.where(delta > 0, 0).rolling(14).mean(), (-delta.where(delta < 0, 0)).rolling(14).mean()
        clean_rsi = round(100 - (100 / (1 + (gain.iloc[-1] / loss.iloc[-1]))), 1) if loss.iloc[-1] != 0 else 50.0
        
        ma5 = round(df_hist['Close'].rolling(5).mean().iloc[-1], 2)
        ma10 = round(df_hist['Close'].rolling(10).mean().iloc[-1], 2)
        ma20 = round(df_hist['Close'].rolling(20).mean().iloc[-1], 2)
        ma60 = round(df_hist['Close'].rolling(60).mean().iloc[-1], 2)
        bias_60 = ((curr_p - ma60) / ma60) * 100
        
        ma_alert_str = check_ma_status(curr_p, ma5, ma10, ma20, ma60)
        is_golden, golden_msg = check_golden_entry(df_hist)
        raw_yield = info.get('dividendYield', 0) or 0
        
        vol_ratio = curr_vol / df_hist['Volume'].iloc[-6:-1].mean() if df_hist['Volume'].iloc[-6:-1].mean() > 0 else 0
        pure_id = ''.join(filter(str.isdigit, sid))
        fs, ss = get_streak_only(pure_id) 

        score = 5
        if (info.get('profitMargins', 0) or 0) > 0: score += 1
        if curr_p > ma60: score += 1
        if 0.02 < raw_yield < 0.12: score += 1
        if 45 < clean_rsi < 68: score += 1
        if fs >= 2 or ss >= 1: score += 1
        if is_golden: score += 3

        # ✨ 關鍵防呆機制：如果 Google Sheets 抓下來的名字是空的，才會退回去用字典的名字
        map_name, industry = STOCK_INFO_MAP.get(str(sid), (sid, "其他/ETF"))
        final_stock_name = passed_name if passed_name else map_name
        
        market_label = '櫃' if '.TWO' in full_id else '市'

        vol_today_lots = int(curr_vol / 1000) if not pd.isna(curr_vol) else 0
        vol_ma5_lots = int(df_hist['Volume'].iloc[-6:-1].mean() / 1000) if not pd.isna(df_hist['Volume'].iloc[-6:-1].mean()) else 0

        res = {
            "id": f"{sid}{market_label}", "name": final_stock_name, "score": score, "rsi": clean_rsi, "industry": industry,
            "vol_r": round(vol_ratio, 1), "p": round(curr_p, 2), "yield": raw_yield, "amt_t": round(today_amount, 1),
            "d1": (curr_p / df_hist['Close'].iloc[-2]) - 1, "d5": (curr_p / df_hist['Close'].iloc[-6]) - 1,
            "m1": (curr_p / df_hist['Close'].iloc[-21]) - 1, "m6": (curr_p / df_hist['Close'].iloc[-121]) - 1,
            "is_hold": is_hold, "cost": cost, "bias_str": f"{bias_60:+.1f}%", "vol_str": get_vol_status_str(vol_ratio),
            "fs": fs, "ss": ss, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma_alert": ma_alert_str,
            "is_golden": is_golden, "golden_msg": golden_msg,
            "v_today": vol_today_lots,
            "v_ma5": vol_ma5_lots
        }
        
        # 🚀【動態量化風險診斷評級】
        if bias_60 > 15 or clean_rsi > 75: res["risk"] = "🚨高檔過熱"
        elif curr_p < ma20: res["risk"] = "⚠️破線警戒"
        else: res["risk"] = "🟢正常"
            
        # 🚀【動態量化波段趨勢評級】
        if ma5 > ma10 and ma10 > ma20 and ma20 > ma60: res["trend"] = "📈強勢多頭"
        elif curr_p < ma60: res["trend"] = "📉空頭修正"
        else: res["trend"] = "☁️區間震盪"
            
        # 🚀【動態量化操盤訊號提示】
        if is_golden: res["hint"] = "🔥黃金買點"
        elif score >= 8: res["hint"] = "🚀強勢進攻"
        else: res["hint"] = "👀持續追蹤"
        
        res['ai_strategy'] = get_gemini_strategy(res)
        return res
    except: return None

def get_tw_stock(sid):
    clean_id = str(sid).strip().upper()
    suffixes = [".TWO", ".TW"] if clean_id.startswith(('3', '4', '5', '6', '8')) else [".TW", ".TWO"]
    for suffix in suffixes:
        target = f"{clean_id}{suffix}"
        try:
            hist = yf.Ticker(target).history(period="5d")
            if not hist.empty: return yf.Ticker(target), target
        except: continue
    return None, None

def send_email(subject, body):
    if not MAIL_USER or not MAIL_PASS: return
    msg = MIMEMultipart(); msg['From'] = MAIL_USER; msg['To'] = ", ".join(MAIL_RECEIVERS); msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587); server.starttls(); server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg); server.quit()
        print("✅ 郵件發送成功")
    except Exception as e: print(f"❌ 郵件失敗: {e}")

# ==========================================
# 8. 主程式執行區塊
# ==========================================
def main():
    current_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
    watch_data_list = get_watch_list_from_sheet()
    if not watch_data_list: return

    results_line, results_sheet = [], []
    for idx, stock_data in enumerate(watch_data_list):
        res = fetch_pro_metrics(stock_data)
        if res:
            results_line.append(res)
            results_sheet.append([current_time, res['id'], res['name'], "📦庫存" if res['is_hold'] else "👀觀察", res['score'], res['rsi'], res['industry'], res['bias_str'], res['vol_str'], res['fs'], res['ss'], res['p'], res['yield'], res['amt_t'], res['d1'], res['d5'], res['m1'], res['m6'], res['risk'], res['trend'], res['hint'], res['ai_strategy']])
        if idx < len(watch_data_list) - 1: time.sleep(2.0)
    
    if results_line:
        time.sleep(10) 
        summary_text = generate_and_save_summary(results_line, current_time)
        
        report_sheet_url = sync_to_sheets(results_sheet)
        if not report_sheet_url:
            report_sheet_url = "無法動態獲取連結，請至 Google Drive 查閱"
        
        twd_cost = calculate_twd_cost()
        line_quota_report = get_line_quota_report()

        print("\n==========================================")
        print("💰 本次代碼工作 AI 運作成本結算報告")
        print(f"🔹 執行時間：{current_time}")
        print(f"🔹 AI API 呼叫總次數：{GLOBAL_TOKEN_BILLING['api_calls']} 次")
        print(f"🔹 輸入 Token (Prompt)：{GLOBAL_TOKEN_BILLING['prompt_tokens']:,}")
        print(f"🔹 輸出 Token (Completion)：{GLOBAL_TOKEN_BILLING['completion_tokens']:,}")
        print(f"🔹 總消耗 Tokens：{GLOBAL_TOKEN_BILLING['total_tokens']:,}")
        print(f"🔹 預估本次花費台幣：NT$ {twd_cost} 元")
        print("==========================================\n")
        
        try:
            client = get_gspread_client()
            if client:
                spreadsheet = client.open("全能金流診斷報表")
                
                log_execution_cost_to_sheets(spreadsheet, current_time, twd_cost)
                
                try:
                    s_sheet = spreadsheet.worksheet(current_time)
                    s_sheet.clear()
                except:
                    s_sheet = spreadsheet.add_worksheet(title=current_time, rows=150, cols=10)
                
                lines_list = [[line] for line in summary_text.split('\n')]
                s_sheet.update(values=lines_list, range_name='A1')  
                
                body_requests = []
                
                for row_idx in range(1, len(lines_list) + 1):
                    body_requests.append({
                        "mergeCells": {
                            "range": {
                                "sheetId": s_sheet.id,
                                "startRowIndex": row_idx - 1,
                                "endRowIndex": row_idx,
                                "startColumnIndex": 0,
                                "endColumnIndex": 5
                            },
                            "mergeType": "MERGE_ROWS"
                        }
                    })
                
                body_requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": s_sheet.id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 5
                        },
                        "properties": {
                            "pixelSize": 140
                        },
                        "fields": "pixelSize"
                    }
                })
                
                if body_requests:
                    spreadsheet.batch_update({"requests": body_requests})
                
                s_sheet.format("A1:E150", {
                    "wrapStrategy": "WRAP",
                    "verticalAlignment": "TOP",
                    "textFormat": {
                        "fontSize": 10,
                        "fontFamily": "Microsoft JhengHei"
                    }
                })
                    
                print(f"✅ 獨立日期戰略分頁 [{current_time}] 已完美套用圖2規格生成！")
        except Exception as e: 
            print(f"⚠️ 建立圖2排版戰略分頁失敗: {e}")

        line_quota_html = line_quota_report.replace('\n', '<br>')

        cost_report_html = f"""
        <div style='background-color:#fff9db; padding:15px; border-left:5px solid #fcc419; margin-top:20px; font-family:sans-serif;'>
            <h3 style='margin-top:0; color:#e67e22;'>💰 今日運作成本診斷報告</h3>
            <p><b>【雲端主報表連結】</b><br>
            - 🔗 <a href='{report_sheet_url}'>點擊前往查看數據報表</a></p>
            <p><b>【Gemini API 帳單】</b><br>
            - 消耗總 Tokens：<span style='color:#d9480f;'>{GLOBAL_TOKEN_BILLING['total_tokens']:,}</span><br>
            - 預估台幣費用：<span style='color:#c92a2a;'><b>NT$ {twd_cost} 元</b></span></p>
            <p style='margin-bottom:0;'><b>【LINE Bot 免費額度】</b><br>
            {line_quota_html}</p>
        </div>
        """

        email_body = f"<html><body><h2>📊 {current_time} 全能金流診斷</h2><pre style='font-family:sans-serif; white-space:pre-wrap;'>{summary_text}</pre><hr>{cost_report_html}</body></html>"
        send_email(f"[{current_time}] 台股 AI 戰報 (附成本與 LINE 額度診斷)", email_body)

        if LINE_ACCESS_TOKEN:
            line_msg = (
                f"📊 【{current_time} 戰略報告已更新】\n\n"
                f"今日自選股診斷已執行完畢，全新「三階位階評估 + 智慧單精算」與圖2規格美化分頁已成功產生！\n\n"
                f"🔗 點擊直達雲端主報表：\n{report_sheet_url}\n\n"
                f"── 💸 今日 AI 帳單明細 ──\n"
                f"🔹 總消耗 Tokens：{GLOBAL_TOKEN_BILLING['total_tokens']:,}\n"
                f"💰 今日預估費用：NT$ {twd_cost} 元\n\n"
                f"{line_quota_report}"
            )
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
            payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": line_msg}]}
            requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=payload)
            print("✅ 終極完全體持股體檢戰報已全面部署成功！")

if __name__ == "__main__":
    main()
