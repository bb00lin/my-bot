"""本地離線驗證：確認 promote_images_to_block_media 產生的 storage 是 block 層 ac:image。

不連線 Confluence；用假環境變數載入模組，並 monkeypatch 需要網路的函式。
"""
import os
import sys

os.environ.setdefault("CONF_URL", "https://example.atlassian.net")
os.environ.setdefault("CONF_USER", "dummy@example.com")
os.environ.setdefault("CONF_PASS", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_worklog_to_confluence as m  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

# 不要真的連線抓附件版本／尺寸
m.refresh_conf_image_meta = lambda page_id, filenames: None
m._CONF_IMAGE_META.update({
    "wl_PN-17_shot.png": {"width": 1600, "height": 900, "version": 3, "alt": "shot.png"},
    "wl_PBA-255_old.png": {"width": 800, "height": 600, "version": 1, "alt": "old.png"},
})

# 模擬目前週報的真實結構：表格 -> td -> p -> span(+br) -> 圖片；外加前次執行殘留的 <img> + 🔍 連結
html = (
    '<table><tbody><tr><td>'
    '<p style="color: #555555;">'
    '<span style="color: #ffffff;">--------</span>'
    '\u2514 \U0001f4dd (1h) '
    '<span style="color: #555555;">備註文字</span>'
    '<br /><span style="color: #ffffff;">--------</span>'
    '<span><ac:image ac:width="640"><ri:attachment ri:filename="wl_PN-17_shot.png" /></ac:image>'
    '<br /><a href="https://x/browse/PN-17" style="color: #2980b9;">\U0001f50d \u5728 Jira \u958b\u555f\u539f\u5716</a></span>'
    '</p>'
    '<p>另一段：'
    '<a href="https://x/browse/PBA-255"><img src="https://example.atlassian.net/wiki/download/attachments/208109570/wl_PBA-255_old.png" width="640" alt="old.png" /></a>'
    '<br /><a href="https://x/browse/PBA-255">\U0001f50d \u5728 Jira \u958b\u555f\u539f\u5716</a>'
    '</p>'
    '</td></tr></tbody></table>'
)

soup = BeautifulSoup(html, "html.parser")
m.promote_images_to_block_media(soup, "208109570")
out = str(soup)

print("=== 輸出 storage ===")
print(out)
print()

failures = []
if "🔍" in out:
    failures.append("殘留 🔍 可見連結")
if "<img" in out:
    failures.append("殘留 HTML <img>")

for ac_img in soup.find_all("ac:image"):
    fn = (ac_img.find("ri:attachment") or {}).get("ri:filename")
    parent = ac_img.parent
    pname = getattr(parent, "name", "")
    siblings = [c for c in parent.children if c is not ac_img
                and (getattr(c, "name", None) or str(c).strip())]
    print(f"{fn}: parent=<{pname}> 同層其他節點={len(siblings)} "
          f"祖先鏈={[a.name for a in ac_img.parents][:4]}")
    if pname != "p":
        failures.append(f"{fn} 的父層不是 <p>（{pname}）")
    if siblings:
        failures.append(f"{fn} 與其他內容同段落（inline），會被轉成 inline-media-image")
    if ac_img.find_parent("a"):
        failures.append(f"{fn} 仍被 <a> 包住，media 會帶 link mark")
    for need in ("ac:original-width", "ac:original-height", "ac:custom-width", "ac:width"):
        if not ac_img.get(need):
            failures.append(f"{fn} 缺少 {need}")
    ri = ac_img.find("ri:attachment")
    if not ri.get("ri:version-at-save"):
        failures.append(f"{fn} 缺少 ri:version-at-save")

print()
if failures:
    print("❌ 驗證失敗：")
    for f in failures:
        print("   - " + f)
    sys.exit(1)
print("✅ 全部圖片都是 block 層、獨立 <p>、無 <a> 包裹、屬性完整")
