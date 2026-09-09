"""部署後驗證（唯讀、opt-in、不輸出任何憑證）。

1. 週報頁 storage 的 <ac:image> 數量、對齊屬性、殘留 <img>／🔍。
2. 同頁 ADF：mediaSingle 是否等於 <ac:image> 數；inline-media-image /
   unsupportedInline / unsupportedBlock 是否為 0。
3. 指定任務（預設 PBY-11）各日列的區塊順序，確認文字與圖片是交錯的。
4. 💬 留言連結數量與範例。

驗證完成後可刪除本檔與 .github/workflows/debug_weekly_verify.yml。
"""

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
PAGE_ID = os.environ.get("PROBE_PAGE_ID", "214925313")
TARGET_KEY = os.environ.get("PROBE_ISSUE_KEY", "PBY-11")

if not RAW_URL or not USER or not TOKEN:
    print("MISSING_CREDENTIALS")
    sys.exit(1)

_p = urlparse(RAW_URL)
BASE = f"{_p.scheme}://{_p.netloc}"
AUTH = HTTPBasicAuth(USER, TOKEN)
_SECRETS = [s for s in (USER, TOKEN) if s]


def safe(text):
    out = str(text)
    for s in _SECRETS:
        out = out.replace(s, "<REDACTED>")
    return out


def hr(title):
    print("\n" + "=" * 72)
    print(f"== {title}")
    print("=" * 72)


problems = []

hr("1. storage 狀態")
r = requests.get(
    f"{BASE}/wiki/rest/api/content/{PAGE_ID}"
    "?expand=body.storage,version,metadata.properties.editor",
    auth=AUTH, timeout=90)
print(f"GET content -> {r.status_code}")
if r.status_code != 200:
    print(safe(r.text[:300]))
    sys.exit(1)

j = r.json()
storage = (((j.get("body") or {}).get("storage") or {}).get("value")) or ""
editor = (((j.get("metadata") or {}).get("properties") or {}).get("editor") or {}).get("value")
print(f"  title       = {safe(j.get('title'))}")
print(f"  version     = {(j.get('version') or {}).get('number')}")
print(f"  editor      = {editor!r}  (需為 'v2')")

n_ac = len(re.findall(r"<ac:image\b", storage))
n_img = len(re.findall(r"<img\b", storage))
n_left = len(re.findall(r'<ac:image[^>]*ac:align="left"', storage))
n_start = len(re.findall(r'<ac:image[^>]*ac:layout="align-start"', storage))
n_center = len(re.findall(r'<ac:image[^>]*ac:align="center"', storage))
n_zoom = storage.count("🔍")
n_cmt = len(re.findall(r"focusedCommentId=", storage))
print(f"  <ac:image>            = {n_ac}")
print(f"  殘留 <img>            = {n_img}")
print(f"  ac:align=left         = {n_left}")
print(f"  ac:layout=align-start = {n_start}")
print(f"  ac:align=center       = {n_center}")
print(f"  殘留 🔍 連結          = {n_zoom}")
print(f"  💬 留言連結           = {n_cmt}")

if n_img:
    problems.append(f"殘留 {n_img} 個 HTML <img>")
if n_zoom:
    problems.append(f"殘留 {n_zoom} 個 🔍 連結")
if n_ac and (n_left != n_ac or n_start != n_ac):
    problems.append(f"對齊屬性不齊：left={n_left} align-start={n_start} / 共 {n_ac}")
if n_center:
    problems.append(f"仍有 {n_center} 張置中圖片")

if storage:
    ex = re.search(r"<ac:image\b.*?</ac:image>", storage, re.S)
    if ex:
        print("\n  範例 ac:image：\n   " + safe(ex.group(0)[:340]))

hr("2. ADF 節點統計")
tr = requests.get(f"{BASE}/wiki/api/v2/pages/{PAGE_ID}?body-format=atlas_doc_format",
                  auth=AUTH, timeout=180)
adf = ""
if tr.status_code == 200:
    adf = str(((tr.json().get("body") or {}).get("atlas_doc_format") or {}).get("value") or "")
print(f"  ADF -> {tr.status_code}, len={len(adf)}")
counts = Counter(re.findall(r'"type"\s*:\s*"([A-Za-z0-9_]+)"', adf))
for k in ("mediaSingle", "media", "mediaInline", "inlineExtension", "extension",
          "unsupportedInline", "unsupportedBlock", "table", "panel"):
    print(f"    {k:20s} = {counts.get(k, 0)}")
n_inline_media = adf.count("inline-media-image")
print(f"    inline-media-image   = {n_inline_media}")
n_align_start = adf.count('"layout":"align-start"') + adf.count('"layout": "align-start"')
print(f"    layout=align-start   = {n_align_start}")

if n_ac and counts.get("mediaSingle", 0) != n_ac:
    problems.append(f"mediaSingle={counts.get('mediaSingle', 0)} != <ac:image>={n_ac}")
if n_inline_media:
    problems.append(f"出現 {n_inline_media} 個 inline-media-image（點擊放大會失效）")
for k in ("unsupportedInline", "unsupportedBlock"):
    if counts.get(k, 0):
        problems.append(f"{k} = {counts.get(k)}")

hr(f"3. {TARGET_KEY} 各日區塊順序")


def block_kind(el):
    ac_img = el.find("ac:image")
    if ac_img is not None:
        ri = ac_img.find("ri:attachment")
        return "IMG  ", (ri.get("ri:filename") if ri else "?")
    text = re.sub(r"-{2,}", "", el.get_text(" ", strip=True)).strip()
    return "text ", text[:110]


soup = BeautifulSoup(storage, "html.parser")
found_rows = 0
for body in soup.find_all("ac:rich-text-body"):
    current_key = None
    for child in body.find_all(recursive=False):
        raw = str(child)
        if child.name == "p" and re.match(r"^\s*\d+\.", child.get_text(" ", strip=True)):
            keys = re.findall(r"/browse/([A-Z][A-Z0-9]+-\d+)", raw)
            current_key = keys[0] if keys else None
            continue
        if current_key != TARGET_KEY or child.name != "div":
            continue
        found_rows += 1
        label = re.sub(r"-{2,}", "", (child.find("p").get_text(" ", strip=True)
                                      if child.find("p") else "")).strip()
        print(f"\n  ── 日列: {label[:90]}")
        for sub in child.find_all(recursive=False):
            kind, val = block_kind(sub)
            print(f"     {kind}| {val}")

if not found_rows:
    problems.append(f"storage 中找不到 {TARGET_KEY} 的日列（無法確認順序）")

hr("4. 留言連結範例")
for a in soup.find_all("a", href=re.compile("focusedCommentId")):
    print(f"  {a.get_text(strip=True)}  ->  {a.get('href')}")

hr("結論")
if problems:
    print("❌ 有問題：")
    for p in problems:
        print("   - " + p)
    sys.exit(1)
print("✅ 全部通過：圖片皆為靠左 mediaSingle、無 inline-media-image、無 unsupported 節點")
