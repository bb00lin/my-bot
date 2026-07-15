import os
import requests
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse
from bs4 import BeautifulSoup, Tag, NavigableString
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# 🌟 新增 LINE Bot SDK 的匯入
from linebot import LineBotApi
from linebot.models import TextSendMessage

# 載入自訂的環境變數檔案 (本地端測試用，GitHub Actions 會自動忽略)
load_dotenv("jira_config.txt")

# --- 1. 環境變數與金鑰設定 (對齊 GitHub Secrets) ---
raw_url = os.environ.get("CONF_URL", "")
parsed = urlparse(raw_url)
# ✅ 網址清洗器：強制只保留主網域
JIRA_URL = f"{parsed.scheme}://{parsed.netloc}"

ADMIN_EMAIL = os.environ.get("CONF_USER")
ADMIN_TOKEN = os.environ.get("CONF_PASS")

if not raw_url or not ADMIN_EMAIL or not ADMIN_TOKEN:
    print("❌ 錯誤：找不到環境變數 CONF_URL, CONF_USER 或 CONF_PASS")
    sys.exit(1)

ADMIN_AUTH = HTTPBasicAuth(ADMIN_EMAIL, ADMIN_TOKEN)

# 這裡的 Email 就算在 Github Secrets 沒設定，程式也會自動切換成人名搜尋，非常安全
ACCOUNT_DICT = {
    "Bob Lin": os.environ.get("CONF_USER"),
    "shannonchang": os.environ.get("SHANNON_EMAIL"),
    "sam.chang": os.environ.get("SAM_EMAIL"),
    "Vic Wu": os.environ.get("VIC_EMAIL"),
    "SF Hsieh": os.environ.get("SF_EMAIL")
}

# ✅ 成員專屬背景顏色設定
USER_BG_COLORS = {
    "Bob Lin": "#F5E6FF",       # 紫色
    "shannonchang": "#E8F0FF",  # 藍色
    "sam.chang": "#FFF8E6",     # 黃色
    "Vic Wu": "#FFEAE6",        # 粉色
    "SF Hsieh": "#E0F8EA"       # 綠色
}

# ✅ 台灣國定假日與補班日設定
TW_HOLIDAYS = {
    "2024-01-01", "2024-02-08", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-28", "2024-04-04", "2024-04-05", "2024-06-10", "2024-09-17", "2024-10-10",
    "2025-01-01", "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-28", "2025-04-03", "2025-04-04", "2025-05-01", "2025-05-30", "2025-10-06", "2025-10-10",
    "2026-01-01", "2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
    "2026-02-27", "2026-02-28", "2026-03-01", "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06", "2026-05-01", "2026-05-02", "2026-05-03",
    "2026-06-19", "2026-06-20", "2026-06-21", "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-10-09", "2026-10-10", "2026-10-11",
    "2026-10-24", "2026-10-25", "2026-10-26", "2026-12-25", "2026-12-26", "2026-12-27"
}

TW_MAKEUP_WORKDAYS = set()

