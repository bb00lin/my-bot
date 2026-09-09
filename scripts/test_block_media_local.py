"""本地離線驗證 promote_images_to_block_media 的輸出。

檢查三件事：
1. 圖片是 block 層獨立 <p>（Cloud 才會轉成可點擊放大的 mediaSingle），且靠左對齊。
2. 備註裡「文字 / [[IMG:]] 交錯」的原始順序有被保留。
3. 當日留言連結（💬 留言）只輸出連結、且帶 focusedCommentId。

不連線 Confluence；用假環境變數載入模組，並 monkeypatch 需要網路的函式。
"""
import os
import re
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

failures = []


def check(cond, message):
    if not cond:
        failures.append(message)


def hr(title):
    print("\n" + "=" * 70)
    print(f"== {title}")
    print("=" * 70)


def block_summary(container):
    """把容器的直接子 block 攤成 ('img', 檔名) / ('text', 可見文字) 序列。"""
    out = []
    for child in container.find_all(recursive=False):
        ac_img = child.find("ac:image")
        if ac_img is not None:
            ri = ac_img.find("ri:attachment")
            out.append(("img", ri.get("ri:filename") if ri else "?"))
            continue
        text = (child.get_text() or "").replace("\u00a0", " ")
        text = re.sub(r"-{2,}", "", text).strip()
        out.append(("text", text))
    return out


def audit_images(soup, label):
    for ac_img in soup.find_all("ac:image"):
        ri = ac_img.find("ri:attachment")
        fn = ri.get("ri:filename") if ri else "?"
        parent = ac_img.parent
        pname = getattr(parent, "name", "")
        siblings = [c for c in parent.children if c is not ac_img
                    and (getattr(c, "name", None) or str(c).strip())]
        print(f"  {fn}: parent=<{pname}> 同層其他節點={len(siblings)} "
              f"align={ac_img.get('ac:align')} layout={ac_img.get('ac:layout')}")
        check(pname == "p", f"[{label}] {fn} 的父層不是 <p>（{pname}）")
        check(not siblings,
              f"[{label}] {fn} 與其他內容同段落（inline），會被轉成 inline-media-image")
        check(ac_img.find_parent("a") is None,
              f"[{label}] {fn} 仍被 <a> 包住，media 會帶 link mark")
        check(ac_img.get("ac:align") == "left",
              f"[{label}] {fn} 的 ac:align 不是 left（{ac_img.get('ac:align')}）")
        check(ac_img.get("ac:layout") == "align-start",
              f"[{label}] {fn} 的 ac:layout 不是 align-start（{ac_img.get('ac:layout')}）")
        for need in ("ac:original-width", "ac:original-height", "ac:custom-width", "ac:width"):
            check(bool(ac_img.get(need)), f"[{label}] {fn} 缺少 {need}")
        check(bool(ri.get("ri:version-at-save")), f"[{label}] {fn} 缺少 ri:version-at-save")


# ============================================================
hr("案例 1：既有頁面殘留（舊 <img> + 🔍 連結 + 表格）")
# ============================================================
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
print(out)
check("🔍" not in out, "[案例1] 殘留 🔍 可見連結")
check("<img" not in out, "[案例1] 殘留 HTML <img>")
audit_images(soup, "案例1")

td = soup.find("td")
seq = block_summary(td)
print(f"\n  區塊順序 = {seq}")
check(seq[0] == ("text", "└ 📝 (1h) 備註文字"),
      f"[案例1] 第一個區塊應為備註文字，實得 {seq[0]}")
check(seq[1] == ("img", "wl_PN-17_shot.png"),
      f"[案例1] 圖片應緊接文字之後，實得 {seq[1]}")
check(("text", "另一段：") in seq, "[案例1] 『另一段：』文字遺失")
check(seq.index(("img", "wl_PBA-255_old.png")) > seq.index(("text", "另一段：")),
      "[案例1] 舊 <img> 的位置沒有跟在其原本文字之後")


# ============================================================
hr("案例 2（核心）：備註文字與 [[IMG:]] 交錯，順序必須保留")
# ============================================================
COMMENT = (
    "Slice I/O design\n"
    "Spring Fingers\n"
    "[[IMG:clip_a.png]]\n"
    "moxa iothinx 4510\n"
    "[[IMG:clip_b.png]]\n"
    "行內混排 before [[IMG:clip_c.png]] after\n"
    "收尾文字"
)
for fn in ("wl_PBY-11_clip_a.png", "wl_PBY-11_clip_b.png", "wl_PBY-11_clip_c.png"):
    m._CONF_IMAGE_META[fn] = {"width": 1200, "height": 700, "version": 1, "alt": fn}

