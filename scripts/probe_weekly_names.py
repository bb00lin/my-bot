"""暫時性唯讀探測（opt-in、不修改頁面、不輸出憑證）。

1. 列出週報頁 #Worklog 區間內「成員名稱行」的 storage 與對應 ADF 節點。
2. 列出指定任務各日列（預設 PBA-225）storage。
3. 用 contentbody/convert（不寫頁面）測試候選標記的 storage→ADF 結果。
4. 部署後驗證：圖片 mediaSingle / inline-media-image / unsupported 計數。
"""

import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth

RAW_URL = os.environ.get("CONF_URL", "")
USER = os.environ.get("CONF_USER", "")
TOKEN = os.environ.get("CONF_PASS", "")
PAGE_IDS = [p.strip() for p in os.environ.get("PROBE_PAGE_IDS", "228327425,225247361").split(",") if p.strip()]
TARGET_KEY = os.environ.get("PROBE_ISSUE_KEY", "PBA-225")

if not RAW_URL or not USER or not TOKEN:
    print("MISSING_CREDENTIALS")
    sys.exit(1)

_p = urlparse(RAW_URL)
BASE = f"{_p.scheme}://{_p.netloc}"
AUTH = HTTPBasicAuth(USER, TOKEN)
_SECRETS = [s for s in (USER, TOKEN) if s]
NAMES = ["sam.chang", "Vic Wu", "SF Hsieh", "shannonchang", "Bob Lin"]


def safe(text):
    out = str(text)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    return out


def hr(title):
    print("\n" + "=" * 72)
    print(f"== {title}")
    print("=" * 72)