# --- 2. 設定檔載入引擎 ---
class SettingsManager:
    def __init__(self, filepath="settings_daily.json"):
        self.filepath = filepath
        self.config = {
            "filter_comment": True, "filter_started": True, "day_yesterday": False,
            "exclude_keywords": "DailyMeeting", "auto_clear_first": False,
            "day_auto": True, "day_mon": True, "day_tue": True, "day_wed": True,
            "day_thu": True, "day_fri": True, "show_label": True, "show_parent": True,
            "show_status": True, "show_comment": True, "minor_edit": True,
            "inherit_parent_due": True, 
            "due_date_mode": "方案一: 標題列 (統一以本週報產出日結算)", 
            "show_pending_inprogress": True, "show_pending_waiting": True,
            "show_pending_todo": False, "show_pending_candidate": False,
            "show_pending_blocked": False, "show_pending_abort": False, "show_pending_resume": False,
            "show_pending_has_due": False, "compact_layout": False, "use_jira_macro": False,
            "group_by_project": False, "hide_duplicate_transition": False,
            "hide_status_only": True, "style_weekly": True, "sort_desc": True, 
            "show_issue_total_time": True, "show_confluence_links": True, 
            "show_total_time": True, "show_write_time": True,
            "show_duedate": True, "show_due_tbd": False,
            "enable_newline": True, "newline_chars": "; ,",
            "custom_holidays": [], "custom_makeup_days": []
        }
        self.load_settings()

    def load_settings(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.config.update(json.load(f))
                print(f"⚙️ 成功讀取 {self.filepath} 設定檔")
            except Exception as e:
                print(f"⚠️ 讀取設定檔失敗，將使用預設值 ({e})")
        else:
            print(f"⚠️ 找不到 {self.filepath}，將使用預設值")

    def get(self, key, default=None):
        return self.config.get(key, default)

SETTINGS = SettingsManager()

# Worklog 圖片標記：GUI 寫入 [[IMG:檔名]]，同步到 Confluence 時嵌入
IMG_MARKER_RE = re.compile(r'\[\[IMG:([^\]]+?)\]\]', re.IGNORECASE)
PENDING_CONF_IMAGES = []  # {issue_key, jira_filename, conf_filename}

def conf_unique_img_name(issue_key, jira_filename):
    safe = re.sub(r'[^\w.\-]+', '_', os.path.basename(jira_filename.strip()))
    return f"wl_{issue_key}_{safe}"

def queue_worklog_image(issue_key, jira_filename):
    if not issue_key or not jira_filename:
        return None
    conf_name = conf_unique_img_name(issue_key, jira_filename)
    entry = {
        "issue_key": issue_key,
        "jira_filename": os.path.basename(jira_filename.strip()),
        "conf_filename": conf_name,
    }
    if not any(
        e["issue_key"] == entry["issue_key"] and e["jira_filename"] == entry["jira_filename"]
        for e in PENDING_CONF_IMAGES
    ):
        PENDING_CONF_IMAGES.append(entry)
    return conf_name

def _strip_brackets_keep_img(text):
    """移除一般 [ ]，保留 [[IMG:...]] 標記。"""
    placeholders = {}

    def _stash(m):
        key = f"\x00IMG{len(placeholders)}\x00"
        placeholders[key] = m.group(0)
        return key

    protected = IMG_MARKER_RE.sub(_stash, text)
    protected = protected.replace("[", "").replace("]", "")
    for key, value in placeholders.items():
        protected = protected.replace(key, value)
    return protected


def _apply_newline_delimiters(text):
    """依設定將分隔符換成換行，且不動 [[IMG:...]] 內容。"""
    delimiters = [d for d in SETTINGS.get("newline_chars", "; ,").split(" ") if d]
    if not delimiters:
        return text
    parts = IMG_MARKER_RE.split(text)
    rebuilt = []
    for i, chunk in enumerate(parts):
        if i % 2 == 0:
            for dlim in delimiters:
                chunk = chunk.replace(dlim, "\n")
            rebuilt.append(chunk)
        else:
            rebuilt.append(f"[[IMG:{chunk}]]")
    return "".join(rebuilt)


def append_wysiwyg_comment(
    soup,
    parent_tag,
    comment_text,
    color_style="",
    issue_key=None,
    bg_color="#ffffff",
    hang_prefix="└ 📝 ",
    left_gutter="--------",
):
    """以原始 WeeklyReport 格式寫入備註（單一 span + br，支援 [[IMG:...]]）。
    續行會加上與首行文字對齊的隱形縮排（left_gutter + hang_prefix）。
    """
    if comment_text is None:
        comment_text = ""
    text = str(comment_text).replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_brackets_keep_img(text)

    comment_span = soup.new_tag("span", style=color_style or "color: #555555;")

    def _append_image(container, jira_fn):
        conf_fn = queue_worklog_image(issue_key, jira_fn) if issue_key else None
        if conf_fn:
            img = soup.new_tag("ac:image", **{"ac:width": "640"})
            ri = soup.new_tag("ri:attachment", **{"ri:filename": conf_fn})
            img.append(ri)
            container.append(img)
        else:
            container.append(soup.new_string(f"[[IMG:{jira_fn}]]"))

    def _append_linked_text(container, chunk):
        if not chunk:
            return
        parts = re.split(r"(https?://[^\s]+)", chunk)
        for part in parts:
            if part.startswith("http"):
                a_tag = soup.new_tag(
                    "a",
                    href=part,
                    target="_blank",
                    style="color: #2980b9; text-decoration: none;",
                )
                a_tag.string = part
                container.append(a_tag)
            elif part:
                container.append(soup.new_string(part))

    def _append_line_body(container, line):
        """單行內容：純 IMG 標記轉為圖片，其餘混排連結／文字。"""
        only_img = IMG_MARKER_RE.fullmatch(line.strip())
        if only_img:
            _append_image(container, only_img.group(1).strip())
            return
        pos = 0
        for m in IMG_MARKER_RE.finditer(line):
            _append_linked_text(container, line[pos:m.start()])
            _append_image(container, m.group(1).strip())
            pos = m.end()
        _append_linked_text(container, line[pos:])

    def _append_hang_break():
        # 換行後對齊到首行「文字」起點（與 └ 📝 後相同位置），勿另開 noformat 區塊
        comment_span.append(soup.new_tag("br"))
        align_spacer = soup.new_tag("span", style=f"color: {bg_color}; user-select: none;")
        align_spacer.string = f"{left_gutter}{hang_prefix}"
        comment_span.append(align_spacer)

    if SETTINGS.get("enable_newline"):
        text = _apply_newline_delimiters(text)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        seen_urls = set()
        final_lines = []
        for line in lines:
            urls_in_line = re.findall(r"(https?://[^\s]+)", line)
            if len(urls_in_line) == 1 and line == urls_in_line[0] and line in seen_urls:
                continue
            seen_urls.update(urls_in_line)
            final_lines.append(line)

        for idx, line in enumerate(final_lines):
            _append_line_body(comment_span, line)
            if idx < len(final_lines) - 1:
                _append_hang_break()
    else:
        flat = text.replace("\n", " ").replace("\r", "")
        _append_line_body(comment_span, flat)

    parent_tag.append(comment_span)

def is_image_attachment(att):
    mime = (att.get("mimeType") or "").lower()
    name = (att.get("filename") or "").lower()
    if mime.startswith("image/"):
        return True
    return any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"))

def attachment_created_date_tz8(att):
    created = att.get("created") or ""
    dt = parse_jira_date_to_dt(created)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return created[:10] if created else ""

def list_day_image_filenames(issue_obj_or_atts, day_str, exclude_names=None):
    """依附件建立日期（台北）篩出該日圖片檔名；exclude 避免與 [[IMG:]] 重複。"""
    exclude = {n.lower() for n in (exclude_names or [])}
    if isinstance(issue_obj_or_atts, dict) and "fields" in issue_obj_or_atts:
        attachments = (issue_obj_or_atts.get("fields") or {}).get("attachment") or []
    elif isinstance(issue_obj_or_atts, list):
        attachments = issue_obj_or_atts
    else:
        attachments = []
    result = []
    for att in attachments:
        if not is_image_attachment(att):
            continue
        fn = att.get("filename") or ""
        if not fn or fn.lower() in exclude:
            continue
        if attachment_created_date_tz8(att) == day_str:
            result.append(fn)
            exclude.add(fn.lower())
    return result

def append_day_attachment_images(
    soup, parent_tag, issue_key, filenames, color_style="", bg_color="#ffffff",
    hang_prefix="└ 📝 ", left_gutter="--------",
):
    """把指定檔名圖片接在備註後方（延續同縮排）。"""
    if not issue_key or not filenames:
        return
    for fn in filenames:
        parent_tag.append(soup.new_tag("br"))
        spacer = soup.new_tag("span", style=f"color: {bg_color}; user-select: none;")
        spacer.string = f"{left_gutter}{hang_prefix}"
        parent_tag.append(spacer)
        conf_fn = queue_worklog_image(issue_key, fn)
        if conf_fn:
            img = soup.new_tag("ac:image", **{"ac:width": "640"})
            ri = soup.new_tag("ri:attachment", **{"ri:filename": conf_fn})
            img.append(ri)
            parent_tag.append(img)
        else:
            span = soup.new_tag("span", style=color_style or "color: #555555;")
            span.string = f"[[IMG:{fn}]]"
            parent_tag.append(span)

def upload_pending_images_to_confluence(page_id):
    """從 Jira Issue 附件下載並上傳到 Confluence 頁面。"""
    if not PENDING_CONF_IMAGES:
        return
    print(f"📷 準備同步 {len(PENDING_CONF_IMAGES)} 張 worklog 圖片至 Confluence...")
    attach_url = f"{JIRA_URL}/wiki/rest/api/content/{page_id}/child/attachment"
    headers = {"X-Atlassian-Token": "no-check"}

    for entry in PENDING_CONF_IMAGES:
        issue_key = entry["issue_key"]
        jira_fn = entry["jira_filename"]
        conf_fn = entry["conf_filename"]
        try:
            iss_res = requests.get(
                f"{JIRA_URL}/rest/api/2/issue/{issue_key}?fields=attachment",
                auth=ADMIN_AUTH, timeout=20,
            )
            if iss_res.status_code != 200:
                print(f"  ⚠️ 無法讀取 {issue_key} 附件清單: {iss_res.status_code}")
                continue
            attachments = iss_res.json().get("fields", {}).get("attachment", []) or []
            matched = next(
                (a for a in attachments if a.get("filename", "").lower() == jira_fn.lower()),
                None,
            )
            if not matched:
                print(f"  ⚠️ {issue_key} 找不到附件 {jira_fn}")
                continue
            bin_res = requests.get(matched["content"], auth=ADMIN_AUTH, timeout=60)
            if bin_res.status_code != 200:
                print(f"  ⚠️ 下載失敗 {jira_fn}: {bin_res.status_code}")
                continue
            mime = matched.get("mimeType") or "application/octet-stream"
            files = {"file": (conf_fn, bin_res.content, mime)}
            up = requests.post(attach_url, auth=ADMIN_AUTH, headers=headers, files=files, timeout=60)
            if up.status_code in (200, 201):
                print(f"  ✅ 已上傳 {conf_fn}")
            else:
                # 檔名可能已存在：嘗試更新既有附件
                try:
                    existing = requests.get(
                        f"{attach_url}?filename={conf_fn}",
                        auth=ADMIN_AUTH, timeout=20,
                    )
                    results = (existing.json() or {}).get("results", []) if existing.status_code == 200 else []
                    if results:
                        att_id = results[0]["id"]
                        upd = requests.post(
                            f"{JIRA_URL}/wiki/rest/api/content/{page_id}/child/attachment/{att_id}/data",
                            auth=ADMIN_AUTH, headers=headers, files=files, timeout=60,
                        )
                        if upd.status_code in (200, 201):
                            print(f"  ✅ 已更新既有附件 {conf_fn}")
                        else:
                            print(f"  ⚠️ 更新附件失敗 {conf_fn}: {upd.status_code} {upd.text[:200]}")
                    else:
                        print(f"  ⚠️ 上傳失敗 {conf_fn}: {up.status_code} {up.text[:200]}")
                except Exception as e2:
                    print(f"  ⚠️ 上傳/更新例外 {conf_fn}: {e2}")
        except Exception as e:
            print(f"  ⚠️ 圖片同步失敗 {issue_key}/{jira_fn}: {e}")

    PENDING_CONF_IMAGES.clear()

# --- 3. 核心輔助函式 ---
EMOJI_MAP = {
    "TO DO": "📋", "待辦事項": "📋", "IN PROGRESS": "🔄", "進行中": "🔄",
    "DONE": "✅", "完成": "✅", "BLOCKED": "🛑", "CANDIDATE": "🎯",
    "RESUME": "▶️", "WAITING": "⏳", "等待中": "⏳", "ABORT": "❌"
}

def get_emoji(status_str):
    if not status_str: return "🔸"
    return EMOJI_MAP.get(status_str.strip().upper(), "🔸") 

def translate_status(status_str):
    if not status_str: return "NA"
    s = status_str.upper()
    if s == "IN PROGRESS": return "進行中"
    if s == "DONE": return "完成"
    if s == "TO DO": return "待辦事項"
    if s == "WAITING": return "Waiting"
    return status_str

def parse_jira_date_to_tz8(jira_time_str):
    if not jira_time_str: return ""
    try:
        dt = datetime.strptime(jira_time_str, "%Y-%m-%dT%H:%M:%S.%f%z")
        dt_tz8 = dt.astimezone(timezone(timedelta(hours=8)))
        return dt_tz8.strftime("%Y-%m-%d")
    except Exception:
        return jira_time_str[:10]

def parse_jira_date_to_dt(jira_time_str):
    if not jira_time_str: return None
    try:
        clean_str = re.sub(r'\.\d+', '', jira_time_str)
        dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(timezone(timedelta(hours=8)))
    except Exception:
        try:
            return datetime.strptime(jira_time_str[:10], "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
        except:
            return None

def parse_duration_to_minutes(dur_str):
    if not dur_str or dur_str == "-": return 0
    mins = 0
    parts = re.findall(r'(\d+)([wdhm])', dur_str.lower())
    for val, unit in parts:
        v = int(val)
        if unit == 'w': mins += v * 5 * 8 * 60
        elif unit == 'd': mins += v * 8 * 60
        elif unit == 'h': mins += v * 60
        elif unit == 'm': mins += v
    return mins

def format_duration(total_mins):
    if total_mins == 0: return "0m"
    h = total_mins // 60
    m = total_mins % 60
    if h > 0 and m > 0: return f"{h}h{m}m"
    elif h > 0: return f"{h}h"
    else: return f"{m}m"

def format_total_duration(total_mins):
    if total_mins == 0: return "0h:0m"
    h = total_mins // 60
    m = total_mins % 60
    return f"{h}h:{m}m"

def adjust_duration_for_lunch(start_dt, duration_mins):
    if duration_mins <= 0: return 0
    end_dt = start_dt + timedelta(minutes=duration_mins)
    lunch_start = start_dt.replace(hour=12, minute=0, second=0, microsecond=0)
    lunch_end = start_dt.replace(hour=13, minute=0, second=0, microsecond=0)
    overlap_start = max(start_dt, lunch_start)
    overlap_end = min(end_dt, lunch_end)
    if overlap_start < overlap_end:
        overlap_mins = (overlap_end - overlap_start).total_seconds() / 60
        return int(duration_mins - overlap_mins)
    return int(duration_mins)

def format_due_date(date_str):
    if not date_str: return '"Due TBD"', None
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return f'"Due {dt.strftime("%y")}\' {dt.month}/{dt.day}"', dt
    except: return '"Due TBD"', None

def is_workday(date_obj):
    date_str = date_obj.strftime("%Y-%m-%d")
    if date_str in SETTINGS.get("custom_makeup_days", []) or date_str in TW_MAKEUP_WORKDAYS: 
        return True
    if date_obj.weekday() >= 5: 
        return False
    if date_str in SETTINGS.get("custom_holidays", []) or date_str in TW_HOLIDAYS: 
        return False
    return True

def calculate_working_days(start_date, end_date):
    start = start_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    end = end_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if start == end: return 0
    step = timedelta(days=1) if start < end else timedelta(days=-1)
    current = start + step
    working_days = 0
    while True:
        if is_workday(current): working_days += 1
        if current == end: break
        current += step
    return working_days if start < end else -working_days

# --- 4. 核心邏輯引擎 ---
def get_selected_dates():
    now_tpe = datetime.now(timezone(timedelta(hours=8)))
    
    if SETTINGS.get("day_yesterday"): 
        curr = now_tpe - timedelta(days=1)
        while not is_workday(curr):
            curr -= timedelta(days=1)
        return [curr]

    if SETTINGS.get("day_auto"):
        weekday = now_tpe.weekday()
        SETTINGS.config.update({
            "day_mon": weekday >= 0, "day_tue": weekday >= 1,
            "day_wed": weekday >= 2, "day_thu": weekday >= 3,
            "day_fri": weekday >= 4
        })
            
    weekday = now_tpe.weekday()
    monday = now_tpe - timedelta(days=weekday)
    dates = []
    if SETTINGS.get("day_mon"): dates.append(monday)
    if SETTINGS.get("day_tue"): dates.append(monday + timedelta(days=1))
    if SETTINGS.get("day_wed"): dates.append(monday + timedelta(days=2))
    if SETTINGS.get("day_thu"): dates.append(monday + timedelta(days=3))
    if SETTINGS.get("day_fri"): dates.append(monday + timedelta(days=4))
    return dates

def get_target_report_title(sample_date):
    days_ahead = 4 - sample_date.weekday()
    target_date = sample_date + timedelta(days=days_ahead)
    return f"WeeklyReport_{target_date.strftime('%Y%m%d')}"

_account_id_cache = {}
def get_account_id(email, name):
    if name in _account_id_cache: return _account_id_cache[name]
    if email:
        try:
            res = requests.get(f"{JIRA_URL}/rest/api/3/user/search?query={email}", auth=ADMIN_AUTH, timeout=5)
            if res.status_code == 200 and len(res.json()) > 0:
                _account_id_cache[name] = res.json()[0]['accountId']
                return _account_id_cache[name]
        except: pass
    try:
        clean_name = name.split(" (")[0].strip()
        res = requests.get(f"{JIRA_URL}/rest/api/3/user/search?query={clean_name}", auth=ADMIN_AUTH, timeout=5)
        if res.status_code == 200 and len(res.json()) > 0:
            _account_id_cache[name] = res.json()[0]['accountId']
            return _account_id_cache[name]
    except: pass
    return None

def fetch_all_recent_issues(min_date):
    min_date_str = min_date.strftime("%Y-%m-%d")
    jql = f'updated >= "{min_date_str}" ORDER BY updated DESC'
    all_issues = []
    
    next_page_token = None 
    
    while True:
        payload = {
            "jql": jql, 
            "maxResults": 100, 
            "fields": ["summary", "status", "project", "parent", "labels", "worklog", "assignee", "duedate", "timetracking", "updated", "attachment"],
            "expand": "changelog" 
        }
        
        if next_page_token: payload["nextPageToken"] = next_page_token
            
        try:
            res = requests.post(f"{JIRA_URL}/rest/api/3/search/jql", json=payload, auth=ADMIN_AUTH, timeout=20)
            res.raise_for_status()
            data = res.json()
            
            issues = data.get('issues', [])
            all_issues.extend(issues)
            
            next_page_token = data.get('nextPageToken')
            if not next_page_token or not issues: break
                
        except Exception as e:
            print(f"    ❌ 全域掃描 API 請求失敗: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"    📄 錯誤詳情: {e.response.text}")
            break
            
    print(f"  └ 基礎資料掃描完畢，共 {len(all_issues)} 筆。正在執行「深度預載快取」...")
        
    for issue in all_issues:
        key = issue['key']
        
        wl_data = issue['fields'].get('worklog', {})
        if wl_data.get('total', 0) > len(wl_data.get('worklogs', [])):
            try:
                res = requests.get(f"{JIRA_URL}/rest/api/2/issue/{key}/worklog", auth=ADMIN_AUTH, timeout=10)
                if res.status_code == 200:
                    issue['fields']['worklog']['worklogs'] = res.json().get('worklogs', [])
            except: pass
            
        cl_data = issue.get('changelog', {})
        histories = cl_data.get('histories', [])
        total_cl = cl_data.get('total', len(histories))
        if total_cl > len(histories) or not histories:
            try:
                res = requests.get(f"{JIRA_URL}/rest/api/3/issue/{key}/changelog", auth=ADMIN_AUTH, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    total_real = data.get('total', 0)
                    if total_real > 100:
                        res = requests.get(f"{JIRA_URL}/rest/api/3/issue/{key}/changelog?startAt={total_real-100}&maxResults=100", auth=ADMIN_AUTH, timeout=10)
                        data = res.json()
                    issue['changelog'] = {'histories': data.get('values', [])}
            except: pass
            
    print(f"  └ ⚡ 深度快取完成！後續掃描將以「純本地運算」極速執行。")
    return all_issues

_remote_links_cache = {}
def get_remote_links(key):
    if key in _remote_links_cache: return _remote_links_cache[key]
    links = []
    try:
        res = requests.get(f"{JIRA_URL}/rest/api/3/issue/{key}/remotelink", auth=ADMIN_AUTH, timeout=5)
        if res.status_code == 200:
            for rlink in res.json():
                app_name = rlink.get('application', {}).get('name', '').lower()
                obj = rlink.get('object', {})
                url = obj.get('url', '')
                title = obj.get('title', 'Confluence Page')
                if 'confluence' in app_name or '/wiki/' in url:
                    if 'weeklyreport' not in title.lower() and 'weeklyreport' not in url.lower():
                        links.append({'url': url, 'title': title})
    except: pass
    _remote_links_cache[key] = links
    return links

def fetch_pending_tasks(account_id, updated_keys, target_date):
    if not account_id: return [], [], [], [], [], [], []
    jql = f'assignee = "{account_id}" AND resolution = Unresolved'
    payload = {"jql": jql, "maxResults": 100, "fields": ["summary", "status", "project", "duedate", "created", "worklog", "comment", "parent", "attachment"]}
    
    pending_in_progress, pending_waiting, pending_todo, pending_candidate = [], [], [], []
    pending_blocked, pending_abort, pending_resume = [], [], []
    
    try:
        res = requests.post(f"{JIRA_URL}/rest/api/3/search/jql", json=payload, auth=ADMIN_AUTH, timeout=10)
        if res.status_code == 200:
            issues = res.json().get('issues', [])
            exclude_kws = [kw.strip().lower() for kw in SETTINGS.get("exclude_keywords", "").split(',') if kw.strip()]
            
            def preload_issue_details(issue):
                key = issue.get('key')
                if not key: return
                fields = issue.get('fields') or {}
                wl_data = fields.get('worklog') or {}
                if wl_data.get('total', 0) > len(wl_data.get('worklogs', [])):
                    try:
                        r = requests.get(f"{JIRA_URL}/rest/api/2/issue/{key}/worklog", auth=ADMIN_AUTH, timeout=10)
                        if r.status_code == 200: issue['fields']['worklog']['worklogs'] = r.json().get('worklogs', [])
                    except: pass
                c_data = fields.get('comment') or {}
                if c_data.get('total', 0) > len(c_data.get('comments', [])):
                    try:
                        r = requests.get(f"{JIRA_URL}/rest/api/2/issue/{key}/comment", auth=ADMIN_AUTH, timeout=10)
                        if r.status_code == 200: issue['fields']['comment']['comments'] = r.json().get('comments', [])
                    except: pass
            
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(preload_issue_details, [iss for iss in issues if iss.get('key') not in updated_keys]))

            for issue in issues:
                key = issue['key']
                fields = issue.get('fields') or {}
                summary_text = fields.get('summary', 'NA')
                project_text = fields.get('project', {}).get('name', 'NA')
                
                parent_data = fields.get('parent') or {}
                parent_key = parent_data.get('key')
                
                if key not in updated_keys:
                    is_excluded = any(kw in summary_text.lower() or kw in project_text.lower() for kw in exclude_kws)
                    if is_excluded: continue
                        
                    status_str = fields.get('status', {}).get('name', 'NA')
                    duedate_str, duedate_dt = format_due_date(fields.get('duedate'))
                    confluence_links = get_remote_links(key) if SETTINGS.get("show_confluence_links") else []

                    created_str = fields.get('created')
                    last_active_dt = parse_jira_date_to_dt(created_str)

                    worklogs = (fields.get('worklog') or {}).get('worklogs', [])
                    for wl in worklogs:
                        wl_dt = parse_jira_date_to_dt(wl.get('started') or wl.get('updated'))
                        if wl_dt and (not last_active_dt or wl_dt > last_active_dt):
                            last_active_dt = wl_dt
                            
                    comments = (fields.get('comment') or {}).get('comments', [])
                    for cmt in comments:
                        c_dt = parse_jira_date_to_dt(cmt.get('created') or cmt.get('updated'))
                        if c_dt and (not last_active_dt or c_dt > last_active_dt):
                            last_active_dt = c_dt
                            
                    stagnant_days = 0
                    if last_active_dt:
                        stagnant_days = calculate_working_days(last_active_dt, target_date)
                        if stagnant_days < 0: stagnant_days = 0

                    task_data = {
                        "key": key, "summary": summary_text, "status": status_str,
                        "project": project_text, "parent_key": parent_key, "duedate": duedate_str, "duedate_dt": duedate_dt,
                        "confluence_links": confluence_links, "updated_dt": last_active_dt, "stagnant_days": stagnant_days
                    }
                    
                    status_upper = status_str.upper()
                    if status_upper in ["WAITING", "等待中"] or "WAIT" in status_upper: pending_waiting.append(task_data)
                    elif status_upper in ["CANDIDATE"]: pending_candidate.append(task_data)
                    elif status_upper in ["TO DO", "待辦事項", "OPEN", "NEW"]: pending_todo.append(task_data)
                    elif status_upper in ["BLOCKED"]: pending_blocked.append(task_data)
                    elif status_upper in ["ABORT"]: pending_abort.append(task_data)
                    elif status_upper in ["RESUME"]: pending_resume.append(task_data)
                    elif status_upper in ["IN PROGRESS", "進行中"] or "PROGRESS" in status_upper: pending_in_progress.append(task_data)
                        
            for task_list in [pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume]:
                task_list.sort(key=lambda x: (
                    1 if x.get('project', '').upper() == 'MEETING' else 0,
                    x.get('project', ''),
                    x.get('stagnant_days', 0),
                    x.get('key', '')
                ))
        
            return pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume
    except: pass
    return [], [], [], [], [], [], []

def get_user_status_transition_for_day(key, account_id, email, target_date_str, issue_data):
    day_status_changes = []
    histories = issue_data.get('changelog', {}).get('histories', [])

    for history in histories:
        created_date_tz8 = parse_jira_date_to_tz8(history.get('created', ''))
        author_id = history.get('author', {}).get('accountId')
        author_email = history.get('author', {}).get('emailAddress', '').lower()
        is_author = (account_id and author_id == account_id) or (email and author_email == email.lower())

        if created_date_tz8 == target_date_str and is_author:
            for item in history.get('items', []):
                if item.get('field', '').lower() == 'status':
                    day_status_changes.append((item.get('fromString', ''), item.get('toString', '')))

    if day_status_changes:
        first_trans = translate_status(day_status_changes[0][0])
        last_trans = translate_status(day_status_changes[-1][1])
        if SETTINGS.get("hide_duplicate_transition") and first_trans == last_trans: return ""
        return f"{get_emoji(day_status_changes[0][0])}[{first_trans}] ➜ {get_emoji(day_status_changes[-1][1])}[{last_trans}]"
    return ""

def extract_logs_from_issues(name, email, account_id, target_dates_list, all_issues):
    target_date_strs = [d.strftime("%Y-%m-%d") for d in target_dates_list]
    target_shorts = [d.strftime("%m/%d").lstrip("0").replace("/0", "/") for d in target_dates_list]
    collected_logs = []
    
    exclude_kws = [kw.strip().lower() for kw in SETTINGS.get("exclude_keywords", "").split(',') if kw.strip()]
    
    for issue in all_issues:
        key = issue['key']
        fields = issue['fields']
        
        summary = fields.get('summary', 'NA')
        project_name = fields.get('project', {}).get('name', 'NA')
        
        if any(kw in summary.lower() or kw in project_name.lower() for kw in exclude_kws): continue
            
        parent_data = fields.get('parent') or {}
        parent = (parent_data.get('fields') or {}).get('summary', 'NA')
        parent_key = parent_data.get('key')
        
        if isinstance(parent, str) and parent != "NA":
            parent = re.sub(r'([a-zA-Z0-9])\s*NPI', r'\1 - NPI', parent, flags=re.IGNORECASE)
            
        labels = fields.get('labels', [])
        label_str = labels[0] if labels else "NA"
        current_status = fields.get('status', {}).get('name', 'NA')
        duedate_str, duedate_dt = format_due_date(fields.get('duedate'))
        
        issue_total_sec = fields.get('timetracking', {}).get('timeSpentSeconds', 0)
        issue_total_str = format_total_duration(issue_total_sec // 60)

        user_worklogs = []
        worklogs_data = fields.get('worklog', {}).get('worklogs', [])

        for wl in worklogs_data:
            author_id = wl.get('author', {}).get('accountId')
            author_email = wl.get('author', {}).get('emailAddress', '').lower()
            if not ((account_id and author_id == account_id) or (email and author_email == email.lower())): continue

            comment_raw = wl.get('comment', 'NA')
            if isinstance(comment_raw, dict): 
                comment_texts = []
                def ex(n):
                    if n.get('type') == 'text': comment_texts.append(n.get('text', ''))
                    elif n.get('type') == 'hardBreak': comment_texts.append('\n')
                    for child in n.get('content', []): ex(child)
                    if n.get('type') in ['paragraph', 'listItem']:
                        comment_texts.append('\n')
                ex(comment_raw)
                comment_text = "".join(comment_texts).strip()
            else: comment_text = str(comment_raw).strip() if comment_raw else "NA"
            if not comment_text: comment_text = "NA"

            raw_started = wl.get('started', '')
            start_dt_tz8 = parse_jira_date_to_dt(raw_started)
            started_date_tz8 = start_dt_tz8.strftime("%Y-%m-%d") if start_dt_tz8 else raw_started[:10]

            is_target_day = (started_date_tz8 in target_date_strs)
            has_date_prefix = any(comment_text.startswith(ts) for ts in target_shorts)

            is_valid = False
            if SETTINGS.get("filter_started") and is_target_day: is_valid = True
            if SETTINGS.get("filter_comment") and has_date_prefix: is_valid = True

            if is_valid:
                raw_mins = parse_duration_to_minutes(wl.get('timeSpent', '0m'))
                adjusted_mins = adjust_duration_for_lunch(start_dt_tz8, raw_mins) if start_dt_tz8 else raw_mins
                user_worklogs.append({
                    "comment": comment_text, "duration": format_duration(adjusted_mins), 
                    "duration_mins": adjusted_mins, "started_date": started_date_tz8
                })

        assignee_data = fields.get('assignee')
        is_assignee = assignee_data and ((assignee_data.get('accountId') == account_id) or (assignee_data.get('emailAddress', '').lower() == email.lower()))

        for target_date_str in target_date_strs:
            status_transition_str = get_user_status_transition_for_day(key, account_id, email, target_date_str, issue)
            user_changed_status = bool(status_transition_str)
            
            day_logs = [wl for wl in user_worklogs if wl.get('started_date') == target_date_str]

            if SETTINGS.get("hide_status_only") and not SETTINGS.get("style_weekly"):
                if not day_logs and user_changed_status: continue 

            # 🌟 核心防線一：過濾掉沒有工時的純狀態變更紀錄，避免跑出空日報
            if day_logs:
                confluence_links = get_remote_links(key) if SETTINGS.get("show_confluence_links") else []

                for uwl in day_logs:
                    collected_logs.append({
                        "key": key, "summary": summary, "status": current_status, "transition": status_transition_str,
                        "project": project_name, "parent": parent, "parent_key": parent_key, "label": label_str, "comment": uwl['comment'],
                        "duration": uwl['duration'], "duration_mins": uwl.get('duration_mins', 0),
                        "duedate": duedate_str, "duedate_dt": duedate_dt, "confluence_links": confluence_links,
                        "started_date": uwl['started_date'], "issue_total_str": issue_total_str
                    })
    
    collected_logs.sort(key=lambda x: (1 if x.get('project', '').upper() == 'MEETING' else 0, x.get('project', ''), 0 if x.get('status', '').upper() in ['DONE', '完成'] else 1, x.get('key', '')))
    return collected_logs

# ✅ 聚合邏輯
def enrich_with_weekly_data(base_logs, name, email, account_id, days_to_process, all_issues):
    issue_dict = {issue['key']: issue for issue in all_issues}
    enriched = []
    grouped_keys = []
    for log in base_logs:
        if log['key'] not in grouped_keys: grouped_keys.append(log['key'])
            
    for k in grouped_keys:
        issue_obj = issue_dict.get(k)
        if not issue_obj: continue
        
        base_log = next(l for l in base_logs if l['key'] == k)
        daily_days = []
        has_week_log = False
        
        for target_date in days_to_process:
            day_str = target_date.strftime("%Y-%m-%d")
            day_short = f"{target_date.month}/{target_date.day}"
            day_name = target_date.strftime("%a")
            
            trans = get_user_status_transition_for_day(k, account_id, email, day_str, issue_obj)
            
            wls = []
            for wl in issue_obj['fields'].get('worklog', {}).get('worklogs', []):
                author_id = wl.get('author', {}).get('accountId')
                author_email = wl.get('author', {}).get('emailAddress', '').lower()
                if not ((account_id and author_id == account_id) or (email and author_email == email.lower())): continue
                
                raw_started = wl.get('started', '')
                start_dt_tz8 = parse_jira_date_to_dt(raw_started)
                wl_date_str = start_dt_tz8.strftime("%Y-%m-%d") if start_dt_tz8 else raw_started[:10]
                    
                if wl_date_str == day_str:
                    comment_raw = wl.get('comment', '')
                    if isinstance(comment_raw, dict): 
                        comment_texts = []
                        def ex(n):
                            if n.get('type') == 'text': comment_texts.append(n.get('text', ''))
                            elif n.get('type') == 'hardBreak': comment_texts.append('\n')
                            for child in n.get('content', []): ex(child)
                            if n.get('type') in ['paragraph', 'listItem']:
                                comment_texts.append('\n')
                        ex(comment_raw)
                        comment_text = "".join(comment_texts).strip()
                    else: comment_text = str(comment_raw).strip() if comment_raw else ""
                        
                    raw_mins = parse_duration_to_minutes(wl.get('timeSpent', '0m'))
                    adjusted_mins = adjust_duration_for_lunch(start_dt_tz8, raw_mins) if start_dt_tz8 else raw_mins
                    wls.append({"comment": comment_text, "mins": adjusted_mins})
            
            total_mins_day = sum(w.get('mins', 0) for w in wls)
            dur_str = format_duration(total_mins_day) if total_mins_day > 0 else ""
            
            unique_comments = []
            for w in wls:
                if w['comment'] and w['comment'] not in unique_comments:
                    unique_comments.append(w['comment'])
            
            joined_comment = " / \n".join(unique_comments) if unique_comments else ""
            exclude_imgs = [m.strip() for m in IMG_MARKER_RE.findall(joined_comment)]
            day_images = list_day_image_filenames(issue_obj, day_str, exclude_names=exclude_imgs)
            
            has_log = bool(wls or trans or day_images)
            if SETTINGS.get("hide_status_only"):
                if total_mins_day == 0 and not joined_comment.strip() and not day_images:
                    has_log = False
                    
            if has_log: has_week_log = True
            
            daily_days.append({
                "date": target_date, "day_name": day_name, "day_short": day_short, "dur_str": dur_str,
                "total_mins_day": total_mins_day, "comment": joined_comment, "transition": trans, "has_log": has_log,
                "day_images": day_images,
            })
        
        if not has_week_log: continue

        new_log = base_log.copy()
        new_log['daily_days'] = daily_days
        enriched.append(new_log)
        
    return enriched

def generate_style_2_html(soup, target_date, logs, pending_in_progress=None, pending_waiting=None, pending_todo=None, pending_candidate=None, pending_blocked=None, pending_abort=None, pending_resume=None, total_mins=0, bg_color="#ffffff", update_source_tag="GitHub Update (Bob)"):
    date_str_tag = target_date.strftime("[%Y/%m/%d]")
    weekday_en = target_date.strftime("%A")
    safe_date_class = target_date.strftime("%Y%m%d")
    
    container = soup.new_tag("div", **{
        "class": f"daily-worklog-{safe_date_class}", 
        "style": "max-width: 1000px; margin-bottom: 25px;"
    })
    
    p_date = soup.new_tag("p")
    strong_date = soup.new_tag("strong")
    
    time_parts = []
    if SETTINGS.get("show_total_time"): time_parts.append(f"({format_duration(total_mins)})")
    if SETTINGS.get("show_write_time"): 
        write_time_str = datetime.now(timezone(timedelta(hours=8))).strftime(f'"{update_source_tag} %Y/%m/%d %H:%M"')
        time_parts.append(write_time_str)

    if time_parts:
        strong_date.string = f"{date_str_tag} {weekday_en} - "
        p_date.append(strong_date)
        span_time = soup.new_tag("span", style="color: gray; font-size: 80%;")
        span_time.string = " - ".join(time_parts)
        p_date.append(span_time)
    else:
        strong_date.string = f"{date_str_tag} {weekday_en}"
        p_date.append(strong_date)
        
    container.append(p_date)
    
    current_project = None
    project_box = None
    log_counter = 1

    def create_confluence_panel():
        macro = soup.new_tag("ac:structured-macro", **{"ac:name": "panel", "ac:schema-version": "1"})
        for p_name, p_val in [("borderWidth", "1"), ("borderStyle", "solid"), ("borderColor", "#000000"), ("bgColor", bg_color)]:
            param = soup.new_tag("ac:parameter", **{"ac:name": p_name})
            param.string = p_val
            macro.append(param)
        body = soup.new_tag("ac:rich-text-body")
        macro.append(body)
        return macro, body

    if logs and not SETTINGS.get("group_by_project"):
        panel_macro, project_box = create_confluence_panel()
        container.append(panel_macro)

    mode = SETTINGS.get("due_date_mode", "方案一: 標題列 (統一以本週報產出日結算)")

    for log in logs:
        if SETTINGS.get("group_by_project") and log['project'] != current_project:
            if current_project is not None:
                p_empty = soup.new_tag("p")
                p_empty.append(soup.new_tag("br"))
                container.append(p_empty)
            current_project = log['project']
            
            display_project = current_project
            if display_project.lower().startswith('prj_'): display_project = display_project[4:]
            elif display_project.lower().startswith('report_'): display_project = display_project[7:]
                
            p_proj = soup.new_tag("p", style="margin-top: 15px; margin-bottom: 8px;")
            strong_proj = soup.new_tag("strong")
            strong_proj.string = f"---- 專案: {display_project} ----"
            p_proj.append(strong_proj)
            container.append(p_proj)
            
            panel_macro, project_box = create_confluence_panel()
            container.append(panel_macro)
            log_counter = 1

        p1 = soup.new_tag("p", style="margin-top: 5px; margin-bottom: 2px;")
        num_span = soup.new_tag("span", style="color: black; font-weight: bold;")
        num_span.string = f"{log_counter}. "
        p1.append(num_span)
        
        if SETTINGS.get("use_jira_macro"):
            smart_link = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['key']}", **{"data-card-appearance": "inline"})
            smart_link.string = f"{JIRA_URL}/browse/{log['key']}"
            p1.append(smart_link)
            if SETTINGS.get("show_status"):
                status_text = f" {log['transition']}" if log.get('transition') else f" {get_emoji(log['status'])}[{translate_status(log['status'])}]"
                p1.append(soup.new_string(status_text))
        else:
            a_key = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['key']}")
            a_key.string = log['key']
            p1.append(a_key)
            if SETTINGS.get("show_status"):
                status_text = f" {log['transition']}" if log.get('transition') else f" {get_emoji(log['status'])}[{translate_status(log['status'])}]"
            else: status_text = ""
            p1.append(soup.new_string(f" : {log['summary']}{status_text}"))
        
        is_tbd = (log.get('duedate') == '"Due TBD"')
        
        if "雙管齊下" in mode:
            diff_work_days = log.get('stagnant_days', 0)
            if diff_work_days >= 3:
                warn_color = "#e67e22" if diff_work_days < 5 else ("#e74c3c" if diff_work_days < 15 else ("#c0392b" if diff_work_days < 30 else "#8e44ad"))
                icon = "⏳" if diff_work_days < 5 else ("⚠️" if diff_work_days < 15 else ("🔥" if diff_work_days < 30 else "💀"))
                bg_style = "background-color: #fadbd8; padding: 2px 4px; border-radius: 3px;" if diff_work_days >= 15 else ""
                warning_span = soup.new_tag("span", style=f"color: {warn_color}; {bg_style} font-weight: bold; margin-left: 8px;")
                warning_span.string = f"{icon}停滯 {diff_work_days} 個工作天"
                p1.append(warning_span)

        if "標題列" in mode and "雙管" not in mode:
            if SETTINGS.get("show_duedate") and log.get('duedate') and not is_tbd:
                span_due = soup.new_tag("span", style="color: gray; font-size: 90%; font-weight: bold; margin-left: 8px;")
                if log.get('parent_due_key'):
                    inherit_text = "依故事" if log.get('is_inherited_from_story') else "依父系"
                    span_due.string = f" 📅 {log['duedate']} ({inherit_text} "
                    a_parent = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['parent_due_key']}", style="color: #3498db; text-decoration: none;")
                    a_parent.string = log['parent_due_summary']
                    span_due.append(a_parent)
                    span_due.append(soup.new_string(") "))
                else:
                    span_due.string = f" 📅 {log['duedate']} "
                    
                if log.get('duedate_dt'):
                    diff_days = calculate_working_days(target_date, log['duedate_dt'])
                    color = "#2ecc71" if diff_days >= 0 else "#e74c3c"
                    sign = "+" if diff_days >= 0 else ""
                    warning_icon = "❗" if diff_days <= 2 else ""
                    span_diff = soup.new_tag("span", style=f"color: {color};")
                    span_diff.string = f" {warning_icon}({sign}{diff_days})"
                    span_due.append(span_diff)
                p1.append(span_due)

        if log.get('confluence_links'):
            for cl in log['confluence_links']:
                p1.append(soup.new_string(" "))
                a_conf = soup.new_tag("a", href=cl['url'])
                a_conf.string = f"🔗 {cl['title']}"
                p1.append(a_conf)

        if SETTINGS.get("compact_layout"):
            parts = []
            if SETTINGS.get("show_label") and log['label'] != "NA": parts.append(f"標籤: {log['label']}")
            if not SETTINGS.get("group_by_project"): parts.append(f"專案: {log['project']}")
            if SETTINGS.get("show_parent") and log['parent'] != "NA": parts.append(f"父系: {log['parent']}")
            parts_str = " | ".join(parts)
            
            if parts_str:
                span_parts = soup.new_tag("span", style="margin-left: 10px; color: #7f8c8d;")
                span_parts.string = f" {parts_str}"
                p1.append(span_parts)
            project_box.append(p1)
        else:
            project_box.append(p1)
            p2 = soup.new_tag("p", style="margin-top: 0px; margin-bottom: 2px; color: #555555;")
            
            spacer2 = soup.new_tag("span", style=f"color: {bg_color}; user-select: none;")
            spacer2.string = "----"
            p2.append(spacer2)
            
            parts = []
            if not SETTINGS.get("group_by_project"): parts.append(f"專案: {log['project']}")
            if SETTINGS.get("show_parent") and log['parent'] != "NA": parts.append(f"父系: {log['parent']}")
            parts_str = " | ".join(parts)
            
            if parts_str: p2.append(soup.new_string(f"└ " + parts_str))
            else: p2.append(soup.new_string("└ "))
            
            if SETTINGS.get("show_label") and log['label'] != "NA":
                if parts_str: p2.append(soup.new_string(" - "))
                span_label = soup.new_tag("span", style="color: #e67e22; font-size: 85%; border: 1px solid #e67e22; border-radius: 3px; padding: 0 3px;")
                span_label.string = log['label']
                p2.append(span_label)
                
            project_box.append(p2)
        
        p3 = soup.new_tag("p", style="margin-top: 0px; margin-bottom: 10px; color: #555555;")
        
        spacer3 = soup.new_tag("span", style=f"color: {bg_color}; user-select: none;")
        spacer3.string = "--------"
        p3.append(spacer3)
        
        if SETTINGS.get("show_comment"):
            dur_text = f"({log['duration']}) " if log['duration'] != "-" and log['duration'] != "0m" else ""
            comment_text = log['comment']
            comment_prefix = f"└ 📝 {dur_text}"
            p3.append(soup.new_string(comment_prefix))
            if comment_text:
                append_wysiwyg_comment(
                    soup, p3, comment_text,
                    color_style="color: #555555;",
                    issue_key=log.get('key'),
                    bg_color=bg_color,
                    hang_prefix=comment_prefix,
                    left_gutter="--------",
                )
            day_str = log.get("started_date") or ""
            exclude_imgs = [m.strip() for m in IMG_MARKER_RE.findall(comment_text or "")]
            # style2：若 log 未附 day_images，當場依 started_date 與 issue 附件補上
            day_imgs = log.get("day_images")
            if day_imgs is None and log.get("key") and day_str:
                try:
                    iss_res = requests.get(
                        f"{JIRA_URL}/rest/api/2/issue/{log['key']}?fields=attachment",
                        auth=ADMIN_AUTH, timeout=15,
                    )
                    atts = iss_res.json().get("fields", {}).get("attachment", []) if iss_res.status_code == 200 else []
                    day_imgs = list_day_image_filenames(atts, day_str, exclude_names=exclude_imgs)
                except Exception:
                    day_imgs = []
            append_day_attachment_images(
                soup, p3, log.get("key"), day_imgs or [],
                color_style="color: #555555;", bg_color=bg_color,
                hang_prefix=comment_prefix, left_gutter="--------",
            )
                        
            if SETTINGS.get("show_duedate") and log.get('duedate') and not is_tbd and ("標題列" not in mode or "雙管" in mode):
                span_due = soup.new_tag("span", style="color: gray; font-size: 50%;")
                if log.get('parent_due_key'):
                    inherit_text = "依故事" if log.get('is_inherited_from_story') else "依父系"
                    span_due.string = f" {log['duedate']} ({inherit_text} "
                    a_parent = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['parent_due_key']}", style="color: #3498db; text-decoration: none;")
                    a_parent.string = log['parent_due_summary']
                    span_due.append(a_parent)
                    span_due.append(soup.new_string(") "))
                else:
                    span_due.string = f" {log['duedate']} "
                    
                p3.append(span_due)
                
                if log.get('duedate_dt'):
                    calc_base_date = datetime.strptime(log['started_date'], "%Y-%m-%d")
                    diff_days = calculate_working_days(calc_base_date, log['duedate_dt'])
                    color = "#2ecc71" if diff_days >= 0 else "#e74c3c"
                    sign = "+" if diff_days >= 0 else ""
                    warning_icon = "❗" if diff_days <= 2 else ""
                    span_diff = soup.new_tag("span", style=f"color: {color}; font-size: 50%; margin-left: 4px;")
                    span_diff.string = f" {warning_icon}({sign}{diff_days})"
                    p3.append(span_diff)
        else:
            if log['duration'] != "-": 
                p3.append(soup.new_string(f"└ ⏱️ 耗時: {log['duration']}"))
            
        project_box.append(p3)
        log_counter += 1

    _append_pending_tasks(soup, container, pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume, target_date, bg_color=bg_color)
    return container