soup2 = BeautifulSoup("", "html.parser")
# 模擬 generate_style_3_html 的 panel → div → p 結構
panel_body = soup2.new_tag("ac:rich-text-body")
soup2.append(panel_body)
div_row = soup2.new_tag("div", style="margin-bottom: 12px;")
panel_body.append(div_row)
p_comment = soup2.new_tag("p", style="margin-top: 0px; margin-bottom: 0px; color: #555555;")
spacer = soup2.new_tag("span", style="color: #F5E6FF; user-select: none;")
spacer.string = "--------"
p_comment.append(spacer)
p_comment.append(soup2.new_string("└ 📝 "))
m.append_wysiwyg_comment(
    soup2, p_comment, COMMENT,
    color_style="color: #e74c3c; font-weight: bold;",
    issue_key="PBY-11", bg_color="#F5E6FF",
    hang_prefix="└ 📝 ", left_gutter="--------",
)
m.append_day_comment_link(
    soup2, p_comment, "PBY-11",
    [{"id": "10501", "author": "Judith Chen", "created_dt": None},
     {"id": "10502", "author": "Bob Lin", "created_dt": None}],
    color_style="color: #e74c3c; font-weight: bold;", bg_color="#F5E6FF",
)
div_row.append(p_comment)

m.promote_images_to_block_media(soup2, "214925313")
print(str(soup2).replace("</p>", "</p>\n"))

audit_images(soup2, "案例2")

seq2 = block_summary(div_row)
print("\n  區塊順序：")
for kind, val in seq2:
    print(f"    {kind:5s} | {val}")

expected = [
    ("text", "└ 📝 Slice I/O designSpring Fingers"),
    ("img", "wl_PBY-11_clip_a.png"),
    ("text", "moxa iothinx 4510"),
    ("img", "wl_PBY-11_clip_b.png"),
    ("text", "行內混排 before"),
    ("img", "wl_PBY-11_clip_c.png"),
    ("text", "after收尾文字💬 留言 (2) · 最新 Bob Lin"),
]
check(seq2 == expected, f"[案例2] 區塊順序不符\n     實得 {seq2}\n     預期 {expected}")
check(str(soup2).count("<p") == 7, f"[案例2] 段落數應為 7，實得 {str(soup2).count('<p')}")
check("<p></p>" not in str(soup2), "[案例2] 出現空的 <p>")

a_cmt = soup2.find("a", href=re.compile("focusedCommentId"))
check(a_cmt is not None, "[案例2] 找不到留言連結")
if a_cmt is not None:
    print(f"\n  留言連結 = {a_cmt.get('href')} / {a_cmt.get_text()} "
          f"/ target={a_cmt.get('target')} rel={a_cmt.get('rel')}")
    check(a_cmt.get("href").endswith("/browse/PBY-11?focusedCommentId=10502"),
          f"[案例2] 留言連結應指向最新留言，實得 {a_cmt.get('href')}")
    check(a_cmt.get("target") == "_blank", "[案例2] 留言連結缺少 target=_blank")
    check("noopener" in " ".join(a_cmt.get("rel") or []), "[案例2] 留言連結缺少 rel=noopener")


# ============================================================
hr("案例 3：只有一張圖的備註 / 單一留言標籤 / visibility 過濾")
# ============================================================
m._CONF_IMAGE_META["wl_PBY-11_solo.png"] = {"width": 900, "height": 500, "version": 2, "alt": "solo"}
soup3 = BeautifulSoup("", "html.parser")
p3 = soup3.new_tag("p")
soup3.append(p3)
sp3 = soup3.new_tag("span", style="color: #ffffff; user-select: none;")
sp3.string = "--------"
p3.append(sp3)
p3.append(soup3.new_string("└ 📝 "))
m.append_wysiwyg_comment(soup3, p3, "[[IMG:solo.png]]", issue_key="PBY-11")
m.append_day_comment_link(soup3, p3, "PBY-11",
                          [{"id": "9001", "author": "Judith Chen", "created_dt": None}])
m.promote_images_to_block_media(soup3, "214925313")
print(str(soup3))
audit_images(soup3, "案例3")
check("💬 留言 · Judith Chen" in str(soup3),
      "[案例3] 單一留言應顯示作者名稱")
