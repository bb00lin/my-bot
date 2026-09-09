"""第三階段診斷：找出「什麼樣的 storage 才會轉成可放大的 mediaSingle」。

做法：建立一個測試頁，先上傳附件，再寫入 5 種不同擺放方式的 <ac:image>，
每種用不同檔名以便回讀 ADF 時歸因；最後把 editor 屬性設成 v2 並確認生效。

輸出：每個變體對應的 ADF 節點型別（mediaSingle / mediaInline / inlineExtension）。
絕不輸出任何憑證。
"""

import json
import os
import re
import struct
import sys
from urllib.parse import quote, urlparse

import requests
from requests.auth import HTTPBasicAuth

RAW_URL = os.environ.get("CONF_URL", "")
USER = os.environ.get("CONF_USER", "")
TOKEN = os.environ.get("CONF_PASS", "")
SRC_PAGE_ID = os.environ.get("PROBE_PAGE_ID", "208109570")
SPACE_KEY = os.environ.get("PROBE_SPACE_KEY", "teamAIoTHW")
TITLE = "ZZZ_IMGTEST_variants_probe"

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


def png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return None


# ------------------------------------------------ 取一張來源頁的真實圖片附件
hr("0. 取得一張測試用圖片")
ar = requests.get(f"{BASE}/wiki/rest/api/content/{SRC_PAGE_ID}/child/attachment?limit=100",
                  auth=AUTH, timeout=60)
atts = (ar.json().get("results") or []) if ar.status_code == 200 else []
src = next((a for a in atts if re.search(r"\.png$", a["title"], re.I)), None)
if not src:
    print("找不到 png 附件"); sys.exit(1)
blob = requests.get(f"{BASE}/wiki{(src.get('_links') or {}).get('download')}",
                    auth=AUTH, timeout=60).content
dims = png_size(blob) or (1200, 800)
print(f"  來源檔 = {safe(src['title'])}, bytes={len(blob)}, 原始尺寸={dims}")
OW, OH = dims
W = min(640, OW)

# ------------------------------------------------------------- 刪除舊測試頁
ex = requests.get(f"{BASE}/wiki/rest/api/content?spaceKey={quote(SPACE_KEY)}&title={quote(TITLE)}",
                  auth=AUTH, timeout=40)
for it in (ex.json().get("results") or []) if ex.status_code == 200 else []:
    requests.delete(f"{BASE}/wiki/rest/api/content/{it['id']}", auth=AUTH, timeout=40)
    print(f"  已刪除舊測試頁 {it['id']}")

# --------------------------------------------------- 1. 先建立空白頁 + 上傳附件
hr("1. 建立空白測試頁並先上傳附件（確保 ri:filename 解析得到）")
cr = requests.post(f"{BASE}/wiki/rest/api/content", auth=AUTH, timeout=60, json={
    "type": "page", "title": TITLE, "space": {"key": SPACE_KEY},
    "body": {"storage": {"value": "<p>init</p>", "representation": "storage"}},
})
print(f"  建立頁面 -> {cr.status_code}")
if cr.status_code not in (200, 201):
    print(safe(cr.text[:500])); sys.exit(1)
PID = cr.json()["id"]
print(f"  測試頁 id={PID}  URL={BASE}/wiki/spaces/{SPACE_KEY}/pages/{PID}")

VARIANTS = {
    "v1_block_alone": "圖片單獨佔一個 <p>（block）",
    "v2_inline_with_text": "圖片與文字同一個 <p>（inline）",
    "v3_like_current_report": "模擬目前週報結構：span+br+圖片同一個 <p>",
    "v4_in_table_cell_block": "表格內單獨 <p> 的圖片",
    "v5_block_thumbnail": "block 且 ac:thumbnail=true",
}
names = {}
for key in VARIANTS:
    fn = f"probe_{key}.png"
    names[key] = fn
    up = requests.post(f"{BASE}/wiki/rest/api/content/{PID}/child/attachment",
                       auth=AUTH, headers={"X-Atlassian-Token": "no-check"},
                       files={"file": (fn, blob, "image/png")}, timeout=90)
    print(f"  上傳 {fn} -> {up.status_code}")


def ac_img(fn, thumbnail=False, extra=""):
    thumb = ' ac:thumbnail="true"' if thumbnail else ""
    return (
        f'<ac:image ac:align="center" ac:layout="center"'
        f' ac:original-height="{OH}" ac:original-width="{OW}"'
        f' ac:custom-width="true" ac:alt="{fn}" ac:width="{W}"{thumb}{extra}>'
        f'<ri:attachment ri:filename="{fn}" ri:version-at-save="1" />'
        f"</ac:image>"
    )