def generate_style_3_html(soup, target_date, selected_dates, daily_aggregated_logs, pending_in_progress=None, pending_waiting=None, pending_todo=None, pending_candidate=None, pending_blocked=None, pending_abort=None, pending_resume=None, total_mins=0, weekend_mins=0, bg_color="#ffffff", update_source_tag="GitHub Update (Bob)"):
    date_str_tag = target_date.strftime("[%Y/%m/%d]")
    weekday_en = target_date.strftime("%A")
    safe_date_class = target_date.strftime("%Y%m%d")
    
    container = soup.new_tag("div", **{
        "class": f"daily-worklog-{safe_date_class}", 
        "style": "max-width: 1000px; margin-bottom: 25px;"
    })
    
    p_date = soup.new_tag("p")
    strong_date = soup.new_tag("strong")
    time_parts = []
    
    if SETTINGS.get("show_total_time"):
        time_str = f"({format_duration(total_mins)})"
        if weekend_mins > 0:
            time_str += f" + 週末加班({format_duration(weekend_mins)})"
        time_parts.append(time_str)

    if SETTINGS.get("show_write_time"):
        write_time_str = datetime.now(timezone(timedelta(hours=8))).strftime(f'"{update_source_tag} %Y/%m/%d %H:%M"')
        time_parts.append(write_time_str)

    if time_parts:
        strong_date.string = f"{date_str_tag} {weekday_en} "
        p_date.append(strong_date)
        span_time = soup.new_tag("span", style="color: gray; font-size: 80%;")
        span_time.string = "- " + " - ".join(time_parts)
        p_date.append(span_time)
    else:
        strong_date.string = f"{date_str_tag} {weekday_en}"
        p_date.append(strong_date)
        
    container.append(p_date)
    
    current_project = None
    project_box = None
    log_counter = 1

    def create_confluence_panel():
        macro = soup.new_tag("ac:structured-macro", **{"ac:name": "panel", "ac:schema-version": "1"})
        for p_name, p_val in [("borderWidth", "1"), ("borderStyle", "solid"), ("borderColor", "#000000"), ("bgColor", bg_color)]:
            param = soup.new_tag("ac:parameter", **{"ac:name": p_name})
            param.string = p_val
            macro.append(param)
        body = soup.new_tag("ac:rich-text-body")
        macro.append(body)
        return macro, body

    if daily_aggregated_logs and not SETTINGS.get("group_by_project"):
        panel_macro, project_box = create_confluence_panel()
        container.append(panel_macro)

    mode = SETTINGS.get("due_date_mode", "方案一: 標題列 (統一以本週報產出日結算)")

    for log in daily_aggregated_logs:
        if SETTINGS.get("group_by_project") and log['project'] != current_project:
            if current_project is not None:
                p_empty = soup.new_tag("p")
                p_empty.append(soup.new_tag("br"))
                container.append(p_empty)
            current_project = log['project']
            
            display_project = current_project
            if display_project.lower().startswith('prj_'): display_project = display_project[4:]
            elif display_project.lower().startswith('report_'): display_project = display_project[7:]
                
            p_proj = soup.new_tag("p", style="margin-top: 15px; margin-bottom: 8px;")
            strong_proj = soup.new_tag("strong")
            strong_proj.string = f"---- 專案: {display_project} ----"
            p_proj.append(strong_proj)
            container.append(p_proj)
            
            panel_macro, project_box = create_confluence_panel()
            container.append(panel_macro)
            
            log_counter = 1

        if log_counter > 1:
            project_box.append(soup.new_tag("br"))

        p_header = soup.new_tag("p", style="margin-top: 5px; margin-bottom: 5px;")
        num_span = soup.new_tag("span", style="color: black; font-weight: bold;")
        num_span.string = f"{log_counter}. ☑️ "
        p_header.append(num_span)
        
        if SETTINGS.get("use_jira_macro"):
            smart_link = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['key']}", **{"data-card-appearance": "inline"})
            smart_link.string = f"{JIRA_URL}/browse/{log['key']}"
            p_header.append(smart_link)
        else:
            a_key = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['key']}")
            a_key.string = log['key']
            p_header.append(a_key)
            p_header.append(soup.new_string(f" - {log['summary']} "))

        if "標題列" in mode and "雙管" not in mode:
            if SETTINGS.get("show_duedate") and log.get('duedate') and log['duedate'] != '"Due TBD"':
                span_due = soup.new_tag("span", style="color: gray; font-size: 90%; font-weight: bold; margin-left: 8px;")
                if log.get('parent_due_key'):
                    inherit_text = "依故事" if log.get('is_inherited_from_story') else "依父系"
                    span_due.string = f" 📅 {log['duedate']} ({inherit_text} "
                    a_parent = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['parent_due_key']}", style="color: #3498db; text-decoration: none;")
                    a_parent.string = log['parent_due_summary']
                    span_due.append(a_parent)
                    span_due.append(soup.new_string(") "))
                else:
                    span_due.string = f" 📅 {log['duedate']} "
                    
                if log.get('duedate_dt'):
                    diff_days = calculate_working_days(target_date, log['duedate_dt'])
                    color = "#2ecc71" if diff_days >= 0 else "#e74c3c"
                    sign = "+" if diff_days >= 0 else ""
                    warning_icon = "❗" if diff_days <= 2 else ""
                    span_diff = soup.new_tag("span", style=f"color: {color};")
                    span_diff.string = f" {warning_icon}({sign}{diff_days})"
                    span_due.append(span_diff)
                p_header.append(span_due)

        if "雙管齊下" in mode:
            diff_work_days = log.get('stagnant_days', 0)
            if diff_work_days >= 3:
                warn_color = "#e67e22" if diff_work_days < 5 else ("#e74c3c" if diff_work_days < 15 else ("#c0392b" if diff_work_days < 30 else "#8e44ad"))
                icon = "⏳" if diff_work_days < 5 else ("⚠️" if diff_work_days < 15 else ("🔥" if diff_work_days < 30 else "💀"))
                bg_style = "background-color: #fadbd8; padding: 2px 4px; border-radius: 3px;" if diff_work_days >= 15 else ""
                warning_span = soup.new_tag("span", style=f"color: {warn_color}; {bg_style} font-weight: bold; margin-left: 8px;")
                warning_span.string = f"{icon}停滯 {diff_work_days} 個工作天"
                p_header.append(warning_span)
        
        if SETTINGS.get("show_parent") and log['parent'] != "NA":
            span_parent = soup.new_tag("span", style="color: #95a5a6; font-size: 90%;")
            span_parent.string = f" 父系: {log['parent']}"
            p_header.append(span_parent)
            
        if SETTINGS.get("show_label") and log['label'] != "NA":
            p_header.append(soup.new_string(" - "))
            span_label = soup.new_tag("span", style="color: #e67e22; font-size: 85%; border: 1px solid #e67e22; border-radius: 3px; padding: 0 3px;")
            span_label.string = log['label']
            p_header.append(span_label)
            
        if getattr(SETTINGS, 'get', lambda k: False)("show_issue_total_time") and log.get('issue_total_str'):
            p_header.append(soup.new_string(" - "))
            span_total = soup.new_tag("span", style="color: gray; font-size: 90%;")
            span_total.string = f'"Total: {log["issue_total_str"]}"'
            p_header.append(span_total)

        if log.get('confluence_links'):
            for cl in log['confluence_links']:
                p_header.append(soup.new_string(" "))
                a_conf = soup.new_tag("a", href=cl['url'])
                a_conf.string = f"🔗 {cl['title']}"
                p_header.append(a_conf)

        project_box.append(p_header)
        
        days_to_render = log['daily_days']
        if SETTINGS.get("sort_desc"):
            days_to_render = list(reversed(days_to_render))
        
        for d_info in days_to_render:
            
            if not d_info['has_log']: continue
                
            div_row = soup.new_tag("div", style="margin-bottom: 12px;")

            p_meta = soup.new_tag("p", style="margin-top: 0px; margin-bottom: 2px; color: #555555;")
            
            spacer_meta = soup.new_tag("span", style=f"color: {bg_color}; user-select: none;")
            spacer_meta.string = "----"
            p_meta.append(spacer_meta)
            
            dur_text = f"({d_info['dur_str']}) " if d_info['dur_str'] else ""
            p_meta.append(soup.new_string(f"{d_info['day_short']} {d_info['day_name']} {dur_text}"))

            is_tbd = (log.get('duedate') == '"Due TBD"')
            if SETTINGS.get("show_duedate") and log.get('duedate') and not is_tbd and ("標題列" not in mode or "雙管" in mode):
                span_due = soup.new_tag("span", style="color: gray; font-size: 50%;")
                if log.get('parent_due_key'):
                    inherit_text = "依故事" if log.get('is_inherited_from_story') else "依父系"
                    span_due.string = f" {log['duedate']} ({inherit_text} "
                    a_parent = soup.new_tag("a", href=f"{JIRA_URL}/browse/{log['parent_due_key']}", style="color: #3498db; text-decoration: none;")
                    a_parent.string = log['parent_due_summary']
                    span_due.append(a_parent)
                    span_due.append(soup.new_string(") "))
                else:
                    span_due.string = f" {log['duedate']} "
                    
                p_meta.append(span_due)
                
                if log.get('duedate_dt'):
                    calc_base_date = target_date if ("標題列" in mode and "雙管" not in mode) else d_info['date']
                    diff_days = calculate_working_days(calc_base_date, log['duedate_dt'])
                    color = "#2ecc71" if diff_days >= 0 else "#e74c3c"
                    sign = "+" if diff_days >= 0 else ""
                    warning_icon = "❗" if diff_days <= 2 else ""
                    span_diff = soup.new_tag("span", style=f"color: {color}; font-size: 50%; margin-left: 4px;")
                    span_diff.string = f" {warning_icon}({sign}{diff_days})"
                    p_meta.append(span_diff)

            trans_text = d_info['transition'] if d_info['transition'] else f"{get_emoji(log['status'])}[{translate_status(log['status'])}]"
            p_meta.append(soup.new_string(trans_text))
            div_row.append(p_meta)

            if SETTINGS.get("show_comment"):
                p_comment = soup.new_tag("p", style="margin-top: 0px; margin-bottom: 0px; color: #555555;")
                
                spacer_comment = soup.new_tag("span", style=f"color: {bg_color}; user-select: none;")
                spacer_comment.string = "--------"
                p_comment.append(spacer_comment)
                
                is_target = d_info['date'].date() in [sd.date() for sd in selected_dates]
                color_style = "color: #e74c3c; font-weight: bold;" if is_target else "color: #555555;"

                comment_prefix = "└ 📝 "
                p_comment.append(soup.new_string(comment_prefix))
                
                comment_text = d_info['comment']
                day_imgs = d_info.get("day_images") or []
                if not comment_text:
                    if d_info.get('total_mins_day', 0) > 0:
                        comment_text = "(無填寫工作日誌)"
                    elif d_info.get('transition'):
                        comment_text = "(僅狀態改變)"
                    elif day_imgs:
                        comment_text = "(附件圖片)"
                    else:
                        comment_text = "(無紀錄)"
                    comment_span = soup.new_tag("span", style=color_style)
                    comment_span.string = comment_text
                    p_comment.append(comment_span)
                else:
                    append_wysiwyg_comment(
                        soup, p_comment, comment_text,
                        color_style=color_style,
                        issue_key=log.get('key'),
                        bg_color=bg_color,
                        hang_prefix=comment_prefix,
                        left_gutter="--------",
                    )
                append_day_attachment_images(
                    soup, p_comment, log.get("key"), day_imgs,
                    color_style=color_style, bg_color=bg_color,
                    hang_prefix=comment_prefix, left_gutter="--------",
                )
                    
                div_row.append(p_comment)

            project_box.append(div_row)
            
        log_counter += 1

    _append_pending_tasks(soup, container, pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume, target_date, bg_color=bg_color)
    return container