# └ 📝 前綴段 → 圖片段 → 留言連結段
check(str(soup3).count("<p") == 3, f"[案例3] 段落數應為 3，實得 {str(soup3).count('<p')}")

issue_stub = {"fields": {"comment": {"comments": [
    {"id": "1", "created": "2026-09-09T10:00:00.000+0800", "author": {"displayName": "A"}},
    {"id": "2", "created": "2026-09-09T18:30:00.000+0800", "author": {"displayName": "B"}},
    {"id": "3", "created": "2026-09-09T19:00:00.000+0800", "author": {"displayName": "C"},
     "visibility": {"type": "role", "value": "Administrators"}},
    {"id": "4", "created": "2026-09-08T23:59:00.000+0800", "author": {"displayName": "D"}},
    # updated 是今天但 created 是舊的 → 不可被算進 9/9
    {"id": "5", "created": "2026-08-01T09:00:00.000+0800",
     "updated": "2026-09-09T09:00:00.000+0800", "author": {"displayName": "E"}},
]}}}
picked = m.list_day_issue_comments(issue_stub, "2026-09-09")
print(f"\n  2026-09-09 留言 = {[(c['id'], c['author']) for c in picked]}")
check([c["id"] for c in picked] == ["1", "2"],
      f"[案例3] 留言篩選錯誤（應為 ['1','2']，含 visibility 與 updated 過濾），實得 {[c['id'] for c in picked]}")


# ============================================================
hr("案例 4：PBY-11 2026-09-09 真實備註（12 個標記 + 未標記當日附件）")
# ============================================================
# 這筆備註同時含 Jira 連結語法 [url|url]（pipe 是 newline_chars 分隔符）與連續標記，
# 是實測會讓人誤以為「圖片被搬到最後」的原文；未標記的當日附件才會被接在備註後面。
PBY11_COMMENT = """Slice I/O design

Spring Fingers
[[IMG:wl_20260909_130200_324_6ba52f_clipboard.png]]

moxa iothinx 4510
[[IMG:wl_20260909_130200_880_d0f429_clipboard.png]]

[[IMG:wl_20260909_130200_409_8ed4c1_clipboard.png]]

[[IMG:wl_20260909_130200_313_15c294_clipboard.png]]

WAGO_IO SYS
[[IMG:wl_20260909_130200_379_be58e7_clipboard.png]]

[[IMG:wl_20260909_130200_859_c83766_clipboard.png]]

[[IMG:wl_20260909_130200_325_c5a639_clipboard.png]]

[[IMG:wl_20260909_130200_968_d896d0_clipboard.png]]

实点科技
[https://www.solidotech.com/cn/products/xb6s-pt04a|https://www.solidotech.com/cn/products/xb6s-pt04a]
[[IMG:wl_20260909_130200_231_b0ab82_clipboard.png]]

[[IMG:wl_20260909_130200_215_5adab6_clipboard.png]]

ME/ME-MAX Electronic Housings - Phoenix Contact
[https://www.youtube.com/watch?v=_NYeegIIa_8|https://www.youtube.com/watch?v=_NYeegIIa_8]

[[IMG:wl_20260909_130200_816_f600a6_clipboard.png]]

[https://www.triomotion.com/public/products/motionplc/motionPLCExpansion.php|https://www.triomotion.com/public/products/motionplc/motionPLCExpansion.php]

[[IMG:wl_20260909_130200_036_8cece4_clipboard.png]]

內部匯流排連接器 (側向滑入對接)

型號狀態： 全新客製化專利零件，無法在市面上取得標準品型號。

結構設計： 為了達成模組化的正面滑入擴充機制，設計團隊全新開發了一款專屬的 BUS 電氣連接器。

客製化原因： 開發團隊特別指出，自行開模是為了避開極度擁擠的專利壁壘，沒有通用的標準連接器可以買。"""

PBY11_MARKERS = [x.strip() for x in m.IMG_MARKER_RE.findall(PBY11_COMMENT)]
check(len(PBY11_MARKERS) == 12, f"[案例4] 標記數應為 12，實得 {len(PBY11_MARKERS)}")