# ----------------------------------------------------------- 2. 寫入 5 種變體
hr("2. 寫入 5 種擺放方式")
body = "".join([
    "<h2>v1 block alone</h2>",
    f"<p>{ac_img(names['v1_block_alone'])}</p>",

    "<h2>v2 inline with text</h2>",
    f"<p>前綴文字 {ac_img(names['v2_inline_with_text'])} 後綴文字</p>",

    "<h2>v3 like current report</h2>",
    '<p><span style="color: #555555;">-------- &#9492; &#128221; </span><br />'
    f"{ac_img(names['v3_like_current_report'])}<br />"
    '<a href="https://example.com">visible link</a></p>',

    "<h2>v4 in table cell (block)</h2>",
    "<table><tbody><tr><td>"
    f"<p>{ac_img(names['v4_in_table_cell_block'])}</p>"
    "</td></tr></tbody></table>",

    "<h2>v5 block + thumbnail</h2>",
    f"<p>{ac_img(names['v5_block_thumbnail'], thumbnail=True)}</p>",
])

gv = requests.get(f"{BASE}/wiki/rest/api/content/{PID}?expand=version", auth=AUTH, timeout=40)
ver = (gv.json().get("version") or {}).get("number", 1)
ur = requests.put(f"{BASE}/wiki/rest/api/content/{PID}", auth=AUTH, timeout=90, json={
    "id": PID, "type": "page", "title": TITLE,
    "space": {"key": SPACE_KEY},
    "version": {"number": ver + 1},
    "body": {"storage": {"value": body, "representation": "storage"}},
})
print(f"  更新內容 -> {ur.status_code}")
if ur.status_code != 200:
    print(safe(ur.text[:600]))

# ------------------------------------------------------ 3. 設定 editor = v2
hr("3. 設定 editor 內容屬性 = v2（POST metadata 無效，改用 property API）")
pg = requests.get(f"{BASE}/wiki/rest/api/content/{PID}/property/editor", auth=AUTH, timeout=40)
print(f"  GET property/editor -> {pg.status_code}")
if pg.status_code == 200:
    n = ((pg.json().get("version") or {}).get("number") or 1)
    pr = requests.put(f"{BASE}/wiki/rest/api/content/{PID}/property/editor", auth=AUTH, timeout=40,
                      json={"key": "editor", "value": "v2", "version": {"number": n + 1}})
    print(f"  PUT property/editor -> {pr.status_code}")
else:
    pr = requests.post(f"{BASE}/wiki/rest/api/content/{PID}/property", auth=AUTH, timeout=40,
                       json={"key": "editor", "value": "v2"})
    print(f"  POST property (editor=v2) -> {pr.status_code} {safe(pr.text[:200])}")

chk = requests.get(f"{BASE}/wiki/rest/api/content/{PID}?expand=metadata.properties.editor",
                   auth=AUTH, timeout=40)
edv = None
if chk.status_code == 200:
    edv = (((chk.json().get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value")
print(f"  確認 editor 屬性 = {edv!r}")

# --------------------------------------------------- 4. 回讀 ADF 並逐變體歸因
hr("4. 回讀 ADF：每個變體被轉成什麼節點")
tr = requests.get(f"{BASE}/wiki/api/v2/pages/{PID}?body-format=atlas_doc_format",
                  auth=AUTH, timeout=90)
adf = ""
if tr.status_code == 200:
    adf = str(((tr.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "")
print(f"  ADF -> {tr.status_code}, len={len(adf)}")

for key, desc in VARIANTS.items():
    fn = names[key]
    print(f"\n  --- {key}  ({desc})")
    i = adf.find(fn)
    if i < 0:
        print("      ⚠️ ADF 中找不到此檔名（圖片被丟棄）")
        continue
    ctx = adf[max(0, i - 420):i + 260]
    node = "UNKNOWN"
    for cand in ("mediaSingle", "mediaInline", "inline-external-image",
                 "inline-media-image", "inlineExtension", "extension", "media"):
        if cand in ctx:
            node = cand
            break
    print(f"      判定節點 = {node}")
    print("      片段：" + safe(ctx))

# 也回讀 storage 看 Confluence 是否重寫了我們的 XML
hr("5. Confluence 實際保存的 storage（確認屬性有無被改寫）")
sr = requests.get(f"{BASE}/wiki/rest/api/content/{PID}?expand=body.storage", auth=AUTH, timeout=60)
st = ((((sr.json().get("body") or {}).get("storage") or {}).get("value")) or "") if sr.status_code == 200 else ""
for n in re.findall(r"<ac:image\b.*?</ac:image>", st, re.I | re.S)[:5]:
    print("   " + safe(n[:400]))

hr("PROBE3 DONE")
print(f"TEST_PAGE_URL={BASE}/wiki/spaces/{SPACE_KEY}/pages/{PID}")