def _append_pending_tasks(soup, container, pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume, target_date, bg_color="#ffffff"):
    has_any_pending = bool((SETTINGS.get("show_pending_inprogress") and pending_in_progress) or
                           (SETTINGS.get("show_pending_waiting") and pending_waiting) or
                           (SETTINGS.get("show_pending_todo") and pending_todo) or
                           (SETTINGS.get("show_pending_candidate") and pending_candidate) or
                           (SETTINGS.get("show_pending_blocked") and pending_blocked) or
                           (SETTINGS.get("show_pending_abort") and pending_abort) or
                           (SETTINGS.get("show_pending_resume") and pending_resume))

    if has_any_pending:
        p_spacer_before_pending = soup.new_tag("p")
        p_spacer_before_pending.append(soup.new_tag("br"))
        container.append(p_spacer_before_pending)

    rendered_pending_sections = [0]

    def create_confluence_panel():
        macro = soup.new_tag("ac:structured-macro", **{"ac:name": "panel", "ac:schema-version": "1"})
        for p_name, p_val in [("borderWidth", "1"), ("borderStyle", "solid"), ("borderColor", "#000000"), ("bgColor", bg_color)]:
            param = soup.new_tag("ac:parameter", **{"ac:name": p_name})
            param.string = p_val
            macro.append(param)
        body = soup.new_tag("ac:rich-text-body")
        macro.append(body)
        return macro, body

    def append_ptasks(task_list, title_text, title_color):
        if not task_list: return
        if rendered_pending_sections[0] > 0:
            container.append(soup.new_tag("br"))

        p_divider = soup.new_tag("p", style=f"margin-top: 10px; margin-bottom: 8px; font-weight: bold; color: {title_color};")
        p_divider.string = title_text
        container.append(p_divider)

        panel_macro, pending_box = create_confluence_panel()
        container.append(panel_macro)

        task_counter = 1
        last_project = None
        
        for pl in task_list:
            current_project = pl.get('project', '')
            if last_project is not None and current_project != last_project:
                hr_tag = soup.new_tag("hr", style="border: 0; height: 1px; background-color: #bdc3c7; margin: 12px 0;")
                pending_box.append(hr_tag)
            last_project = current_project
            
            p_pend = soup.new_tag("p", style="margin-top: 4px; margin-bottom: 4px; color: #7f8c8d;")
            num_span = soup.new_tag("span", style="color: black; font-weight: bold;")
            num_span.string = f"{task_counter}. "
            p_pend.append(num_span)
            p_pend.append(soup.new_string(f"[{pl['project']}] "))
            
            if SETTINGS.get("use_jira_macro"):
                smart_link = soup.new_tag("a", href=f"{JIRA_URL}/browse/{pl['key']}", **{"data-card-appearance": "inline"})
                smart_link.string = f"{JIRA_URL}/browse/{pl['key']}"
                p_pend.append(smart_link)
                p_pend.append(soup.new_string(f" {get_emoji(pl['status'])}[{translate_status(pl['status'])}]"))
            else:
                a_key = soup.new_tag("a", href=f"{JIRA_URL}/browse/{pl['key']}")
                a_key.string = pl['key']
                p_pend.append(a_key)
                p_pend.append(soup.new_string(f" : {pl['summary']} {get_emoji(pl['status'])}[{translate_status(pl['status'])}]"))

            if pl.get('updated_dt'):
                diff_work_days = pl.get('stagnant_days', 0)
                
                if diff_work_days >= 30:
                    warning_span = soup.new_tag("span", style="color: #8e44ad; font-weight: bold; margin-left: 8px;")
                    warning_span.string = f"💀停滯 {diff_work_days} 個工作天"
                    p_pend.append(warning_span)
                elif diff_work_days >= 15:
                    warning_span = soup.new_tag("span", style="color: #c0392b; background-color: #fadbd8; padding: 2px 4px; border-radius: 3px; font-weight: bold; margin-left: 8px;")
                    warning_span.string = f"🔥停滯 {diff_work_days} 個工作天"
                    p_pend.append(warning_span)
                elif diff_work_days >= 5:
                    warning_span = soup.new_tag("span", style="color: #e74c3c; font-weight: bold; margin-left: 8px;")
                    warning_span.string = f"⚠️停滯 {diff_work_days} 個工作天"
                    p_pend.append(warning_span)
                elif diff_work_days >= 3:
                    warning_span = soup.new_tag("span", style="color: #e67e22; font-weight: bold; margin-left: 8px;")
                    warning_span.string = f"⏳停滯 {diff_work_days} 個工作天"
                    p_pend.append(warning_span)

            is_tbd = (pl.get('duedate') == '"Due TBD"')
            if SETTINGS.get("show_duedate") and pl.get('duedate'):
                if not (is_tbd and not SETTINGS.get("show_due_tbd")):
                    span_due = soup.new_tag("span", style="color: gray; font-size: 50%;")
                    
                    if pl.get('parent_due_key'):
                        inherit_text = "依故事" if pl.get('is_inherited_from_story') else "依父系"
                        span_due.string = f" {pl['duedate']} ({inherit_text} "
                        a_parent = soup.new_tag("a", href=f"{JIRA_URL}/browse/{pl['parent_due_key']}", style="color: #3498db; text-decoration: none;")
                        a_parent.string = pl['parent_due_summary']
                        span_due.append(a_parent)
                        span_due.append(soup.new_string(")"))
                    else:
                        span_due.string = f" {pl['duedate']}"
                        
                    p_pend.append(span_due)
                    
                    if not is_tbd and pl.get('duedate_dt'):
                        diff_days = calculate_working_days(target_date, pl['duedate_dt'])
                        color = "#2ecc71" if diff_days >= 0 else "#e74c3c"
                        sign = "+" if diff_days >= 0 else ""
                        warning_icon = "❗" if diff_days <= 2 else ""
                        span_diff = soup.new_tag("span", style=f"color: {color}; font-size: 50%; margin-left: 4px;")
                        span_diff.string = f" {warning_icon}({sign}{diff_days})"
                        p_pend.append(span_diff)

            if pl.get('confluence_links'):
                for cl in pl['confluence_links']:
                    p_pend.append(soup.new_string(" "))
                    a_conf = soup.new_tag("a", href=cl['url'])
                    a_conf.string = f"🔗 {cl['title']}"
                    p_pend.append(a_conf)
            
            pending_box.append(p_pend)
            task_counter += 1
        rendered_pending_sections[0] += 1

    if SETTINGS.get("show_pending_inprogress"): append_ptasks(pending_in_progress, "🔄 進行中且尚未更新進度的任務：", "#d35400")
    if SETTINGS.get("show_pending_waiting"): append_ptasks(pending_waiting, "⏳ Waiting 狀態的任務：", "#8e44ad")
    if SETTINGS.get("show_pending_todo"): append_ptasks(pending_todo, "📋 待辦事項 (To Do) 任務：", "#2980b9")
    if SETTINGS.get("show_pending_candidate"): append_ptasks(pending_candidate, "🎯 Candidate 狀態任務：", "#16a085")
    if SETTINGS.get("show_pending_blocked"): append_ptasks(pending_blocked, "🛑 BLOCKED 任務：", "#c0392b")
    if SETTINGS.get("show_pending_abort"): append_ptasks(pending_abort, "❌ ABORT 任務：", "#7f8c8d")
    if SETTINGS.get("show_pending_resume"): append_ptasks(pending_resume, "▶️ RESUME 任務：", "#f39c12")

    p_spacer = soup.new_tag("p")
    p_spacer.append(soup.new_tag("br"))
    container.append(p_spacer)

