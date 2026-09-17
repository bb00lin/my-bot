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
AUTH = HTTPBasicAuth(USERNAME, API_TOKEN)


def _env_flag(name, default):
    """讀取布林環境變數；值為空時（例如排程觸發沒有手動輸入）沿用預設。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --- 半年分類設定 ---
# 分類頁刻意命名為 2025H1 這種不含 WeeklyReport 的格式，
# 否則會被 find_latest_report 的 "WeeklyReport*" 搜尋誤選為複製範本。
REPORT_TITLE_PATTERN = re.compile(r'^WeeklyReport_(\d{8})$')
GROUP_TITLE_PATTERN = re.compile(r'^\d{4}H[12]$')
GROUP_BY_HALF = _env_flag("WEEKLY_GROUP_BY_HALF", True)
# DRY RUN 只影響分類：新週報照常建立（暫掛舊位置），但不建立分類頁也不搬移任何頁面。
ORGANIZE_DRY_RUN = _env_flag("WEEKLY_ORGANIZE_DRY_RUN", False)
# 只分類模式：跳過建立新週報，用於一次性把既有頁面歸位而不多生一週的頁面。
ORGANIZE_ONLY = _env_flag("WEEKLY_ORGANIZE_ONLY", False)
MAX_PAGINATION_REQUESTS = 50
GROUP_PAGE_BODY = (
    '<p>此頁由 confluence_api2.py 自動建立，用於收納該半年度的週報。</p>'
    '<ac:structured-macro ac:name="children" ac:schema-version="1">'
    '<ac:parameter ac:name="sort">title</ac:parameter>'
    '<ac:parameter ac:name="reverse">true</ac:parameter>'
    '</ac:structured-macro>'
)


def parse_report_date(title):
    """WeeklyReport_YYYYMMDD -> date 物件；非週報標題回傳 None。"""
    match = REPORT_TITLE_PATTERN.match((title or '').strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def half_year_group_title(report_date):
    """依週報日期決定所屬半年分類頁標題（例: 2025H2）。"""
    return f"{report_date.year}H{1 if report_date.month <= 6 else 2}"


def find_latest_report():
    print("🔍 正在搜尋最新週報...")
    cql = 'type=page AND title ~ "WeeklyReport*" ORDER BY created DESC'
    url = f"{API_ENDPOINT}/search"
    params = {'cql': cql, 'limit': 25, 'expand': 'body.storage,ancestors,space'}

    try:
        response = requests.get(url, auth=AUTH, params=params)
        response.raise_for_status()
        results = response.json().get('results', [])
        # CQL 的 ~ 是模糊比對，總目錄頁 WeeklyReport 也會命中，這裡只留真正的週報
        reports = [page for page in results if parse_report_date(page.get('title', ''))]
        if not reports:
            print("⚠️ 找不到任何基準週報。")
            sys.exit(1)
        latest = reports[0]
        print(f"✅ 找到基準週報: {latest['title']} (ID: {latest['id']})")
        return latest
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")
        sys.exit(1)


def calculate_next_report_date(latest_title):
    match = re.search(r"(\d{8})", latest_title)
    if match:
        last_date_str = match.group(1)
        try:
            last_date_obj = datetime.strptime(last_date_str, "%Y%m%d").date()
            return last_date_obj + timedelta(days=7)
        except ValueError:
            pass

    print("⚠️ 無法解析標題日期，使用本週五為基準。")
    today = datetime.now().date()
    return today + timedelta(days=(4 - today.weekday()))


def resolve_root_page(latest_page):
    """
    找出週報總目錄頁（例: WeeklyReport）。
    由基準頁的祖先從最深往上找，跳過半年分類頁後的第一個祖先即為總目錄。
    """
    for ancestor in reversed(latest_page.get('ancestors') or []):
        if not GROUP_TITLE_PATTERN.match((ancestor.get('title') or '').strip()):
            return ancestor
    return None


def _paged_get(url, params):
    """依回傳的 next 連結走訪 Confluence 分頁 API，彙整所有 results。"""
    results = []
    next_url, next_params = url, dict(params)

    for _ in range(MAX_PAGINATION_REQUESTS):
        response = requests.get(next_url, auth=AUTH, params=next_params)
        response.raise_for_status()
        data = response.json()
        batch = data.get('results', [])
        results.extend(batch)

        links = data.get('_links', {})
        next_path = links.get('next')
        if not batch or not next_path:
            return results
        next_url = f"{links.get('base') or f'{BASE_URL}/wiki'}{next_path}"
        next_params = None

    print(f"  ⚠️ 分頁請求已達上限 {MAX_PAGINATION_REQUESTS} 次，結果可能不完整。")
    return results


def list_child_pages(parent_id):
    return _paged_get(f"{API_ENDPOINT}/{parent_id}/child/page", {'limit': 100})


def load_group_pages(root_id):
    """讀取總目錄底下現有的半年分類頁，回傳 {標題: 頁面 ID}。"""
    groups = {}
    for child in list_child_pages(root_id):
        title = (child.get('title') or '').strip()
        if GROUP_TITLE_PATTERN.match(title):
            groups[title] = str(child['id'])
    return groups


def find_page_by_exact_title(space_key, title):
    response = requests.get(
        API_ENDPOINT,
        auth=AUTH,
        params={'spaceKey': space_key, 'title': title, 'limit': 1}
    )
    if response.status_code != 200:
        return None
    results = response.json().get('results', [])
    return str(results[0]['id']) if results else None


def create_group_page(root_id, space_key, group_title):
    payload = {
        "title": group_title,
        "type": "page",
        "space": {"key": space_key},
        "ancestors": [{"id": str(root_id)}],
        "body": {
            "storage": {
                "value": GROUP_PAGE_BODY,
                "representation": "storage"
            }
        }
    }

    response = requests.post(
        API_ENDPOINT,
        auth=AUTH,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload)
    )
    if response.status_code in (200, 201):
        print(f"  📁 已建立半年分類頁: {group_title}")
        return str(response.json()['id'])

    # 同空間內標題必須唯一：若已被既有頁面佔用就沿用，否則每次執行都會卡在這裡
    existing = find_page_by_exact_title(space_key, group_title)
    if existing:
        print(f"  ⚠️ 空間內已存在標題為 '{group_title}' 的頁面，直接沿用 (ID: {existing})")
        return existing

    print(f"  ❌ 建立分類頁 '{group_title}' 失敗: {response.status_code} {response.text[:200]}")
    return None


def ensure_group_page(root_id, space_key, group_title, groups):
    if groups.get(group_title):
        return groups[group_title]
    if ORGANIZE_DRY_RUN:
        print(f"  🧪 [DRY RUN] 將建立半年分類頁: {group_title}")
        return None

    group_id = create_group_page(root_id, space_key, group_title)
    if group_id:
        groups[group_title] = group_id
    return group_id


def _move_by_ancestor_update(page_id, parent_id, page_title):
    """備援搬移：move API 不可用時改更新 ancestors，內文原樣回填不做任何改寫。"""
    detail = requests.get(
        f"{API_ENDPOINT}/{page_id}",
        auth=AUTH,
        params={'expand': 'body.storage,version,space'}
    )
    if detail.status_code != 200:
        print(f"  ❌ 讀取 '{page_title}' 失敗，略過搬移: {detail.status_code}")
        return False

    data = detail.json()
    payload = {
        "id": str(page_id),
        "type": "page",
        "title": data['title'],
        "space": {"key": data['space']['key']},
        "ancestors": [{"id": str(parent_id)}],
        "body": {
            "storage": {
                "value": data['body']['storage']['value'],
                "representation": "storage"
            }
        },
        "version": {"number": data['version']['number'] + 1, "minorEdit": True}
    }

    response = requests.put(
        f"{API_ENDPOINT}/{page_id}",
        auth=AUTH,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload)
    )
    if response.status_code == 200:
        return True

    print(f"  ❌ 搬移 '{page_title}' 失敗: {response.status_code} {response.text[:200]}")
    return False


def move_page_under(page_id, parent_id, page_title):
    """把頁面掛到指定父頁面底下。優先用 move API（不動內文、不產生新版本）。"""
    response = requests.put(f"{API_ENDPOINT}/{page_id}/move/append/{parent_id}", auth=AUTH)
    if response.status_code in (200, 204):
        return True
    if response.status_code in (404, 405):
        return _move_by_ancestor_update(page_id, parent_id, page_title)

    print(f"  ❌ 搬移 '{page_title}' 失敗: {response.status_code} {response.text[:200]}")
    return False


def find_all_report_pages(space_key):
    """搜尋空間內所有週報頁面（含目前所在位置），用於揪出被搬出分類頁的漏網之魚。"""
    cql = f'type=page AND space="{space_key}" AND title ~ "WeeklyReport*"'
    pages = _paged_get(
        f"{API_ENDPOINT}/search",
        {'cql': cql, 'limit': 100, 'expand': 'ancestors'}
    )
    return [page for page in pages if parse_report_date(page.get('title', ''))]


def build_organize_plan(reports, groups):
    """算出需要搬移的週報清單 [(頁面, 目標分類標題), ...]，依標題排序方便閱讀 log。"""
    plan = []
    for page in sorted(reports, key=lambda p: p.get('title', '')):
        report_date = parse_report_date(page.get('title', ''))
        if not report_date:
            continue

        group_title = half_year_group_title(report_date)
        expected_id = groups.get(group_title)
        ancestors = page.get('ancestors') or []
        current_parent_id = str(ancestors[-1]['id']) if ancestors else None

        # 分類頁還不存在時 expected_id 為 None，此時一律視為待搬移
        if expected_id and current_parent_id == expected_id:
            continue
        plan.append((page, group_title))
    return plan


def organize_reports_by_half_year(root_id, space_key, groups):
    print("\n🗂️ 正在依半年 (H1/H2) 整理週報頁面...")
    reports = find_all_report_pages(space_key)
    if not reports:
        print("  ⚠️ 找不到任何 WeeklyReport_YYYYMMDD 頁面，略過整理。")
        return

    plan = build_organize_plan(reports, groups)
    if not plan:
        print(f"  ✅ 共 {len(reports)} 份週報，全部已歸類正確，無需搬移。")
        return

    print(f"  📋 共 {len(reports)} 份週報，其中 {len(plan)} 份需要歸類：")
    for page, group_title in plan:
        print(f"    └ {page['title']} → {group_title}")

    if ORGANIZE_DRY_RUN:
        print("  🧪 DRY RUN：僅列出計畫，未實際建立分類頁或搬移任何頁面。")
        return

    moved, failed = 0, 0
    for page, group_title in plan:
        group_id = ensure_group_page(root_id, space_key, group_title, groups)
        if not group_id:
            failed += 1
            continue
        if move_page_under(page['id'], group_id, page['title']):
            moved += 1
            print(f"  ✅ {page['title']} → {group_title}")
        else:
            failed += 1

    summary = f"\n🗂️ 整理完畢！成功歸類 {moved} 份週報"
    print(f"{summary}，失敗 {failed} 份。" if failed else f"{summary}。")


def resolve_new_page_parent(latest_page, root_page, space_key, report_date, groups):
    """決定新週報掛在哪個父頁面：優先放進對應半年分類頁，否則沿用基準頁的父頁面。"""
    if GROUP_BY_HALF and root_page:
        group_id = ensure_group_page(
            root_page['id'], space_key, half_year_group_title(report_date), groups
        )
        if group_id:
            return group_id

    ancestors = latest_page.get('ancestors') or []
    return str(ancestors[-1]['id']) if ancestors else None


def create_new_report(latest_page, root_page, space_key, groups):
    next_date = calculate_next_report_date(latest_page['title'])
    new_title = f"WeeklyReport_{next_date.strftime('%Y%m%d')}"
    print(f"📄 準備建立新頁面: {new_title}")

    check_url = f"{API_ENDPOINT}/search"
    check_params = {'cql': f'title = "{new_title}"'}
    check_resp = requests.get(check_url, auth=AUTH, params=check_params)
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

    parent_id = resolve_new_page_parent(latest_page, root_page, space_key, next_date, groups)
    ancestors = [{'id': parent_id}] if parent_id else []

    payload = {
        "title": new_title,
        "type": "page",
        "space": {"key": space_key},
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
            auth=AUTH,
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
                    r_links_resp = requests.get(remote_link_url, auth=AUTH)

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
                                del_resp = requests.delete(f"{remote_link_url}/{link_id}", auth=AUTH)
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
    print(f"=== Confluence API 自動週報 (完美排版 + 歷史大清洗 + 半年分類) ===")
    if ORGANIZE_DRY_RUN:
        print("🧪 DRY RUN 模式：只列出分類計畫，不會建立分類頁也不會搬移既有頁面。")
    if ORGANIZE_ONLY:
        print("📌 只分類模式：本次不建立新週報，僅整理既有頁面。")
        if not GROUP_BY_HALF:
            print("⚠️ 只分類模式搭配分類總開關關閉，本次沒有任何事情可做。")
            return
    try:
        latest_page = find_latest_report()
        space_key = latest_page['space']['key']
        root_page = resolve_root_page(latest_page) if GROUP_BY_HALF else None

        groups = {}
        if GROUP_BY_HALF:
            if root_page:
                print(f"📚 週報總目錄頁: {root_page['title']} (ID: {root_page['id']})")
                groups = load_group_pages(root_page['id'])
            else:
                print("⚠️ 基準週報沒有可用的父頁面，本次略過半年分類。")

        if not ORGANIZE_ONLY:
            create_new_report(latest_page, root_page, space_key, groups)

        if GROUP_BY_HALF and root_page:
            organize_reports_by_half_year(root_page['id'], space_key, groups)
    except Exception as e:
        print(f"執行中斷: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
