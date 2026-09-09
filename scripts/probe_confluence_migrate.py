"""驗證 / 回溯修正既有週報頁的圖片（opt-in）。

對 PROBE_PAGE_IDS 內每個頁面：
  * PROBE_MIGRATE=true 時，用上線用的 promote_images_to_block_media 改寫既有 storage
    並設定 editor=v2（不重新產生報告內容，只搬動圖片）。
  * 一律回讀 ADF，報告 mediaSingle 數量作為「點圖可放大」的證據。

絕不輸出任何憑證。
"""

import os
import re
import sys
from collections import Counter

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import daily_worklog_to_confluence as m  # noqa: E402

BASE = m.JIRA_URL
AUTH = m.ADMIN_AUTH
PAGE_IDS = [p.strip() for p in os.environ.get("PROBE_PAGE_IDS", "").split(",") if p.strip()]
DO_MIGRATE = os.environ.get("PROBE_MIGRATE", "false").lower() == "true"
SPACE_KEY = os.environ.get("PROBE_SPACE_KEY", "teamAIoTHW")
_SECRETS = [s for s in (m.ADMIN_EMAIL, m.ADMIN_TOKEN) if s]

if PAGE_IDS == ["auto"]:
    # 自動找出 space 內仍使用舊版行內 <img> 的頁面
    PAGE_IDS = []
    start = 0
    while start < 300:
        lr = requests.get(
            f"{BASE}/wiki/rest/api/content?spaceKey={SPACE_KEY}&type=page"
            f"&limit=25&start={start}&expand=body.storage",
            auth=AUTH, timeout=60)
        if lr.status_code != 200:
            break
        results = lr.json().get("results") or []
        if not results:
            break
        for it in results:
            st = ((it.get("body") or {}).get("storage") or {}).get("value") or ""
            if "/wiki/download/attachments/" in st and "<img" in st:
                PAGE_IDS.append(str(it["id"]))
        start += 25
    print(f"auto 掃描結果：需要回溯修正的頁面 = {PAGE_IDS}")


def safe(t):
    out = str(t)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    return out


def adf_counts(page_id):
    r = requests.get(f"{BASE}/wiki/api/v2/pages/{page_id}?body-format=atlas_doc_format",
                     auth=AUTH, timeout=120)
    adf = str(((r.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "") \
        if r.status_code == 200 else ""
    return r.status_code, adf, Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf))


for pid in PAGE_IDS:
    print("\n" + "=" * 72)
    print(f"== 頁面 {pid}")
    print("=" * 72)

    r = requests.get(
        f"{BASE}/wiki/rest/api/content/{pid}?expand=body.storage,version,space,metadata.properties.editor",
        auth=AUTH, timeout=60)
    if r.status_code != 200:
        print(f"  GET 失敗 {r.status_code}")
        continue
    j = r.json()
    storage = (((j.get("body") or {}).get("storage") or {}).get("value")) or ""
    ver = (j.get("version") or {}).get("number") or 1
    ed = (((j.get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value")
    print(f"  title={safe(j.get('title'))} version={ver} editor={ed!r}")
    print(f"  storage: <img>={len(re.findall(r'<img', storage))} "
          f"<ac:image>={len(re.findall(r'<ac:image', storage))} 🔍={storage.count('🔍')}")

    if DO_MIGRATE:
        soup = BeautifulSoup(storage, "html.parser")
        m.promote_images_to_block_media(soup, pid)
        new_storage = str(soup)
        if new_storage != storage:
            ur = requests.put(f"{BASE}/wiki/rest/api/content/{pid}", auth=AUTH, timeout=120, json={
                "id": pid, "type": "page", "title": j.get("title"),
                "space": {"key": (j.get("space") or {}).get("key")},
                "version": {"number": ver + 1, "minorEdit": True},
                "body": {"storage": {"value": new_storage, "representation": "storage"}},
            })
            print(f"  PUT 改寫 -> {ur.status_code}")
            if ur.status_code != 200:
                print("  " + safe(ur.text[:400]))
        else:
            print("  storage 無變化，略過寫入")
        m.ensure_page_editor_v2(pid)

    status, adf, c = adf_counts(pid)
    n_ac = len(re.findall(r"<ac:image", (requests.get(
        f"{BASE}/wiki/rest/api/content/{pid}?expand=body.storage", auth=AUTH, timeout=60
    ).json().get("body") or {}).get("storage", {}).get("value") or ""))
    print(f"  ADF -> {status}, len={len(adf)}")
    for k in ("mediaSingle", "media", "inlineExtension", "unsupportedInline",
              "unsupportedBlock", "textColor", "table", "panel", "embedCard"):
        print(f"    {k:20s} = {c.get(k, 0)}")
    print(f"    inline-media-image    = {adf.count('inline-media-image')}")
    print(f"    inline-external-image = {adf.count('inline-external-image')}")
    ok = n_ac > 0 and c.get("mediaSingle", 0) >= n_ac \
        and adf.count("inline-media-image") == 0 and adf.count("inline-external-image") == 0
    print(f"  storage <ac:image>={n_ac} / ADF mediaSingle={c.get('mediaSingle', 0)}")
    print("  結論：" + ("✅ 每張圖都是可點擊放大的 mediaSingle" if ok else "❌ 仍有圖片不可放大"))

print("\nMIGRATE/VERIFY DONE")
