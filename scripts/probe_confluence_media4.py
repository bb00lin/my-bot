"""第四階段：端到端驗證。

用「真的要上線的那支函式」promote_images_to_block_media 處理真實週報頁的 storage，
寫到一個可刪除的測試頁，再回讀 ADF 確認每張圖都變成 mediaSingle。

這是上線前的最後一道證據；不動到真實週報頁。
"""

import os
import re
import sys
from collections import Counter
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import daily_worklog_to_confluence as m  # noqa: E402

BASE = m.JIRA_URL
AUTH = m.ADMIN_AUTH
SRC = os.environ.get("PROBE_PAGE_ID", "208109570")
SPACE_KEY = os.environ.get("PROBE_SPACE_KEY", "teamAIoTHW")
TITLE = "ZZZ_IMGTEST_endtoend_probe"
_SECRETS = [s for s in (m.ADMIN_EMAIL, m.ADMIN_TOKEN) if s]


def safe(t):
    out = str(t)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    return out


def hr(t):
    print("\n" + "=" * 72)
    print(f"== {t}")
    print("=" * 72)


# ------------------------------------------------------------ 1. 取真實 storage
hr("1. 取得真實週報頁 storage")
r = requests.get(f"{BASE}/wiki/rest/api/content/{SRC}?expand=body.storage", auth=AUTH, timeout=60)
storage = ((((r.json().get("body") or {}).get("storage") or {}).get("value")) or "") if r.status_code == 200 else ""
print(f"  GET -> {r.status_code}, storage len={len(storage)}")
n_img = len(re.findall(r"<img\b", storage))
n_ac = len(re.findall(r"<ac:image\b", storage))
print(f"  來源頁 <img>={n_img}, <ac:image>={n_ac}")

# --------------------------------------------------------- 2. 建測試頁 + 複製附件
hr("2. 建立測試頁並先複製圖片附件")
ex = requests.get(f"{BASE}/wiki/rest/api/content?spaceKey={quote(SPACE_KEY)}&title={quote(TITLE)}",
                  auth=AUTH, timeout=40)
for it in (ex.json().get("results") or []) if ex.status_code == 200 else []:
    requests.delete(f"{BASE}/wiki/rest/api/content/{it['id']}", auth=AUTH, timeout=40)
    print(f"  已刪除舊測試頁 {it['id']}")

cr = requests.post(f"{BASE}/wiki/rest/api/content", auth=AUTH, timeout=60, json={
    "type": "page", "title": TITLE, "space": {"key": SPACE_KEY},
    "body": {"storage": {"value": "<p>init</p>", "representation": "storage"}},
})
print(f"  建立頁面 -> {cr.status_code}")
if cr.status_code not in (200, 201):
    print(safe(cr.text[:500])); sys.exit(1)
PID = cr.json()["id"]
print(f"  測試頁 id={PID}")

ar = requests.get(f"{BASE}/wiki/rest/api/content/{SRC}/child/attachment?limit=200", auth=AUTH, timeout=60)
atts = (ar.json().get("results") or []) if ar.status_code == 200 else []
copied = 0
for a in atts:
    fn = a["title"]
    if not re.search(r"\.(png|jpe?g|gif|webp|bmp)$", fn, re.I):
        continue
    dl = (a.get("_links") or {}).get("download") or ""
    if not dl:
        continue
    blob = requests.get(f"{BASE}/wiki{dl}", auth=AUTH, timeout=60).content
    up = requests.post(f"{BASE}/wiki/rest/api/content/{PID}/child/attachment",
                       auth=AUTH, headers={"X-Atlassian-Token": "no-check"},
                       files={"file": (fn, blob, (a.get("extensions") or {}).get("mediaType") or "image/png")},
                       timeout=90)
    if up.status_code in (200, 201):
        copied += 1
print(f"  已複製 {copied} 個圖片附件")

# ------------------------------------- 3. 用上線用的函式改寫 storage（block 化）
hr("3. 執行 promote_images_to_block_media（上線用函式）")
soup = BeautifulSoup(storage, "html.parser")
m.promote_images_to_block_media(soup, PID)
new_storage = str(soup)
print(f"  改寫後 storage len={len(new_storage)}")
print(f"  <img> 剩下 = {len(re.findall(r'<img', new_storage))}")
print(f"  <ac:image> = {len(re.findall(r'<ac:image', new_storage))}")
sample = re.findall(r"<ac:image\b.*?</ac:image>", new_storage, re.S)[:2]
for s in sample:
    print("  範例：" + safe(s[:340]))

# ------------------------------------------------------------- 4. 寫入 + editor v2
hr("4. 寫入測試頁並設定 editor=v2")
gv = requests.get(f"{BASE}/wiki/rest/api/content/{PID}?expand=version", auth=AUTH, timeout=40)
ver = (gv.json().get("version") or {}).get("number", 1)
ur = requests.put(f"{BASE}/wiki/rest/api/content/{PID}", auth=AUTH, timeout=120, json={
    "id": PID, "type": "page", "title": TITLE, "space": {"key": SPACE_KEY},
    "version": {"number": ver + 1},
    "body": {"storage": {"value": new_storage, "representation": "storage"}},
})
print(f"  PUT body -> {ur.status_code}")
if ur.status_code != 200:
    print(safe(ur.text[:600]))
m.ensure_page_editor_v2(PID)
chk = requests.get(f"{BASE}/wiki/rest/api/content/{PID}?expand=metadata.properties.editor",
                   auth=AUTH, timeout=40)
edv = (((chk.json().get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value") \
    if chk.status_code == 200 else None
print(f"  editor 屬性 = {edv!r}")

# ---------------------------------------------------------------- 5. 回讀 ADF
hr("5. 回讀 ADF：mediaSingle 應等於圖片數")
tr = requests.get(f"{BASE}/wiki/api/v2/pages/{PID}?body-format=atlas_doc_format", auth=AUTH, timeout=120)
adf = str(((tr.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "") \
    if tr.status_code == 200 else ""
print(f"  ADF -> {tr.status_code}, len={len(adf)}")
c = Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf))
for k in ("mediaSingle", "media", "mediaInline", "inlineExtension", "extension",
          "unsupportedInline", "unsupportedBlock", "textColor", "backgroundColor",
          "table", "panel", "expand", "embedCard", "inlineCard"):
    print(f"    {k:20s} = {c.get(k, 0)}")
print(f"    inline-media-image 出現次數 = {adf.count('inline-media-image')}")
print(f"    inline-external-image 出現次數 = {adf.count('inline-external-image')}")

i = adf.find('"mediaSingle"')
if i >= 0:
    print("\n  mediaSingle 片段：\n   " + safe(adf[max(0, i - 40):i + 420]))

expected = n_img + n_ac
got = c.get("mediaSingle", 0)
print(f"\n  預期 mediaSingle = {expected}，實際 = {got}")
print("  結論：" + ("✅ 全部圖片都成為可放大的 media 節點" if got >= expected and expected > 0
                    else "❌ 尚未全部轉成 mediaSingle"))

hr("PROBE4 DONE")
print(f"TEST_PAGE_URL={BASE}/wiki/spaces/{SPACE_KEY}/pages/{PID}")