def run_clear_logic():
    try:
        api_endpoint = f"{JIRA_URL}/wiki/rest/api/content"
        selected_dates = get_selected_dates()
        if not selected_dates: return print("⚠️ 未選擇 any 要更新的日期(無法判斷目標頁面)。")

        target_title = get_target_report_title(selected_dates[0])
        print(f"\n=========================================\n🎯 目標週報頁面: {target_title} (區間清除模式)\n=========================================\n")
        print(f"🔍 正在 Confluence 搜尋頁面...")
        res = requests.get(api_endpoint, params={"title": target_title, "expand": "body.storage,version"}, auth=ADMIN_AUTH)
        pages = res.json().get('results', [])
        if not pages: return print(f"❌ 找不到目標頁面 {target_title}！")
            
        page_data = pages[0]
        page_id = page_data['id']
        html_content = page_data['body']['storage']['value']
        soup = BeautifulSoup(html_content, 'html.parser')
        
        page_needs_update = False
        cleaned_count = 0

        for element in soup.find_all(lambda tag: tag.has_attr('class') and any(c.startswith('daily-worklog-') for c in tag['class'])):
            element.extract()
            page_needs_update = True
            cleaned_count += 1
        
        for name, email in ACCOUNT_DICT.items():
            acc_id = get_account_id(email, name)
            target_mention = None
            if acc_id:
                ri_user = soup.find('ri:user', attrs={'ri:account-id': acc_id})
                if ri_user: target_mention = ri_user.find_parent('ac:link')
            if not target_mention:
                all_links = soup.find_all('ac:link')
                for link in all_links:
                    if name.lower() in str(link).lower() or (email and email.split('@')[0].lower() in str(link).lower()):
                        target_mention = link; break
            if not target_mention:
                target_mention = soup.find(string=re.compile(f"@{name}", re.I))
                if not target_mention and email:
                    target_mention = soup.find(string=re.compile(f"@{email.split('@')[0]}", re.I))
            if not target_mention: continue

            mention_container = target_mention.find_parent(['p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div'])
            if not mention_container: mention_container = target_mention

            if isinstance(mention_container, Tag) and mention_container.name in ['p', 'div', 'li', 'h2', 'h3', 'h4', 'h5', 'h6']:
                mention_container.name = 'h1'
                page_needs_update = True

            user_nodes = []
            current_node = mention_container.next_sibling
            while current_node:
                if isinstance(current_node, NavigableString) and str(current_node).strip().startswith('@'): break 
                if isinstance(current_node, Tag):
                    if current_node.name == 'ac:link' or current_node.find('ac:link'): break 
                    if current_node.name in ['h1', 'h2', 'hr']: break 
                    if str(current_node.get_text()).strip().startswith('@'): break
                user_nodes.append(current_node)
                current_node = current_node.next_sibling

            nodes_to_remove = []
            i = 0
            while i < len(user_nodes):
                node = user_nodes[i]
                text = node.get_text(strip=True) if isinstance(node, Tag) else str(node).strip()
                if re.match(r'^\[\d{4}/\d{2}/\d{2}\]', text):
                    nodes_to_remove.append(node)
                    i += 1
                    while i < len(user_nodes):
                        next_node = user_nodes[i]
                        next_text = next_node.get_text(strip=True) if isinstance(next_node, Tag) else str(next_node).strip()
                        if re.match(r'^\[\d{4}/\d{2}/\d{2}\]', next_text): break
                        nodes_to_remove.append(next_node)
                        i += 1
                else:
                    i += 1

        for node in nodes_to_remove:
            node.extract()
            page_needs_update = True
            cleaned_count += 1
        
        if page_needs_update:
            print(f"🧹 發現 {cleaned_count} 個日誌區塊/殘留，正在更新至 Confluence...")
            url = f"{api_endpoint}/{page_id}"
            
            payload = {
                    "version": {"number": page_data['version']['number'] + 1, "minorEdit": SETTINGS.get("minor_edit")},
                    "title": page_data['title'],
                    "type": "page",
                    "body": {"storage": {"value": str(soup), "representation": "storage"}},
                    "metadata": {
                        "properties": {
                            "content-appearance-published": {"value": "full-width"},
                            "content-appearance-draft": {"value": "full-width"}
                        }
                    }
                }
            update_res = requests.put(url, json=payload, auth=ADMIN_AUTH, headers={"Content-Type": "application/json"})
            if update_res.status_code == 200:
                print("🎉 大功告成！已成功清除本頁所有舊日誌。")
            else: print(f"❌ 儲存至 Confluence 失敗: {update_res.text}")
        else:
            print("📭 頁面乾淨無殘留，無需清除。")

    except Exception as e: print(f"\n❌ 程式發生意外錯誤: {e}")


