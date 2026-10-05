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

# 4. 候選標記轉換測試（不寫頁面）
hr("候選標記 storage→ADF")
acc = ""
try:
    ur = requests.get(f"{BASE}/rest/api/3/myself", auth=AUTH, timeout=20)
    acc = ur.json().get("accountId", "") if ur.status_code == 200 else ""
except Exception:
    pass
BG = "#F5E6FF"
CANDIDATES = {
    "A_current_p_span": f'<p style="margin-top:20px; margin-bottom:10px;"><span style="background-color:{BG}; font-weight:bold; font-size:120%; padding:3px 8px; border-radius:4px; border:1px solid #7f8c8d;">@Bob Lin</span></p>',
    "B_h3_strong_span": f'<h3><strong><span style="background-color:{BG};">@Bob Lin</span></strong></h3>',
    "C_h2_strong_span": f'<h2><strong><span style="background-color:{BG};">@Bob Lin</span></strong></h2>',
    "D_h3_mention": f'<h3><ac:link><ri:user ri:account-id="{acc}" /></ac:link></h3>',
    "E_p_strong_mention": f'<p><strong><ac:link><ri:user ri:account-id="{acc}" /></ac:link></strong></p>',
    "F_h3_strong_mention": f'<h3><strong><ac:link><ri:user ri:account-id="{acc}" /></ac:link></strong></h3>',
}
for label, sto in CANDIDATES.items():
    for to in ("atlas_doc_format", "view"):
        try:
            cr = requests.post(
                f"{BASE}/wiki/rest/api/contentbody/convert/{to}",
                json={"value": sto, "representation": "storage"},
                auth=AUTH, timeout=60,
            )
            val = cr.json().get("value", "") if cr.status_code == 200 else cr.text[:200]
            print(f"\n  [{label} -> {to}] {cr.status_code}: {safe(str(val)[:600])}")
        except Exception as e:
            print(f"\n  [{label} -> {to}] error {e}")

print("\nDONE")