# 同一天上傳、但備註沒有 [[IMG:]] 的附件（day-attachment 掃描補上，只能接在備註之後）
PBY11_ORPHANS = [
    "wl_20260909_130207_640_c03141_clipboard.png",
    "wl_20260909_130209_351_e227fa_clipboard.png",
]
for fn in PBY11_MARKERS + PBY11_ORPHANS:
    m._CONF_IMAGE_META[m.conf_unique_img_name("PBY-11", fn)] = {
        "width": 1200, "height": 700, "version": 1, "alt": fn,
    }

soup4 = BeautifulSoup("", "html.parser")
row4 = soup4.new_tag("div", style="margin-bottom: 12px;")
soup4.append(row4)
p4 = soup4.new_tag("p", style="margin-top: 0px; margin-bottom: 10px; color: #555555;")
sp4 = soup4.new_tag("span", style="color: #ffffff; user-select: none;")
sp4.string = "--------"
p4.append(sp4)
p4.append(soup4.new_string("└ 📝 (3h56m) "))
m.append_wysiwyg_comment(
    soup4, p4, PBY11_COMMENT,
    color_style="color: #555555;", issue_key="PBY-11", bg_color="#ffffff",
    hang_prefix="└ 📝 (3h56m) ", left_gutter="--------",
)
# 未標記當日附件確實會被 exclude_names 之外的部分掃進來並附加在最後
excl = [x.strip() for x in m.IMG_MARKER_RE.findall(PBY11_COMMENT)]
day_atts = [
    {"filename": fn, "id": str(i), "created": "2026-09-09T16:34:08.000+0800"}
    for i, fn in enumerate(PBY11_MARKERS + PBY11_ORPHANS, 1)
]
swept = m.list_day_image_filenames(day_atts, "2026-09-09", exclude_names=excl, issue_key="PBY-11")
check(swept == PBY11_ORPHANS,
      f"[案例4] day-attachment 掃描應只剩未標記的 {PBY11_ORPHANS}，實得 {swept}")
m.append_day_attachment_images(
    soup4, p4, "PBY-11", swept,
    color_style="color: #555555;", bg_color="#ffffff",
    hang_prefix="└ 📝 (3h56m) ", left_gutter="--------",
)
row4.append(p4)

m.promote_images_to_block_media(soup4, "214925313")
audit_images(soup4, "案例4")

seq4 = block_summary(row4)
print("\n  區塊順序：")
for kind, val in seq4:
    print(f"    {kind:5s} | {val[:95]}")

imgs4 = [v for k, v in seq4 if k == "img"]
expected4 = [m.conf_unique_img_name("PBY-11", fn) for fn in PBY11_MARKERS + PBY11_ORPHANS]
check(imgs4 == expected4,
      f"[案例4] 圖片順序不符\n     實得 {imgs4}\n     預期 {expected4}")
check(len(imgs4) == len(set(imgs4)), f"[案例4] 有重複圖片：{imgs4}")

# 中文段落是備註最後一段文字；其後只允許「未標記」的當日附件
para_idx = max(i for i, (k, v) in enumerate(seq4)
               if k == "text" and "沒有通用的標準連接器可以買" in v)
after = [v for k, v in seq4[para_idx + 1:] if k == "img"]
check(after == [m.conf_unique_img_name("PBY-11", fn) for fn in PBY11_ORPHANS],
      f"[案例4] 中文段落之後應只有未標記附件，實得 {after}")
marked_positions = [seq4.index(("img", m.conf_unique_img_name("PBY-11", fn)))
                    for fn in PBY11_MARKERS]
check(all(i < para_idx for i in marked_positions),
      "[案例4] 有 [[IMG:]] 標記的圖片被排到中文段落之後")
check(marked_positions == sorted(marked_positions),
      "[案例4] 標記圖片的相對順序與原文不一致")
# 使用者指認的那張（第 2 個標記）必須就在 'moxa iothinx 4510' 之後
d0f429 = m.conf_unique_img_name("PBY-11", "wl_20260909_130200_880_d0f429_clipboard.png")
moxa_idx = next(i for i, (k, v) in enumerate(seq4) if k == "text" and "moxa iothinx 4510" in v)
check(seq4[moxa_idx + 1] == ("img", d0f429),
      f"[案例4] 第 2 個標記未緊接 'moxa iothinx 4510'，實得 {seq4[moxa_idx + 1]}")


hr("結果")
if failures:
    print("❌ 驗證失敗：")
    for f in failures:
        print("   - " + f)
    sys.exit(1)
print("✅ 圖片皆為 block 層獨立 <p>、靠左、無 <a> 包裹；文字/圖片交錯順序保留；留言連結正確")
