"""一次性、唯讀的診斷工具（opt-in，可刪除）。

目的：蒐集判斷「Confluence Cloud 點圖放大（lightbox）」可行性所需的實證：
  1. 目標頁面的 editor 屬性（v1 = legacy renderer / v2 = Fabric renderer）
  2. 目標頁面 body.storage 中 <ac:image> / <img> / <a> 的真實 XML
  3. 同一個 space 內「由編輯器 UI 插入」圖片的真實 storage XML（比對範本）
  4. 附件各種下載網址的真實 Content-Type / Content-Disposition
  5. 頁面是否能以 ADF (atlas_doc_format) 讀取

此腳本只做 GET，不修改任何內容，且絕不輸出憑證。
"""

import os
import re
import sys
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse

RAW_URL = os.environ.get("CONF_URL", "")
USER = os.environ.get("CONF_USER", "")
TOKEN = os.environ.get("CONF_PASS", "")
PAGE_ID = os.environ.get("PROBE_PAGE_ID", "208109570")
SPACE_KEY = os.environ.get("PROBE_SPACE_KEY", "teamAIoTHW")

if not RAW_URL or not USER or not TOKEN:
    print("MISSING_CREDENTIALS")
    sys.exit(1)

_p = urlparse(RAW_URL)
BASE = f"{_p.scheme}://{_p.netloc}"
AUTH = HTTPBasicAuth(USER, TOKEN)

_SECRETS = [s for s in (USER, TOKEN) if s]


def safe(text):
    """移除任何可能的憑證字串。"""
    out = str(text)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    # Basic base64 / token 樣式保險
    out = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1<REDACTED>", out)
    return out


def hr(title):
    print("\n" + "=" * 72)
    print(f"== {title}")
    print("=" * 72)


def get(url, **kw):
    return requests.get(url, auth=AUTH, timeout=40, **kw)


# ---------------------------------------------------------------- 1. editor 屬性
hr("1. 目標頁面 editor 屬性 / 版本")
url = f"{BASE}/wiki/rest/api/content/{PAGE_ID}?expand=body.storage,version,metadata.properties.editor,space"
r = get(url)
print(f"GET /wiki/rest/api/content/{PAGE_ID} -> {r.status_code}")
page = {}
if r.status_code == 200:
    page = r.json()
    props = ((page.get("metadata") or {}).get("properties") or {})
    editor = (props.get("editor") or {})
    print(f"  title            : {safe(page.get('title'))}")
    print(f"  space            : {safe(((page.get('space') or {}).get('key')))}")
    print(f"  version.number   : {(page.get('version') or {}).get('number')}")
    print(f"  editor property  : {editor.get('value')!r}  (None/v1=legacy, v2=Fabric)")
    print(f"  editor prop ver  : {((editor.get('version') or {}).get('number'))}")
    print(f"  editor prop id   : {editor.get('id')}")
else:
    print("  " + safe(r.text[:400]))

storage = (((page.get("body") or {}).get("storage") or {}).get("value")) or ""
print(f"  body.storage 長度 : {len(storage)}")

# ---------------------------------------------------------- 2. 目前頁面圖片標記
hr("2. 目標頁面現有圖片相關標記（去重、最多各 6 筆）")


def show_matches(label, pattern, text, limit=6, flags=re.I | re.S):
    found = re.findall(pattern, text, flags)
    uniq = []
    for f in found:
        s = f if isinstance(f, str) else f[0]
        if s not in uniq:
            uniq.append(s)
    print(f"\n-- {label}: 共 {len(found)} 筆，去重 {len(uniq)} 筆")
    for s in uniq[:limit]:
        print("   " + safe(s[:400]))


show_matches("<ac:image ...> 完整節點", r"<ac:image\b.*?</ac:image>|<ac:image\b[^>]*/>", storage)
show_matches("<img ...>", r"<img\b[^>]*>", storage)
show_matches("<a ...> 開頭標籤", r"<a\b[^>]*>", storage)
show_matches("ri:attachment", r"<ri:attachment\b[^>]*>", storage)

# --------------------------------------------- 3. 找出「編輯器 UI 插入」的圖片範本
hr("3. 掃描 space 內由編輯器插入的圖片 storage XML（範本比對）")
EDITOR_SIGNATURES = ("ri:version-at-save", "ac:original-width", "ac:original-height", "ac:local-id")