def run_sync_logic():
    start_time = time.time()
    PENDING_CONF_IMAGES.clear()
    
    sync_status = "success"
    sync_message = ""
    total_logs_written = 0
    
    is_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    github_event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    
    if is_github_actions:
        if github_event_name == "schedule":
            update_source_tag = "Scheduled Update"
        elif github_event_name == "workflow_dispatch":
            update_source_tag = "GitHub Update"
        else:
            update_source_tag = "GitHub Update (Bob)"
    else:
        update_source_tag = "Local Update"
        
    try:
        if SETTINGS.get("auto_clear_first"):
            print("=========================================")
            print("🧹 [排程任務] 執行批次寫入前的前置自動清除作業")
            print("=========================================")
            run_clear_logic()
            print("\n✅ 前置清除作業完成，準備開始批次寫入...\n")

        if not SETTINGS.get("filter_comment") and not SETTINGS.get("filter_started"):
            sync_status = "error"
            sync_message = "❌ 錯誤：請至少在 settings_daily.json 啟用一種「智慧過濾機制」。"
            return print(sync_message)

        api_endpoint = f"{JIRA_URL}/wiki/rest/api/content"
        selected_dates = get_selected_dates()
        if not selected_dates:
            sync_status = "warning"
            sync_message = "⚠️ 未選擇任何要更新的日期。"
            return print(sync_message)

        target_title = get_target_report_title(selected_dates[0])
        print(f"\n=========================================\n🎯 目標週報頁面: {target_title}\n=========================================\n")
        
        print(f"🔍 正在 Confluence 搜尋頁面...")
        res = requests.get(api_endpoint, params={"title": target_title, "expand": "body.storage,version"}, auth=ADMIN_AUTH)
        
        if res.status_code != 200:
            sync_status = "error"
            print(f"❌ API 請求被 Confluence 拒絕！(狀態碼: {res.status_code})")
            return

        pages = res.json().get('results', [])
        if not pages:
            sync_status = "error"
            sync_message = f"❌ 找不到目標頁面 {target_title}，請先確保週報已建立！"
            return print(sync_message)
            
        page_data = pages[0]
        page_id = page_data['id']
        html_content = page_data['body']['storage']['value']
        soup = BeautifulSoup(html_content, 'html.parser')
        
        page_needs_update = False

        week_start = selected_dates[0] - timedelta(days=selected_dates[0].weekday())
        days_to_process = [week_start + timedelta(days=i) for i in range(7)]
        
        # ====================================================
        # 🌟 核心防線一：在填入新資料前，先把 #Worklog 區間「無差別掃乾淨」
        # ====================================================
        print("\n🧹 執行清潔防呆：正在精準清洗 #Worklog 區間的所有歷史進度...")
        start_marker = soup.find(string=re.compile(r'#Worklog\s*$'))
        end_marker = soup.find(string=re.compile(r'#Worklog End\s*$'))

        if start_marker and end_marker:
            start_element = start_marker.parent
            end_element = end_marker.parent
            
            elements_to_remove = []
            current_element = start_element.next_sibling
            while current_element and current_element != end_element:
                elements_to_remove.append(current_element)
                current_element = current_element.next_sibling
                
            for element in elements_to_remove:
                if hasattr(element, 'decompose'): element.decompose()
                else: element.extract()
            page_needs_update = True
            print("  └ ✅ #Worklog 區間舊日誌已全部清除乾淨。")
        else:
            print("  └ ⚠️ 找不到 #Worklog 區間標記，將以追加方式寫入。")

        print(f"\n📡 啟動全域雷達：一次性掃描 Jira 自 {week_start.strftime('[%Y/%m/%d]')} 起的所有變更紀錄...")
        all_issues_pool = fetch_all_recent_issues(week_start)
        
        target_date = max(selected_dates)
        target_date_tags = [d.strftime("[%Y/%m/%d]") for d in days_to_process]

        # ====================================================
        # 🌟 核心防線二：強制定向排序 (符合您要求的指定順序)
        # ====================================================
        STRICT_ORDER = ["sam.chang", "Vic Wu", "SF Hsieh", "shannonchang", "Bob Lin"]
        
        # 建立暫存湯匙容器，用來依序收集所有人的排版結果
        combined_soup = BeautifulSoup("", "html.parser")

        print("\n=========================================")
        print("🚀 開始針對個別成員進度進行封裝與排版...")

        explicit_parent_due_map = {}
        if SETTINGS.get("inherit_parent_due"):
            parent_keys_to_fetch = set()
            for iss in all_issues_pool:
                fields = iss.get('fields') or {}
                parent_data = fields.get('parent') or {}
                if parent_data.get('key'): parent_keys_to_fetch.add(parent_data['key'])
            
            if parent_keys_to_fetch:
                p_list = list(parent_keys_to_fetch)
                for i in range(0, len(p_list), 50):
                    chunk = p_list[i:i+50]
                    jql = f"key in ({','.join(chunk)})"
                    try:
                        pres = requests.post(f"{JIRA_URL}/rest/api/3/search/jql", json={"jql": jql, "maxResults": 100, "fields": ["duedate", "summary"]}, auth=ADMIN_AUTH, timeout=10)
                        if pres.status_code == 200:
                            for pi in pres.json().get('issues', []):
                                fields = pi.get('fields') or {}
                                due = fields.get('duedate')
                                if due: explicit_parent_due_map[pi['key']] = {'due': due, 'summary': fields.get('summary', 'Unknown'), 'key': pi['key']}
                    except: pass

        story_due_map = {}
        fetched_projects = set()

        def ensure_project_rank_mapped(proj_key):
            if not proj_key or proj_key in fetched_projects: return
            fetched_projects.add(proj_key)
            try:
                jql = f'project = "{proj_key}" ORDER BY Rank ASC'
                res = requests.post(f"{JIRA_URL}/rest/api/3/search/jql", json={"jql": jql, "maxResults": 3000, "fields": ["issuetype", "duedate", "summary"]}, auth=ADMIN_AUTH, timeout=15)
                if res.status_code == 200:
                    current_epic_due, current_epic_summary, current_epic_key = None, "", ""
                    current_story_due, current_story_summary, current_story_key = None, "", ""
                    for i_rank in res.json().get('issues', []):
                        r_key = i_rank['key']
                        r_fields = i_rank.get('fields') or {}
                        r_type = r_fields.get('issuetype', {}).get('name', '').lower()
                        r_due = r_fields.get('duedate')
                        r_summary = r_fields.get('summary', 'Unknown')
                        is_epic = 'epic' in r_type or '大型工作' in r_type or r_fields.get('issuetype', {}).get('hierarchyLevel') == 1
                        is_story = 'story' in r_type or '故事' in r_type
                        if is_epic:
                            current_epic_due, current_epic_summary, current_epic_key = r_due, r_summary, r_key
                            current_story_due, current_story_summary, current_story_key = None, "", ""
                        elif is_story:
                            current_story_due, current_story_summary, current_story_key = r_due, r_summary, r_key
                        else:
                            if not r_due:
                                if current_story_due: story_due_map[r_key] = {'due': current_story_due, 'summary': current_story_summary, 'key': current_story_key}
                                elif current_epic_due: story_due_map[r_key] = {'due': current_epic_due, 'summary': current_epic_summary, 'key': current_epic_key}
            except: pass

        if SETTINGS.get("inherit_parent_due"):
            for iss in all_issues_pool: ensure_project_rank_mapped(iss.get('fields', {}).get('project', {}).get('key'))

        # 🌟 核心防線三：今日日期黑名單攔截 (今天是 7/2，故黑名單包含 7/2 及之後的日期)
        today_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

        # 依照您指定的嚴格順序，依序將各個同仁的進度封裝進 combined_soup 裡
        for target_user_key in STRICT_ORDER:
            name = next((k for k in ACCOUNT_DICT.keys() if k == target_user_key or (target_user_key == "Bob Lin" and k == "Bob Lin") or (target_user_key == "Vic Wu" and k == "Vic Wu") or (target_user_key == "SF Hsieh" and k == "SF Hsieh")), None)
            if not name:
                name = next((k for k in ACCOUNT_DICT.keys() if target_user_key.lower() in k.lower()), None)
            if not name: continue
                
            email = ACCOUNT_DICT[name]
            acc_id = get_account_id(email, name)
            user_bg_color = USER_BG_COLORS.get(name, "#ffffff")
            
            raw_logs = extract_logs_from_issues(name, email, acc_id, days_to_process, all_issues_pool)
            
            # 🛡️ 實施未來黑名單過濾：只要日期大於等於今天(7/2)，就一律攔截丟棄！
            # 這樣週一、週二、週三的歷史數據都能完美保留，但今天(7/2)的任何變動絕不會跑出來
            logs = [l for l in raw_logs if l['started_date'] < today_str]
            
            if SETTINGS.get("inherit_parent_due"):
                for log in logs:
                    proj_key = log['key'].split('-')[0]
                    ensure_project_rank_mapped(proj_key)
                    if not log.get('duedate_dt'):
                        p_info = story_due_map.get(log['key']) or explicit_parent_due_map.get(log.get('parent_key', ''))
                        if p_info:
                            log['duedate_dt'] = datetime.strptime(p_info['due'][:10], "%Y-%m-%d")
                            log['duedate'] = f'"Due {log["duedate_dt"].strftime("%y")}\' {log["duedate_dt"].month}/{log["duedate_dt"].day}"'
                            log['parent_due_key'] = p_info['key']
                            log['parent_due_summary'] = p_info['summary']
                            log['is_inherited_from_story'] = (log['key'] in story_due_map)

            if SETTINGS.get("style_weekly") and logs:
                daily_aggregated_logs = enrich_with_weekly_data(logs, name, email, acc_id, days_to_process, all_issues_pool)
                
                # 🛡️ 同步修剪 Style 3 風格內的日誌子項目，剔除今天(7/2)及之後的項目
                if daily_aggregated_logs:
                    for alog in daily_aggregated_logs:
                        alog['daily_days'] = [d for d in alog['daily_days'] if d['date'].strftime("%Y-%m-%d") < today_str]
                    daily_aggregated_logs = [alog for alog in daily_aggregated_logs if alog['daily_days']]
                
                total_mins = sum(d.get('total_mins_day', 0) for log in daily_aggregated_logs for d in log['daily_days'] if d['date'].date() in [sd.date() for sd in selected_dates])
                weekend_mins = sum(d.get('total_mins_day', 0) for log in daily_aggregated_logs for d in log['daily_days'] if d['date'].weekday() >= 5 and d['date'].date() in [sd.date() for sd in selected_dates])
            else:
                daily_aggregated_logs = None
                total_mins = sum(log.get('duration_mins', 0) for log in logs if log.get('started_date') in [sd.strftime("%Y-%m-%d") for sd in selected_dates])
                weekend_mins = sum(log.get('duration_mins', 0) for log in logs if log.get('started_date') in [sd.strftime("%Y-%m-%d") for sd in selected_dates if sd.weekday() >= 5])

            updated_keys = {log['key'] for log in logs}
            pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume = fetch_pending_tasks(acc_id, updated_keys, target_date)
            
            if SETTINGS.get("inherit_parent_due"):
                for p_list in [pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume]:
                    for t in p_list:
                        proj_key = t['key'].split('-')[0]
                        ensure_project_rank_mapped(proj_key)
                        if not t.get('duedate_dt'):
                            p_info = story_due_map.get(t['key']) or explicit_parent_due_map.get(t.get('parent_key', ''))
                            if p_info:
                                t['duedate_dt'] = datetime.strptime(p_info['due'][:10], "%Y-%m-%d")
                                t['duedate'] = f'"Due {t["duedate_dt"].strftime("%y")}\' {t["duedate_dt"].month}/{t["duedate_dt"].day}"'
                                t['parent_due_key'] = p_info['key']
                                t['parent_due_summary'] = p_info['summary']
                                t['is_inherited_from_story'] = (t['key'] in story_due_map)

            if SETTINGS.get("show_pending_has_due"):
                pending_in_progress = [p for p in pending_in_progress if p.get('duedate_dt') is not None]
                pending_waiting = [p for p in pending_waiting if p.get('duedate_dt') is not None]
                pending_todo = [p for p in pending_todo if p.get('duedate_dt') is not None]
                pending_candidate = [p for p in pending_candidate if p.get('duedate_dt') is not None]
                pending_blocked = [p for p in pending_blocked if p.get('duedate_dt') is not None]
                pending_abort = [p for p in pending_abort if p.get('duedate_dt') is not None]
                pending_resume = [p for p in pending_resume if p.get('duedate_dt') is not None]

            # 建立人名標題
            p_user = soup.new_tag("p", style="margin-top:20px; margin-bottom:10px;")
            span_user = soup.new_tag("span", style=f"background-color:{user_bg_color}; font-weight:bold; font-size:120%; padding:3px 8px; border-radius:4px; border:1px solid #7f8c8d;")
            span_user.string = f"@{name}"
            p_user.append(span_user)

            if daily_aggregated_logs or logs:
                if SETTINGS.get("style_weekly") and daily_aggregated_logs:
                    new_html_block = generate_style_3_html(soup, target_date, selected_dates, daily_aggregated_logs, pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume, total_mins, weekend_mins, bg_color=user_bg_color, update_source_tag=update_source_tag)
                else:
                    new_html_block = generate_style_2_html(soup, target_date, logs, pending_in_progress, pending_waiting, pending_todo, pending_candidate, pending_blocked, pending_abort, pending_resume, total_mins, bg_color=user_bg_color, update_source_tag=update_source_tag)
                
                # 正序疊加進臨時的 combined_soup 容器中
                combined_soup.append(p_user)
                combined_soup.append(new_html_block)
                
                total_logs_written += (len(logs) + len(pending_in_progress) + len(pending_waiting) + len(pending_todo) + len(pending_candidate) + len(pending_blocked) + len(pending_abort) + len(pending_resume))
                page_needs_update = True
                print(f"  ☑️ 成功處理 {name} 進度封裝。")

        # 最後將排好順序的整包 combined_soup 一次性安插在 #Worklog 正下方
        if page_needs_update and start_marker:
            start_element.insert_after(combined_soup)

        if page_needs_update:
            print(f"\n💾 發現頁面有變動，正在將最終結果儲存至 Confluence...")
            upload_pending_images_to_confluence(page_id)
            url = f"{api_endpoint}/{page_id}"
            payload = {
                "version": {"number": page_data['version']['number'] + 1, "minorEdit": SETTINGS.get("minor_edit")},
                "title": page_data['title'],
                "type": "page",
                "body": {"storage": {"value": str(soup), "representation": "storage"}}
            }
            update_res = requests.put(url, json=payload, auth=ADMIN_AUTH, headers={"Content-Type": "application/json"})
            if update_res.status_code == 200:
                page_url = f"{JIRA_URL}/wiki/pages/viewpage.action?pageId={page_id}"
                sync_message = f"🎉 同步完成！\n本次共更新了 {total_logs_written} 筆任務紀錄至 Confluence。\n目標頁面：{target_title}\n網址連結：{page_url}"
                print(f"🎉 大功告成！已成功更新 {total_logs_written} 筆任務紀錄！")

                # ==========================================
                # 🌟 歷史大清洗與逆向防洗版
                # ==========================================
                jira_keys = list(set(re.findall(r'[A-Z][A-Z0-9]+-\d+', str(soup))))
                if jira_keys:
                    print(f"\n⏳ 偵測到 {len(jira_keys)} 個 Jira 任務。等待 5 秒鐘讓系統連動...")
                    time.sleep(5)
                    cleared_count = 0
                    for key in jira_keys:
                        try:
                            remote_link_url = f"{JIRA_URL}/rest/api/3/issue/{key}/remotelink"
                            r_links_resp = requests.get(remote_link_url, auth=ADMIN_AUTH, timeout=10)
                            if r_links_resp.status_code == 200:
                                for link_obj in r_links_resp.json():
                                    url_val = link_obj.get('object', {}).get('url', '')
                                    title_val = link_obj.get('object', {}).get('title', '')
                                    if "WeeklyReport" in title_val or "WeeklyReport" in url_val or str(page_id) in url_val:
                                        link_id = link_obj.get('id')
                                        del_resp = requests.delete(f"{remote_link_url}/{link_id}", auth=ADMIN_AUTH, timeout=10)
                                        if del_resp.status_code in [200, 204]: cleared_count += 1
                        except: pass
                    print(f"✅ 歷史大清洗完畢！共成功拔成了 {cleared_count} 筆殘留的 Jira 週報連動紀錄。")
            else:
                sync_status = "error"
                sync_message = f"❌ 儲存至 Confluence 失敗。"
        else:
            sync_status = "warning"
            sync_message = "📭 執行完畢，沒有找到需要變動的紀錄。"
            print(f"\n{sync_message}")

    except Exception as e:
        sync_status = "error"
        sync_message = f"❌ 程式發生意外錯誤：{e}"
        print(f"\n{sync_message}")
    finally:
        elapsed = int(time.time() - start_time)
        mins, secs = divmod(elapsed, 60)
        time_str = f"{mins}m{secs}s" if mins > 0 else f"{secs}s"
        
        line_user_id = os.environ.get("LINE_USER_ID", "")
        line_display_name = os.environ.get("LINE_DISPLAY_NAME", "")
        line_access_token = os.environ.get("LINE_ACCESS_TOKEN", "")
        admin_user_id = "U2e9b79c2f71cb2a3db62e5d75254270c"
        
        if line_user_id and line_access_token:
            user_label = line_display_name if line_display_name else f"ID: {line_user_id}"
            try:
                line_bot_api = LineBotApi(line_access_token)
                final_push_message = f"{sync_message}\n\n⏱️ 執行耗時: {time_str}"
                line_bot_api.push_message(line_user_id, TextSendMessage(text=final_push_message))
                if line_user_id != admin_user_id:
                    admin_copy_message = f"🕵️ 任務完工報告副本\n執行同仁：{user_label}\n\n{final_push_message}"
                    line_bot_api.push_message(admin_user_id, TextSendMessage(text=admin_copy_message))
            except Exception as e: print(f"❌ LINE 推播回報發送失敗: {e}")

if __name__ == "__main__":
    print("=== Confluence 自動填表機 (GitHub Actions Headless V50.15 全週合併版) ===")
    run_sync_logic()