def adf_text(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        if node.get("type") == "mention":
            return (node.get("attrs") or {}).get("text", "@mention")
        return "".join(adf_text(c) for c in node.get("content") or [])
    return ""


def walk(node, fn, depth=0):
    if isinstance(node, dict):
        fn(node, depth)
        for c in node.get("content") or []:
            walk(c, fn, depth + 1)


problems = []

for page_id in PAGE_IDS:
    hr(f"頁面 {page_id}")
    r = requests.get(f"{BASE}/wiki/rest/api/content/{page_id}?expand=body.storage,version",
                     auth=AUTH, timeout=90)
    print(f"GET content -> {r.status_code}")
    if r.status_code != 200:
        print(safe(r.text[:300]))
        continue
    j = r.json()
    storage = j["body"]["storage"]["value"]
    print(f"  title={j.get('title')} version={(j.get('version') or {}).get('number')} len={len(storage)}")

    # 1. 成員名稱行
    soup = BeautifulSoup(storage, "html.parser")
    for name in NAMES:
        hits = soup.find_all(string=re.compile(rf"@{re.escape(name)}\b"))
        for h in hits[:3]:
            blk = h.find_parent(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"])
            print(f"\n  [text @{name}] {safe(str(blk or h)[:400])}")
    for link in soup.find_all("ac:link")[:12]:
        if link.find("ri:user"):
            blk = link.find_parent(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"])
            print(f"\n  [ri:user mention] {safe(str(blk or link)[:400])}")
    sm = re.search(r"#Worklog\s*<", storage)
    if sm:
        print(f"\n  #Worklog 周邊: {safe(storage[max(0, sm.start() - 200): sm.start() + 400])}")
    em = re.search(r"#Worklog End", storage)
    if em:
        print(f"\n  #Worklog End 周邊: {safe(storage[max(0, em.start() - 300): em.start() + 100])}")

    # 2. 目標任務日列
    for body in soup.find_all("ac:rich-text-body"):
        current_key = None
        for child in body.find_all(recursive=False):
            raw = str(child)
            if child.name == "p" and re.match(r"^\s*\d+\.", child.get_text(" ", strip=True)):
                keys = re.findall(r"/browse/([A-Z][A-Z0-9]+-\d+)", raw)
                current_key = keys[0] if keys else None
                continue
            if current_key == TARGET_KEY and child.name == "div":
                print(f"\n  [{TARGET_KEY} 日列] {safe(raw[:700])}")

    # 3. ADF 統計 + 名稱行 ADF
    tr = requests.get(f"{BASE}/wiki/api/v2/pages/{page_id}?body-format=atlas_doc_format",
                      auth=AUTH, timeout=180)
    adf_raw = ""
    if tr.status_code == 200:
        adf_raw = str(((tr.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "")
    counts = Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf_raw))
    n_ac = len(re.findall(r"<ac:image\b", storage))
    print(f"\n  ADF -> {tr.status_code} len={len(adf_raw)}")
    print(f"    <ac:image>={n_ac} mediaSingle={counts.get('mediaSingle', 0)} "
          f"inline-media-image={adf_raw.count('inline-media-image')} "
          f"unsupportedInline={counts.get('unsupportedInline', 0)} "
          f"unsupportedBlock={counts.get('unsupportedBlock', 0)} mention={counts.get('mention', 0)} "
          f"heading={counts.get('heading', 0)}")
    print(f"    「worklog內容如圖」 storage={storage.count('worklog內容如圖')} "
          f"(附件圖片)={storage.count('(附件圖片)')} 💬={len(re.findall('focusedCommentId=', storage))} "
          f"📎={storage.count('📎')}")
    if adf_raw:
        try:
            doc = json.loads(adf_raw)

            def show(node, depth):
                if node.get("type") in ("paragraph", "heading"):
                    t = adf_text(node)
                    if any(t.strip().startswith(f"@{n}") for n in NAMES) or t.strip() in [f"@{n}" for n in NAMES]:
                        print(f"    [ADF name] {safe(json.dumps(node, ensure_ascii=False)[:500])}")

            walk(doc, show)
        except Exception as e:
            print(f"    ADF parse error: {e}")

    # 部署後：名稱 heading 與「worklog內容如圖」日列
    n_h3 = sum(1 for h in soup.find_all("h3")
               if any(h.get_text().strip() == f"@{n}" for n in NAMES) and h.find("strong"))
    n_toc = len(re.findall(r'ac:name="toc"', storage))
    print(f"    member h3+strong={n_h3} toc macro={n_toc}")
    for p in soup.find_all("p"):
        if "worklog內容如圖" not in p.get_text():
            continue
        row = p.find_parent("div")
        if row is None:
            continue
        seq = []
        for sub in row.find_all(recursive=False):
            img = sub.find("ac:image")
            if img is not None:
                ri = img.find("ri:attachment")
                seq.append(f"IMG({ri.get('ri:filename') if ri else '?'}, align={img.get('ac:align')})")
            else:
                seq.append("text(" + re.sub(r"-{2,}", "", sub.get_text(" ", strip=True))[:70] + ")")
        print(f"    [如圖列] {safe(' → '.join(seq))}")

# 4. 用新程式碼渲染 9/30 PBA-225 情境，轉 ADF 確認（不寫頁面）
hr("新程式碼渲染 → ADF（不寫頁面）")
from datetime import datetime  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import daily_worklog_to_confluence as m  # noqa: E402

SRC_PAGE = "225247361"
JIRA_FN = "wl_20260930_170000_441_471659_clipboard.png"


def day(comment, day_images, mins=60):
    return {"date": datetime(2026, 9, 30), "day_name": "Wed", "day_short": "9/30",
            "dur_str": m.format_duration(mins), "total_mins_day": mins, "comment": comment,
            "transition": "", "has_log": True, "day_images": day_images, "day_comments": []}


s = BeautifulSoup("", "html.parser")
s.append(m.build_member_name_heading(s, "Bob Lin", m.USER_BG_COLORS["Bob Lin"]))
log = {"key": "PBA-225", "summary": "probe", "status": "IN PROGRESS", "project": "PBA",
       "parent": "NA", "label": "NA", "duedate": '"Due TBD"',
       "daily_days": [day(f"[[IMG:{JIRA_FN}]]", []), day("", [JIRA_FN])]}
s.append(m.generate_style_3_html(s, datetime(2026, 10, 2), [datetime(2026, 9, 30)], [log],
                                 bg_color=m.USER_BG_COLORS["Bob Lin"]))
m.PENDING_CONF_IMAGES.clear()
m.promote_images_to_block_media(s, SRC_PAGE)
sto = str(s)
cr = requests.post(f"{BASE}/wiki/rest/api/contentbody/convert/atlas_doc_format?contentIdContext={SRC_PAGE}",
                   json={"value": sto, "representation": "storage"}, auth=AUTH, timeout=60)
print(f"  convert -> {cr.status_code}")
if cr.status_code == 200:
    adf_s = cr.json().get("value", "")
    c = Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf_s))
    n_ac = sto.count("<ac:image")
    print(f"  <ac:image>={n_ac} mediaSingle={c.get('mediaSingle', 0)} "
          f"inline-media-image={adf_s.count('inline-media-image')} "
          f"unsupportedInline={c.get('unsupportedInline', 0)} unsupportedBlock={c.get('unsupportedBlock', 0)} "
          f"heading={c.get('heading', 0)} align-start={adf_s.count('align-start')}")
    doc = json.loads(adf_s)
    seq = []

    def collect(node, depth):
        t = node.get("type")
        if t == "heading":
            seq.append(f"heading{(node.get('attrs') or {}).get('level')}"
                       f"[{adf_text(node)} marks={[mk['type'] for c2 in node.get('content') or [] for mk in c2.get('marks') or []]}]")
        elif t == "paragraph":
            txt = re.sub(r"-{2,}", "", adf_text(node)).strip()
            if txt:
                seq.append(f"p[{txt[:50]}]")
        elif t == "mediaSingle":
            seq.append(f"mediaSingle({(node.get('attrs') or {}).get('layout')})")

    walk(doc, collect)
    print("  順序: " + safe(" → ".join(seq)))
    if n_ac != c.get("mediaSingle", 0) or adf_s.count("inline-media-image") or c.get("unsupportedInline") or c.get("unsupportedBlock"):
        problems.append("新程式碼渲染的 ADF 不符預期")
else:
    print(safe(cr.text[:300]))

hr("結論")
for p in problems:
    print(" - " + p)
print("DONE")
