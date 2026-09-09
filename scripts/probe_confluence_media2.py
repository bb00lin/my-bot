"""第二階段診斷（opt-in，唯讀 + 建立一個可刪除的測試頁）。

目的：
  A. 統計目標頁 storage -> ADF 轉換結果的節點型別，判斷「改用 Fabric renderer(v2)」
     是否會破壞現有配色 / 版面（textColor / backgroundColor / unsupported* 節點）。
  B. 讀取已知可放大的 v2 頁面（編輯器插入圖片）的 ADF，取得 media 節點真實結構。
  C. 建立一個測試頁：把目標頁的 <img> 換成「編輯器等價」的 <ac:image>，
     並設定 editor=v2，再讀回 ADF 確認圖片是否變成 mediaSingle/media 節點。
  D. 產出 Confluence 原生 ?preview= 連結（不需 lightbox 的備援方案）。

絕不輸出任何憑證。
"""

import json
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
GOOD_PAGE_ID = os.environ.get("PROBE_GOOD_PAGE_ID", "9830401")
SPACE_KEY = os.environ.get("PROBE_SPACE_KEY", "teamAIoTHW")
MAKE_TEST_PAGE = os.environ.get("PROBE_MAKE_TEST_PAGE", "true").lower() == "true"

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


def count_nodes(adf_text):
    """統計 ADF 中 type / mark 出現次數。"""
    types = Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf_text))
    return types


# ------------------------------------------------------- A. 目標頁 ADF 節點統計
hr("A. 目標頁 storage -> ADF 轉換節點統計（評估轉 v2 的風險）")
r = requests.get(f"{BASE}/wiki/api/v2/pages/{PAGE_ID}?body-format=atlas_doc_format",
                 auth=AUTH, timeout=60)
