"""最終驗證 + 清理（opt-in）。

1. 讀真實週報頁的 editor 屬性與 ADF，確認每張圖都是 mediaSingle（可點擊放大）。
2. 刪除診斷期間建立的 ZZZ_IMGTEST_* 測試頁（用 PROBE_KEEP_PAGE_ID 保留一個）。

絕不輸出任何憑證。
"""

import os
import re
import sys
from collections import Counter
from urllib.parse import quote, urlparse

import requests
from requests.auth import HTTPBasicAuth

RAW_URL = os.environ.get("CONF_URL", "")
USER = os.environ.get("CONF_USER", "")
TOKEN = os.environ.get("CONF_PASS", "")
PAGE_ID = os.environ.get("PROBE_PAGE_ID", "208109570")
SPACE_KEY = os.environ.get("PROBE_SPACE_KEY", "teamAIoTHW")
KEEP = os.environ.get("PROBE_KEEP_PAGE_ID", "")
DO_CLEANUP = os.environ.get("PROBE_CLEANUP", "true").lower() == "true"

if not RAW_URL or not USER or not TOKEN:
    print("MISSING_CREDENTIALS")
    sys.exit(1)

_p = urlparse(RAW_URL)
BASE = f"{_p.scheme}://{_p.netloc}"
AUTH = HTTPBasicAuth(USER, TOKEN)
_SECRETS = [s for s in (USER, TOKEN) if s]


def safe(t):
    out = str(t)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    return out


def hr(t):
    print("\n" + "=" * 72)
    print(f"== {t}")
    print("=" * 72)


hr("1. 真實週報頁狀態")
r = requests.get(
    f"{BASE}/wiki/rest/api/content/{PAGE_ID}?expand=body.storage,version,metadata.properties.editor",
    auth=AUTH, timeout=60)
print(f"GET -> {r.status_code}")
storage = ""
if r.status_code == 200:
    j = r.json()
    ed = (((j.get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value")
    storage = (((j.get("body") or {}).get("storage") or {}).get("value")) or ""
    print(f"  title          = {safe(j.get('title'))}")
    print(f"  version        = {(j.get('version') or {}).get('number')}")
    print(f"  editor 屬性    = {ed!r}   (需為 'v2')")
n_ac = len(re.findall(r"<ac:image\b", storage))
n_img = len(re.findall(r"<img\b", storage))
print(f"  storage: <ac:image>={n_ac}, 殘留 <img>={n_img}")
zoom = storage.count("🔍")
print(f"  殘留「🔍」提示連結 = {zoom}")
m = re.search(r"<ac:image\b.*?</ac:image>", storage, re.S)
if m:
    print("  範例 ac:image：\n   " + safe(m.group(0)[:340]))

hr("2. 真實週報頁 ADF：mediaSingle 數量")
tr = requests.get(f"{BASE}/wiki/api/v2/pages/{PAGE_ID}?body-format=atlas_doc_format",
                  auth=AUTH, timeout=120)
adf = str(((tr.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "") \
    if tr.status_code == 200 else ""
print(f"  ADF -> {tr.status_code}, len={len(adf)}")
c = Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf))
for k in ("mediaSingle", "media", "mediaInline", "inlineExtension", "extension",
          "unsupportedInline", "unsupportedBlock", "textColor", "backgroundColor",
          "table", "panel", "expand", "embedCard", "inlineCard"):
    print(f"    {k:20s} = {c.get(k, 0)}")
print(f"    inline-media-image    = {adf.count('inline-media-image')}")
print(f"    inline-external-image = {adf.count('inline-external-image')}")
ok = c.get("mediaSingle", 0) >= n_ac and n_ac > 0 and adf.count("inline-media-image") == 0
print("\n  結論：" + ("✅ 真實頁面每張圖都是可點擊放大的 mediaSingle"
                      if ok else "❌ 仍有圖片不是 mediaSingle"))

if DO_CLEANUP:
    hr("3. 清理 ZZZ_IMGTEST_* 測試頁")
    sr = requests.get(
        f"{BASE}/wiki/rest/api/content?spaceKey={quote(SPACE_KEY)}&type=page&limit=200",
        auth=AUTH, timeout=60)
    for it in (sr.json().get("results") or []) if sr.status_code == 200 else []:
        title = it.get("title") or ""
        if not title.startswith("ZZZ_IMGTEST_"):
            continue
        if KEEP and str(it["id"]) == str(KEEP):
            print(f"  保留 {title} (id={it['id']})")
            continue
        d = requests.delete(f"{BASE}/wiki/rest/api/content/{it['id']}", auth=AUTH, timeout=40)
        print(f"  刪除 {title} (id={it['id']}) -> {d.status_code}")

hr("VERIFY DONE")