start = 0
scanned = 0
hits = 0
while start < 200:
    lurl = (
        f"{BASE}/wiki/rest/api/content?spaceKey={quote(SPACE_KEY)}&type=page"
        f"&limit=25&start={start}&expand=body.storage,metadata.properties.editor"
    )
    lr = get(lurl)
    if lr.status_code != 200:
        print(f"  列表查詢失敗 {lr.status_code}: {safe(lr.text[:200])}")
        break
    results = lr.json().get("results") or []
    if not results:
        break
    for it in results:
        scanned += 1
        st = ((it.get("body") or {}).get("storage") or {}).get("value") or ""
        if not any(sig in st for sig in EDITOR_SIGNATURES):
            continue
        ed = (((it.get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value")
        nodes = re.findall(r"<ac:image\b.*?</ac:image>|<ac:image\b[^>]*/>", st, re.I | re.S)
        nodes = [n for n in nodes if any(sig in n for sig in EDITOR_SIGNATURES)]
        if not nodes:
            continue
        hits += 1
        print(f"\n  >>> 頁面 id={it.get('id')} editor={ed!r} title={safe(it.get('title'))}")
        for n in nodes[:3]:
            print("      " + safe(n[:500]))
        if hits >= 8:
            break
    if hits >= 8:
        break
    start += 25

print(f"\n  掃描頁面數={scanned}，找到含編輯器簽章的頁面數={hits}")

# --------------------------------------------------------------- 4. ADF 可讀性
hr("4. 頁面是否能以 ADF (atlas_doc_format) 讀取 / 寫入")
for fmt in ("atlas_doc_format", "storage"):
    ar = get(f"{BASE}/wiki/api/v2/pages/{PAGE_ID}?body-format={fmt}")
    body = ""
    if ar.status_code == 200:
        body = str(((ar.json().get("body") or {}).get(fmt) or {}).get("value") or "")
    print(f"  GET /wiki/api/v2/pages/{PAGE_ID}?body-format={fmt} -> {ar.status_code}, len={len(body)}")
    if fmt == "atlas_doc_format" and body:
        print("    ADF 前 600 字：" + safe(body[:600]))
    elif ar.status_code != 200:
        print("    " + safe(ar.text[:300]))

# -------------------------------------------------- 5. 附件下載網址 header 實測
hr("5. 附件下載網址真實 Content-Type / Content-Disposition")
ar = get(f"{BASE}/wiki/rest/api/content/{PAGE_ID}/child/attachment?limit=100")
atts = ar.json().get("results") or [] if ar.status_code == 200 else []
print(f"  附件數 = {len(atts)}")
img_atts = [a for a in atts if str((a.get("metadata") or {}).get("mediaType") or a.get("extensions", {}).get("mediaType", "")).startswith("image")]
if not img_atts:
    img_atts = [a for a in atts if re.search(r"\.(png|jpe?g|gif|webp|bmp)$", a.get("title") or "", re.I)]
print(f"  圖片附件數 = {len(img_atts)}")

for a in img_atts[:3]:
    print(f"    - id={a.get('id')} title={safe(a.get('title'))} "
          f"mediaType={safe((a.get('extensions') or {}).get('mediaType'))} "
          f"downloadLink={safe((a.get('_links') or {}).get('download'))}")

if img_atts:
    att = img_atts[0]
    att_id = att["id"]
    fn = att["title"]
    dl = ((att.get("_links") or {}).get("download") or "")
    enc = quote(fn)

    candidates = [
        ("wiki/download 純路徑", f"{BASE}/wiki/download/attachments/{PAGE_ID}/{enc}"),
        ("wiki/download api=v2", f"{BASE}/wiki/download/attachments/{PAGE_ID}/{enc}?api=v2"),
        ("wiki/download download=true", f"{BASE}/wiki/download/attachments/{PAGE_ID}/{enc}?api=v2&download=true"),
        ("wiki/download preview=true", f"{BASE}/wiki/download/attachments/{PAGE_ID}/{enc}?api=v2&preview=true"),
        ("wiki/download inline=true", f"{BASE}/wiki/download/attachments/{PAGE_ID}/{enc}?api=v2&inline=true"),
        ("_links.download 原樣", f"{BASE}/wiki{dl}" if dl.startswith("/") else dl),
        ("v1 content/{attId}/download", f"{BASE}/wiki/rest/api/content/{att_id}/download"),
    ]

    for label, u in candidates:
        if not u:
            continue
        try:
            resp = requests.get(u, auth=AUTH, timeout=40, stream=True, allow_redirects=True)
            h = resp.headers
            hist = " -> ".join(str(x.status_code) for x in resp.history)
            final = urlparse(resp.url)
            print(f"\n  [{label}]")
            print(f"    request path : {safe(urlparse(u).path)}?{safe(urlparse(u).query)}")
            print(f"    status       : {resp.status_code}" + (f" (redirects {hist})" if hist else ""))
            print(f"    final host   : {final.netloc}")
            print(f"    final path   : {safe(final.path)}")
            print(f"    Content-Type : {h.get('Content-Type')}")
            print(f"    Content-Disp : {safe(h.get('Content-Disposition'))}")
            print(f"    Content-Len  : {h.get('Content-Length')}")
            resp.close()
        except Exception as e:
            print(f"\n  [{label}] 例外: {safe(e)}")

hr("PROBE DONE")
