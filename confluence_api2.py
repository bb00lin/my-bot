import os
import requests
import json
import re
import sys
from datetime import datetime, timedelta, date
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse

# --- 設定區 ---
RAW_URL = os.environ.get("CONF_URL")
USERNAME = os.environ.get("CONF_USER")
API_TOKEN = os.environ.get("CONF_PASS")

if not RAW_URL or not USERNAME or not API_TOKEN:
    print("錯誤：缺少環境變數")
    sys.exit(1)

parsed = urlparse(RAW_URL)
BASE_URL = f"{parsed.scheme}://{parsed.netloc}"
API_ENDPOINT = f"{BASE_URL}/wiki/rest/api/content"

def find_latest_report():
    print("正在搜尋最新週報...")
    cql = 'type=page AND title ~ "WeeklyReport*" ORDER BY created DESC'
    url = f"{API_ENDPOINT}/search"
    params = {'cql': cql, 'limit': 1, 'expand': 'body.storage,ancestors,space'}
    
    try:
        response = requests.get(url, auth=HTTPBasicAuth(USERNAME, API_TOKEN), params=params)
        response.raise_for_status()
        results = response.json().get('results', [])
        if not results:
            print("⚠️ 找不到任何基準週報。")
            sys.exit(1)
        latest = results[0]
        print(f"✅ 找到基準週報: {latest['title']} (ID: {latest['id']})")
        return latest
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")
        sys.exit(1)

def calculate_next_filename(latest_title):
    """
    從標題解析日期，並推算下週五的檔名 (YYYYMMDD)
    """
    match = re.search(r"(\d{8})", latest_title)
    if match:
        last_date_str = match.group(1)
        try:
            last_date_obj = datetime.strptime(last_date_str, "%Y%m%d").date()
            next_date = last_date_obj + timedelta(days=7)
            return next_date.strftime("%Y%m%d")
        except ValueError: pass
            
    print("⚠️ 無法解析標題日期，使用本週五為基準。")
    today = datetime.now().date()
    friday = today + timedelta(days=(4 - today.weekday()))
    return friday.strftime("%Y%m%d")

def shift_all_dates(content):
    """
    將內容中所有日期格式字串增加 7 天，並盡量保留原始格式 (單/雙碼、分隔符)
    支援格式: YYYY-M-D, YYYY/M/D, YYYY.M.D
    """
    print("正在執行全域日期推移 (+7 days)...")
    
    # Regex 說明:
    # (\d{4}) : 年
    # ([-/.]) : 分隔符 (記住這一個，後面要用同一個)
    # (\d{1,2}): 月
    # \2      : 引用第 2 組的分隔符 (確保前後一致)
    # (\d{1,2}): 日
    pattern = re.compile(r'(\d{4})([-/.])(\d{1,2})\2(\d{1,2})')
    
    def replace_callback(match):
        year_str, sep, month_str, day_str = match.groups()
        full_str = match.group(0)
        
        try:
            # 解析日期
            current_date = date(int(year_str), int(month_str), int(day_str))
            # 加 7 天
            new_date = current_date + timedelta(days=7)
            
            # --- 格式還原邏輯 ---
            # 檢查原本的月/日是否有補 0 (透過字串長度判斷)
            # 如果原字串長度是 2 (例如 '01')，新日期也要補 0
            # 如果原字串長度是 1 (例如 '1')，新日期不要補 0
            
            # 處理月份
            if len(month_str) == 2:
                new_month_str = f"{new_date.month:02d}"
            else:
                new_month_str = f"{new_date.month}"
                
            # 處理日期
            if len(day_str) == 2:
                new_day_str = f"{new_date.day:02d}"
            else:
                new_day_str = f"{new_date.day}"
            
            # 組合成新字串，使用原本的分隔符
            new_date_str = f"{new_date.year}{sep}{new_month_str}{sep}{new_day_str}"
            
            # print(f"  Debug: {full_str} -> {new_date_str}") 
            return new_date_str
            
        except ValueError:
            return full_str # 如果日期不合法 (例如 2026-02-30)，就不動它

    return pattern.sub(replace_callback, content)

def create_new_report(latest_page):
    # 1. 計算新檔名
    next_filename = calculate_next_filename(latest_page['title'])
    new_title = f"WeeklyReport_{next_filename}"
    print(f"準備建立: {new_title}")
    
    # 2. 檢查重複
    check_url = f"{API_ENDPOINT}/search"
    check_params = {'cql': f'title = "{new_title}"'}
    check_resp = requests.get(check_url, auth=HTTPBasicAuth(USERNAME, API_TOKEN), params=check_params)
    if check_resp.json().get('results'):
        print(f"⚠️ 跳過：頁面 '{new_title}' 已經存在！")
        return

    # 3. 處理內容 (全域日期 +7)
    original_body = latest_page['body']['storage']['value']
    new_body = shift_all_dates(original_body)
    
    # 4. 建立頁面
    ancestors = []
    if latest_page.get('ancestors'):
        ancestors.append({'id': latest_page['ancestors'][-1]['id']})
    
    payload = {
        "title": new_title,
        "type": "page",
        "space": {"key": latest_page['space']['key']},
        "ancestors": ancestors,
        "body": {
            "storage": {
                "value": new_body,
                "representation": "storage"
            }
        }
    }
    
    try:
        response = requests.post(
            API_ENDPOINT, 
            auth=HTTPBasicAuth(USERNAME, API_TOKEN),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload)
        )
        response.raise_for_status()
        data = response.json()
        webui = data['_links']['webui']
        link = f"{BASE_URL}/wiki{webui}" if not webui.startswith('/wiki') else f"{BASE_URL}{webui}"
        
        print(f"🎉 成功建立！所有日期已推移 7 天。")
        print(f"連結: {link}")
        
    except requests.exceptions.HTTPError as e:
        print(f"❌ 建立失敗: {e}")
        print(response.text)
        sys.exit(1) # 讓 GitHub Actions 知道失敗了

def main():
    print(f"=== Confluence API 自動週報 (v8.0 日期推移版) ===")
    try:
        latest_page = find_latest_report()
        create_new_report(latest_page)
    except Exception as e:
        print(f"執行中斷: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()