print(f"GET v2 pages/{PAGE_ID} atlas_doc_format -> {r.status_code}")
adf_target = ""
if r.status_code == 200:
    adf_target = str(((r.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "")
print(f"  ADF 長度 = {len(adf_target)}")
c = count_nodes(adf_target)
KEYS = ["media", "mediaSingle", "mediaGroup", "mediaInline", "unsupportedInline",
        "unsupportedBlock", "textColor", "backgroundColor", "extension",
        "bodiedExtension", "inlineExtension", "table", "layoutSection",
        "paragraph", "text", "link", "confluenceUnsupportedInline",
        "confluenceUnsupportedBlock"]
for k in KEYS:
    print(f"  {k:32s} = {c.get(k, 0)}")
print("\n  其他前 20 種節點型別：")
for k, v in c.most_common(28):
    if k not in KEYS:
        print(f"    {k:30s} = {v}")

# 現有 <img> 在 ADF 中變成什麼？
hr("A2. 目前 <img> 在 ADF 中的對應節點")
for m in re.finditer(r'.{160}wl_[A-Z]', adf_target):
    print("   ..." + safe(m.group(0)[:200]))
    break
idx = adf_target.find("wl_")
if idx > 0:
    print("   附近片段：" + safe(adf_target[max(0, idx - 300):idx + 200]))
else:
    print("   ADF 中找不到 wl_ 檔名（表示 <img> 可能在轉換時被丟棄或轉為外部連結）")

# ---------------------------------------- B. 已知可放大頁面（v2 + 編輯器插入圖）
hr("B. 已知可點擊放大的 v2 頁面 ADF media 節點真實結構")
r2 = requests.get(f"{BASE}/wiki/api/v2/pages/{GOOD_PAGE_ID}?body-format=atlas_doc_format",
                  auth=AUTH, timeout=60)
print(f"GET v2 pages/{GOOD_PAGE_ID} atlas_doc_format -> {r2.status_code}")
adf_good = ""
if r2.status_code == 200:
    adf_good = str(((r2.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "")
print(f"  ADF 長度 = {len(adf_good)}")
i = adf_good.find('"mediaSingle"')
if i >= 0:
    print("  mediaSingle 片段：\n   " + safe(adf_good[i - 60:i + 520]))
else:
    j = adf_good.find('"media"')
    print("  找不到 mediaSingle；media 片段：\n   " + safe(adf_good[max(0, j - 120):j + 420]))

# --------------------------------------------------------- C. 建立測試頁驗證
hr("C. 建立測試頁：<ac:image>（編輯器等價屬性）+ editor=v2")

# 取得目標頁 storage 與附件
sr = requests.get(f"{BASE}/wiki/rest/api/content/{PAGE_ID}?expand=body.storage",
                  auth=AUTH, timeout=60)
storage = ""
if sr.status_code == 200:
    storage = (((sr.json().get("body") or {}).get("storage") or {}).get("value")) or ""
print(f"  取得目標頁 storage 長度 = {len(storage)}")

ar = requests.get(f"{BASE}/wiki/rest/api/content/{PAGE_ID}/child/attachment?limit=200&expand=version",
                  auth=AUTH, timeout=60)
atts = (ar.json().get("results") or []) if ar.status_code == 200 else []
by_name = {a["title"]: a for a in atts}
print(f"  目標頁附件數 = {len(atts)}")


def png_jpeg_size(data):
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            import struct
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
    except Exception:
        pass
    return None


def build_ac_image(fn, width=640):
    a = by_name.get(fn)
    ver = 1
    if a:
        ver = ((a.get("version") or {}).get("number")) or 1
    ow, oh = 1200, 800
    dl = ((a or {}).get("_links") or {}).get("download") or ""
    if dl:
        try:
            b = requests.get(f"{BASE}/wiki{dl}", auth=AUTH, timeout=40).content
            sz = png_jpeg_size(b)
            if sz:
                ow, oh = sz
        except Exception:
            pass
    w = min(width, ow)
    return (
        f'<ac:image ac:align="center" ac:layout="center" '
        f'ac:original-height="{oh}" ac:original-width="{ow}" '
        f'ac:custom-width="true" ac:alt="{fn}" ac:width="{w}">'
        f'<ri:attachment ri:filename="{fn}" ri:version-at-save="{ver}" />'
        f"</ac:image>"
    )


# 把 <a ...><img .../></a>（或裸 <img/>）換成 ac:image
IMG_RE = re.compile(r'(?:<a\b[^>]*>\s*)?<img\b[^>]*?src="[^"]*?/wiki/download/attachments/\d+/([^"?]+)"[^>]*/?>(?:\s*</a>)?', re.I)
replaced = {"n": 0}


def _sub(m):
    fn = m.group(1)
    from urllib.parse import unquote
    fn = unquote(fn)
    if fn not in by_name:
        return m.group(0)
    replaced["n"] += 1
    return build_ac_image(fn)


test_storage = IMG_RE.sub(_sub, storage)
# 移除舊的可見「在 Jira 開啟原圖」文字連結，避免干擾判讀
test_storage = re.sub(r'<a\b[^>]*>\s*🔍[^<]*</a>', '', test_storage)
print(f"  已替換 <img> -> <ac:image> 數量 = {replaced['n']}")

test_page_id = None
if MAKE_TEST_PAGE and replaced["n"] > 0:
    title = "ZZZ_IMGTEST_lightbox_probe"
    # 若已存在先刪掉
    ex = requests.get(f"{BASE}/wiki/rest/api/content?spaceKey={quote(SPACE_KEY)}&title={quote(title)}",
                      auth=AUTH, timeout=40)
    for it in (ex.json().get("results") or []) if ex.status_code == 200 else []:
        requests.delete(f"{BASE}/wiki/rest/api/content/{it['id']}", auth=AUTH, timeout=40)
        print(f"  已刪除舊測試頁 {it['id']}")

    payload = {
        "type": "page",
        "title": title,
        "space": {"key": SPACE_KEY},
        "body": {"storage": {"value": test_storage, "representation": "storage"}},
        "metadata": {"properties": {"editor": {"key": "editor", "value": "v2"}}},
    }
    cr = requests.post(f"{BASE}/wiki/rest/api/content", auth=AUTH, json=payload, timeout=90)
    print(f"  建立測試頁 -> {cr.status_code}")
    if cr.status_code in (200, 201):
        test_page_id = cr.json()["id"]
        print(f"  測試頁 id = {test_page_id}")
        print(f"  測試頁 URL = {BASE}/wiki/spaces/{SPACE_KEY}/pages/{test_page_id}")

        # 複製附件（圖片）到測試頁，讓 ri:attachment 解析得到
        copied = 0
        for a in atts:
            fn = a["title"]
            if fn not in by_name:
                continue
            if not re.search(r"\.(png|jpe?g|gif|webp)$", fn, re.I):
                continue
            dl = (a.get("_links") or {}).get("download") or ""
            if not dl:
                continue
            b = requests.get(f"{BASE}/wiki{dl}", auth=AUTH, timeout=60).content
            up = requests.post(
                f"{BASE}/wiki/rest/api/content/{test_page_id}/child/attachment",
                auth=AUTH, headers={"X-Atlassian-Token": "no-check"},
                files={"file": (fn, b, (a.get("extensions") or {}).get("mediaType") or "image/png")},
                timeout=90,
            )
            if up.status_code in (200, 201):
                copied += 1
        print(f"  已複製圖片附件 {copied} 個到測試頁")

        # 確認 editor 屬性
        pr = requests.get(f"{BASE}/wiki/rest/api/content/{test_page_id}?expand=metadata.properties.editor",
                          auth=AUTH, timeout=40)
        ed = None
        if pr.status_code == 200:
            ed = (((pr.json().get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value")
        print(f"  測試頁 editor 屬性 = {ed!r}")

        # 讀回 ADF，確認是否出現 mediaSingle / media
        tr = requests.get(f"{BASE}/wiki/api/v2/pages/{test_page_id}?body-format=atlas_doc_format",
                          auth=AUTH, timeout=90)
        adf_test = ""
        if tr.status_code == 200:
            adf_test = str(((tr.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "")
        print(f"  測試頁 ADF -> {tr.status_code}, len={len(adf_test)}")
        ct = count_nodes(adf_test)
        for k in ["mediaSingle", "media", "mediaGroup", "unsupportedInline", "unsupportedBlock",
                  "textColor", "backgroundColor", "extension", "table"]:
            print(f"    {k:22s} = {ct.get(k, 0)}")
        k = adf_test.find('"mediaSingle"')
        if k >= 0:
            print("    mediaSingle 片段：\n     " + safe(adf_test[max(0, k - 80):k + 520]))
        else:
            print("    ⚠️ 測試頁 ADF 中沒有 mediaSingle")
    else:
        print("  " + safe(cr.text[:600]))

# --------------------------------------------- D. Confluence 原生 preview 連結
hr("D. Confluence 原生 ?preview= 連結（備援方案，直接開原生檢視器）")
if atts:
    a = atts[0]
    num_id = re.sub(r"^att", "", a["id"])
    print(f"  範例：{BASE}/wiki/spaces/{SPACE_KEY}/pages/{PAGE_ID}"
          f"?preview=%2F{PAGE_ID}%2F{num_id}%2F{quote(a['title'])}")
    print("  （此連結會開啟頁面並自動彈出 Confluence 原生圖片檢視器）")

hr("PROBE2 DONE")
if test_page_id:
    print(f"TEST_PAGE_URL={BASE}/wiki/spaces/{SPACE_KEY}/pages/{test_page_id}")
