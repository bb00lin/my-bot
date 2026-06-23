import os
import requests
import json
import re
import sys
import time
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- 設定區 ---
RAW_URL = os.environ.get("CONF_URL")
USERNAME = os.environ.get("CONF_USER")
API_TOKEN = os.environ.get("CONF_PASS")

if not RAW_URL or not USERNAME or not API_TOKEN:
    print("❌ 錯誤：缺少環境變數 (請確認已設定 CONF_URL, CONF_USER, CONF_PASS)")
    sys.exit(1)

parsed = urlparse(RAW_URL)
BASE_URL = f"{parsed.scheme}://{parsed.netloc}"
API_ENDPOINT = f"{BASE_URL}/wiki/rest/api/content"

def find_latest_report():
    print("🔍 正在搜尋最新週報...")
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
    match = re.search(r"(\d{8})", latest_title)
    if match:
        last_date_str = match.group(1)
        try:
            last_date_obj = datetime.strptime(last_date_str, "%Y%m%d").date()
            next_date = last_date_obj + timedelta(days=7)
            return next_date.strftime("%Y%m%d")
        except ValueError: 
            pass
            
    print("⚠️ 無法解析標題日期，使用本週五為基準。")
    today = datetime.now().date()
    friday = today + timedelta(days=(4 - today.weekday()))
    return friday.strftime("%Y%m%d")

def create_new_report(latest_page):
    next_filename = calculate_next_filename(latest_page['title'])
    new_title = f"WeeklyReport_{next_filename}"
    print(f"📄 準備建立新頁面: {new_title}")
    
    check_url = f"{API_ENDPOINT}/search"
    check_params = {'cql': f'title = "{new_title}"'}
    check_resp = requests.get(check_url, auth=HTTPBasicAuth(USERNAME, API_TOKEN), params=check_params)
    if check_resp.json().get('results'):
        print(f"⚠️ 跳過：頁面 '{new_title}' 已經存在！")
        return

    original_body = latest_page['body']['storage']['value']
    
    # 🧹 清理上一週的日誌區塊 (daily-worklog)，確保下方施工區乾淨
    soup = BeautifulSoup(original_body, 'html.parser')
    for div in soup.find_all('div'):
        classes = div.get('class', [])
        if any(cls.startswith('daily-worklog-') for cls in classes):
            div.extract()
            
    new_body = str(soup)

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
    
    # 發送請求建立頁面
    try:
        response = requests.post(
            API_ENDPOINT, 
            auth=HTTPBasicAuth(USERNAME, API_TOKEN),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload)
        )
        response.raise_for_status()
        data = response.json()
        new_page_id = str(data['id']) # 取得新頁面的 ID
        webui = data['_links']['webui']
        link = f"{BASE_URL}/wiki{webui}" if not webui.startswith('/wiki') else f"{BASE_URL}{webui}"
        
        print(f"🎉 成功建立新週報！(表格排版已完整保留，舊日誌區塊已清空)")
        print(f"🌐 頁面連結: {link}")
        
        # ==========================================
        # 🌟 歷史大清洗邏輯：刪除 Jira 中被自動產生的所有週報 Linked Pages
        # ==========================================
        # 從內文中找出所有可能是 Jira 任務編號的字串 (例如 RFQ-93, PBY-102)
        jira_keys = list(set(re.findall(r'[A-Z][A-Z0-9]+-\d+', original_body)))
        
        if jira_keys:
            print(f"\n⏳ 偵測到 {len(jira_keys)} 個 Jira 任務。")
            print("等待 5 秒鐘，讓 Atlassian 系統完成背景自動連動...")
            time.sleep(5)
            
            print("🧹 啟動歷史大清洗：正在拔除 Jira 任務底下『所有』的週報連動紀錄...")
            cleared_count = 0
            
            for key in jira_keys:
                try:
                    # 去該 Jira 任務查詢所有的 Remote Links
                    remote_link_url = f"{BASE_URL}/rest/api/3/issue/{key}/remotelink"
                    r_links_resp = requests.get(remote_link_url, auth=HTTPBasicAuth(USERNAME, API_TOKEN))
                    
                    if r_links_resp.status_code == 200:
                        r_links = r_links_resp.json()
                        for link_obj in r_links:
                            url_val = link_obj.get('object', {}).get('url', '')
                            title_val = link_obj.get('object', {}).get('title', '')
                            
                            # 💡 判斷條件：只要標題包含 "WeeklyReport"，或網址包含 "WeeklyReport"，
                            # 或是我們剛剛建立的新頁面，就一律刪除！
                            if "WeeklyReport" in title_val or "WeeklyReport" in url_val or new_page_id in url_val:
                                link_id = link_obj.get('id')
                                # 呼叫 API 刪除該筆連動紀錄
                                del_resp = requests.delete(f"{remote_link_url}/{link_id}", auth=HTTPBasicAuth(USERNAME, API_TOKEN))
                                if del_resp.status_code in [200, 204]:
                                    cleared_count += 1
                                    print(f"  └ 🗑️ 已刪除 [{key}] 的殘留紀錄: {title_val}")
                except Exception as e:
                    pass
                    
            print(f"\n✅ 歷史大清洗完畢！共成功拔除了 {cleared_count} 筆殘留的 Jira 週報連動紀錄。")

    except requests.exceptions.HTTPError as e:
        print(f"❌ 建立失敗: {e}")
        print(response.text)
        sys.exit(1)

def main():
    print(f"=== Confluence API 自動週報 (完美排版 + 歷史大清洗) ===")
    try:
        latest_page = find_latest_report()
        create_new_report(latest_page)
    except Exception as e:
        print(f"執行中斷: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()