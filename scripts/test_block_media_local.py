"""本地離線驗證 promote_images_to_block_media 的輸出。

檢查三件事：
1. 圖片是 block 層獨立 <p>（Cloud 才會轉成可點擊放大的 mediaSingle），且靠左對齊。
2. 備註裡「文字 / [[IMG:]] 交錯」的原始順序有被保留。
3. 當日留言連結（💬 留言）只輸出連結、且帶 focusedCommentId。
4. 只有圖片（或沒有備註但有當日圖片）的日列，└ 📝 後顯示「worklog內容如圖」再接圖片。
5. 成員名稱行為 <h1><strong><span>，正規化可重複執行不巢狀。

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


# ============================================================
hr("案例 5：只有圖片的備註 → 「worklog內容如圖」在圖片前（style 3 真實渲染）")
# ============================================================
from datetime import datetime  # noqa: E402

CAP = m.IMAGE_ONLY_COMMENT_TEXT
for fn in ("a.png", "b.png", "c.png", "d.png", "e.png", "f.png"):
    m._CONF_IMAGE_META[m.conf_unique_img_name("PBA-225", fn)] = {
        "width": 1316, "height": 730, "version": 6, "alt": fn,
    }


def day(comment="", mins=60, transition="", day_images=None, day_comments=None, d=30):
    return {
        "date": datetime(2026, 9, d), "day_name": "Wed", "day_short": f"9/{d}",
        "dur_str": m.format_duration(mins) if mins else "", "total_mins_day": mins,
        "comment": comment, "transition": transition, "has_log": True,
        "day_images": day_images or [], "day_comments": day_comments or [],
    }


def render_style3(days):
    s = BeautifulSoup("", "html.parser")
    log = {
        "key": "PBA-225", "summary": "Demo", "status": "IN PROGRESS", "project": "PBA",
        "parent": "NA", "label": "NA", "duedate": '"Due TBD"', "daily_days": days,
    }
    container = m.generate_style_3_html(
        s, datetime(2026, 10, 2), [datetime(2026, 9, 30)], [log], bg_color="#F5E6FF",
    )
    s.append(container)
    m.promote_images_to_block_media(s, "225247361")
    return s


def row_sequences(s):
    body = s.find("ac:rich-text-body")
    return [block_summary(div) for div in body.find_all("div", recursive=False)]


def comment_seq(rows, idx=0):
    """去掉日期列（第一個 block），只看 └ 📝 之後的區塊。"""
    return rows[idx][1:]


# 5a：單一標記
s5 = render_style3([day("[[IMG:a.png]]")])
audit_images(s5, "案例5a")
seq5 = comment_seq(row_sequences(s5))
print(f"  5a = {seq5}")
check(seq5 == [("text", f"└ 📝 {CAP}"), ("img", "wl_PBA-225_a.png")],
      f"[案例5a] 應為 文字「{CAP}」→ 圖片，實得 {seq5}")

# 5b：多筆 worklog 合併（' / \n' 分隔）且都只有圖片
s5b = render_style3([day("[[IMG:a.png]] / \n[[IMG:b.png]]")])
audit_images(s5b, "案例5b")
seq5b = comment_seq(row_sequences(s5b))
print(f"  5b = {seq5b}")
check(seq5b == [("text", f"└ 📝 {CAP}"), ("img", "wl_PBA-225_a.png"), ("img", "wl_PBA-225_b.png")],
      f"[案例5b] 多筆合併的純圖片備註不符，實得 {seq5b}")
check("/" not in "".join(v for k, v in seq5b if k == "text").replace("└ 📝", ""),
      "[案例5b] 殘留 ' / ' 分隔符")

# 5c：有文字就不加
s5c = render_style3([day("真的有文字\n[[IMG:a.png]]")])
seq5c = comment_seq(row_sequences(s5c))
print(f"  5c = {seq5c}")
check(CAP not in str(s5c), "[案例5c] 有文字的備註不應出現「worklog內容如圖」")
check(seq5c[0] == ("text", "└ 📝 真的有文字"), f"[案例5c] 文字順序錯誤 {seq5c}")


# ============================================================
hr("案例 6：沒有備註 + 當日附件圖片 → 「worklog內容如圖」；其他 fallback 不變")
# ============================================================
s6 = render_style3([day("", mins=60, day_images=["c.png"])])
audit_images(s6, "案例6a")
seq6 = comment_seq(row_sequences(s6))
print(f"  6a (有工時 + 當日圖片) = {seq6}")
check(seq6 == [("text", f"└ 📝 {CAP}"), ("img", "wl_PBA-225_c.png")],
      f"[案例6a] 應為 「{CAP}」→ 圖片（取代 (無填寫工作日誌)），實得 {seq6}")
check("(無填寫工作日誌)" not in str(s6), "[案例6a] 仍出現 (無填寫工作日誌)")

s6b = render_style3([day("", mins=0, day_images=["d.png"])])
seq6b = comment_seq(row_sequences(s6b))
print(f"  6b (零工時 + 當日圖片) = {seq6b}")
check(seq6b == [("text", f"└ 📝 {CAP}"), ("img", "wl_PBA-225_d.png")],
      f"[案例6b] 應取代 (附件圖片)，實得 {seq6b}")
check("(附件圖片)" not in str(s6b), "[案例6b] 仍出現 (附件圖片)")

# 6c：留言連結 + 非圖片附件 + 圖片，順序：文字 → 💬 → 圖片 → 📎
file_meta = {"filename": "spec.pdf", "id": "777", "content": "x"}
s6c = render_style3([day(
    "", mins=30, day_images=["e.png", file_meta],
    day_comments=[{"id": "42", "author": "Vic Wu", "created_dt": None}],
)])
audit_images(s6c, "案例6c")
seq6c = comment_seq(row_sequences(s6c))
print(f"  6c = {seq6c}")
check(seq6c[0] == ("text", f"└ 📝 {CAP}💬 留言 · Vic Wu"), f"[案例6c] 首段應為文字+留言連結，實得 {seq6c}")
check(seq6c[1] == ("img", "wl_PBA-225_e.png"), f"[案例6c] 留言之後應為圖片，實得 {seq6c}")
check(seq6c[-1] == ("text", "📎 spec.pdf"), f"[案例6c] 最後應為 📎 附件連結，實得 {seq6c}")
check(s6c.find("a", href=re.compile(r"focusedCommentId=42")) is not None, "[案例6c] 💬 連結遺失")
check(s6c.find("a", href=re.compile(r"attachmentId=777")) is not None, "[案例6c] 📎 連結遺失")

# 6d：無圖片時 fallback 維持原樣
for label, kwargs, expect in [
    ("6d 有工時無圖", dict(mins=60), "(無填寫工作日誌)"),
    ("6e 僅狀態改變", dict(mins=0, transition="🔄[進行中] ➜ ✅[完成]"), "(僅狀態改變)"),
    ("6f 只有非圖片附件", dict(mins=0, day_images=[file_meta]), "(附件)"),
    ("6g 什麼都沒有", dict(mins=0), "(無紀錄)"),
]:
    sx = render_style3([day("", **kwargs)])
    txt = sx.get_text()
    print(f"  {label}: {'OK' if expect in txt else 'MISSING'} / 含如圖={CAP in txt}")
    check(expect in txt and CAP not in txt, f"[{label}] 應顯示 {expect} 且不含「{CAP}」")

# 6h：整份輸出的圖片統計（block <ac:image> 數 == 獨立 <p> 數、無 inline）
all_imgs = sum(len(x.find_all("ac:image")) for x in (s5, s5b, s6, s6b, s6c))
print(f"  ac:image 總數 = {all_imgs}")
check(all_imgs == 6, f"[案例6h] ac:image 總數應為 6，實得 {all_imgs}")


# ============================================================
hr("案例 7：style 2 也套用同樣規則")
# ============================================================
def render_style2(comment, day_images):
    s = BeautifulSoup("", "html.parser")
    log = {
        "key": "PBA-225", "summary": "Demo", "status": "IN PROGRESS", "transition": "",
        "project": "PBA", "parent": "NA", "label": "NA", "comment": comment,
        "duration": "1h", "duedate": '"Due TBD"', "started_date": "2026-09-30",
        "day_images": day_images,
    }
    s.append(m.generate_style_2_html(s, datetime(2026, 9, 30), [log], bg_color="#F5E6FF"))
    m.promote_images_to_block_media(s, "225247361")
    return s


s7 = render_style2("[[IMG:f.png]]", [])
audit_images(s7, "案例7a")
body7 = s7.find("ac:rich-text-body")
seq7 = block_summary(body7)
print(f"  7a = {seq7}")
idx7 = next(i for i, (k, v) in enumerate(seq7) if k == "text" and v.startswith("└ 📝"))
check(seq7[idx7] == ("text", f"└ 📝 (1h) {CAP}") and seq7[idx7 + 1] == ("img", "wl_PBA-225_f.png"),
      f"[案例7a] style2 純圖片備註不符，實得 {seq7}")
s7b = render_style2("NA", ["c.png"])
seq7b = block_summary(s7b.find("ac:rich-text-body"))
print(f"  7b = {seq7b}")
check(("text", f"└ 📝 (1h) {CAP}") in seq7b, f"[案例7b] style2 NA + 當日圖片應顯示「{CAP}」，實得 {seq7b}")


# ============================================================
hr("案例 8：成員名稱行 → <h1><strong><span>，可重複執行不巢狀")
# ============================================================
legacy = (
    '<p local-id="aa">#Worklog</p>'
    '<p style="margin-top: 20.0px;"><span style="background-color: rgb(255,248,230);font-weight: bold;'
    'font-size: 120.0%;">@sam.chang</span></p>'
    '<div class="daily-worklog-20261002"><p>body</p></div>'
    '<h3><span style="background-color: rgb(245,230,255);">@Bob Lin</span></h3>'
    '<p>@Bob Lin 不是名稱行</p>'
    '<p><ac:link><ri:user ri:account-id="x" /></ac:link></p>'
    '<p local-id="bb">#Worklog End<br /></p>'
    '<p><span>@Vic Wu</span></p>'
)
s8 = BeautifulSoup(legacy, "html.parser")
start8 = s8.find(string=re.compile(r"#Worklog\s*$")).parent
end8 = s8.find(string=re.compile(r"#Worklog End\s*$")).parent
n1 = m.normalize_member_names_in_region(s8, start8, end8)
first = str(s8)
n2 = m.normalize_member_names_in_region(s8, start8, end8)
print(first)
check(n1 == 2, f"[案例8] 第一次應改寫 2 行，實得 {n1}")
check(n2 == 0 and str(s8) == first, "[案例8] 第二次執行不應再改動（非冪等）")
h1s = s8.find_all("h1")
check([h.get_text() for h in h1s] == ["@sam.chang", "@Bob Lin"], f"[案例8] h1 內容不符 {[h.get_text() for h in h1s]}")
for h in h1s:
    check(len(h.find_all("strong")) == 1 and not h.find("h1"), f"[案例8] {h.get_text()} 有巢狀 strong/heading")
    check("font-size" not in str(h) and "font-weight" not in str(h), f"[案例8] {h.get_text()} 殘留 ADF 不支援的樣式")
check("rgb(255,248,230)" in str(h1s[0]), "[案例8] 未沿用原本的背景色")
check(s8.find("h3") is None, "[案例8] 非 h1 的名稱行沒有被標準化")
check("<p>@Bob Lin 不是名稱行</p>" in first, "[案例8] 誤改了一般段落")
check("<p><span>@Vic Wu</span></p>" in first, "[案例8] 改到了 #Worklog End 之後的內容")
check(s8.find("ri:user") is not None and s8.find("ri:user").find_parent("p") is not None,
      "[案例8] 真正的 mention 不應被改寫")

built = m.build_member_name_heading(s8, "Vic Wu", m.USER_BG_COLORS["Vic Wu"])
check(m.is_member_name_heading(built), "[案例8] build_member_name_heading 不是標準格式")
check(m.normalize_member_name_block(s8, built) is built, "[案例8] 標準格式被重複包裝")

# run_clear_logic 用 '@name' 文字找錨點：新格式仍可被找到，且它的下一個兄弟就是日誌區塊
anchor = s8.find(string=re.compile("@sam.chang", re.I))
container = anchor.find_parent(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div"])
check(container is not None and container.name == "h1", "[案例8] '@name' 錨點找不到 h1 容器")
check(m.member_name_of_block(container) == "sam.chang", "[案例8] 錨點容器無法辨識為成員名稱行")

# confluence_api2 的同名正規化（新週報建立時）結果需一致
os.environ.setdefault("CONF_URL", "https://example.atlassian.net")
import confluence_api2 as c2  # noqa: E402
s8b = BeautifulSoup(legacy, "html.parser")
c1 = c2.normalize_member_name_lines(s8b)
c2_second = c2.normalize_member_name_lines(s8b)
print(f"  confluence_api2: 第一次 {c1} 行、第二次 {c2_second} 行")
check(c1 == 2 and c2_second == 0, f"[案例8] confluence_api2 正規化次數不符 {c1}/{c2_second}")
check(str(s8b) == first, "[案例8] confluence_api2 與每日腳本的名稱行格式不一致")


hr("結果")
if failures:
    print("❌ 驗證失敗：")
    for f in failures:
        print("   - " + f)
    sys.exit(1)
print("✅ 圖片皆為 block 層獨立 <p>、靠左、無 <a> 包裹；文字/圖片交錯順序保留；留言連結正確；"
      "純圖片備註顯示「worklog內容如圖」；成員名稱行為冪等的 <h1><strong>")
