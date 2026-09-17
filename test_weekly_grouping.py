#!/usr/bin/env python3

"""Unit + integration verification for the WeeklyReport half-year grouping (no credentials required).

以假的 Confluence / Jira 傳輸層驅動兩支腳本的真實程式碼：
  1. confluence_api2.py  -> 驗證半年分類、漏網之魚回收、冪等性
  2. daily_worklog_to_confluence.py -> 驗證分類後仍能找到並寫入正確頁面

執行方式: python3 test_weekly_grouping.py
"""

from __future__ import annotations

import json as jsonlib
import os
import re
import sys
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("CONF_URL", "https://example.atlassian.net/wiki")
os.environ.setdefault("CONF_USER", "verify@example.com")
os.environ.setdefault("CONF_PASS", "dummy-token")
os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("LINE_USER_ID", None)
os.environ.pop("LINE_ACCESS_TOKEN", None)

import requests

import confluence_api2 as api

SPACE_KEY = "team_AIoTHW"
BASE = "https://example.atlassian.net"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------------------------------------------------------------------------
# 假的 Confluence / Jira
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = jsonlib.dumps(self._payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} for fake request")


class FakeConfluence:
    """只實作這兩支腳本真正會用到的 endpoint。"""

    def __init__(self):
        self.pages = {}
        self.next_id = 1000
        self.clock = 0
        self.requests = []          # (method, path, params, payload)
        self.move_supported = True

    # --- 建立測試資料 ---

    def add_page(self, title, parent_id=None, body="<p></p>", page_id=None):
        page_id = str(page_id if page_id is not None else self.next_id)
        self.next_id = max(self.next_id + 1, int(page_id) + 1)
        self.clock += 1
        self.pages[page_id] = {
            "id": page_id,
            "title": title,
            "parent_id": str(parent_id) if parent_id else None,
            "body": body,
            "version": 1,
            "created": self.clock,
        }
        return page_id

    def children_of(self, parent_id):
        return [p for p in self.pages.values() if p["parent_id"] == str(parent_id)]

    def by_title(self, title):
        for page in self.pages.values():
            if page["title"] == title:
                return page
        return None

    def parent_title_of(self, title):
        page = self.by_title(title)
        if not page or not page["parent_id"]:
            return None
        parent = self.pages.get(page["parent_id"])
        return parent["title"] if parent else None

    # --- 回應組裝 ---

    def _ancestor_chain(self, page):
        chain = []
        cursor = page["parent_id"]
        guard = 0
        while cursor and guard < 20:
            parent = self.pages.get(cursor)
            if not parent:
                break
            chain.append(parent)
            cursor = parent["parent_id"]
            guard += 1
        return list(reversed(chain))

    def _serialize(self, page, expand=""):
        fields = set((expand or "").split(","))
        out = {
            "id": page["id"],
            "type": "page",
            "title": page["title"],
            "_links": {"webui": f"/spaces/{SPACE_KEY}/pages/{page['id']}/{page['title']}"},
        }
        if "space" in fields:
            out["space"] = {"key": SPACE_KEY}
        if "version" in fields:
            out["version"] = {"number": page["version"]}
        if "body.storage" in fields:
            out["body"] = {"storage": {"value": page["body"], "representation": "storage"}}
        if "ancestors" in fields:
            out["ancestors"] = [
                {"id": a["id"], "type": "page", "title": a["title"]}
                for a in self._ancestor_chain(page)
            ]
        return out

    def _paged(self, path, matched, params, expand):
        limit = int((params or {}).get("limit", 25))
        start = int((params or {}).get("start", 0))
        window = matched[start:start + limit]
        body = {
            "results": [self._serialize(p, expand) for p in window],
            "start": start,
            "limit": limit,
            "size": len(window),
            "_links": {"base": f"{BASE}/wiki"},
        }
        if start + limit < len(matched):
            query = f"?start={start + limit}&limit={limit}"
            if (params or {}).get("cql"):
                query += f"&cql={params['cql']}"
            if expand:
                query += f"&expand={expand}"
            body["_links"]["next"] = f"/rest/api/content{path}{query}"
        return FakeResponse(200, body)

    # --- 路由 ---

    def handle(self, method, url, params=None, payload=None):
        parsed = urlparse(url)
        path = parsed.path
        if params is None and parsed.query:
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        params = params or {}
        self.requests.append((method, path, dict(params), payload))

        if "/rest/api/3/" in path:
            return self._handle_jira(method, path)

        conf = path.split("/wiki/rest/api/content", 1)
        if len(conf) != 2:
            return FakeResponse(404, {"message": f"unhandled path {path}"})
        tail = conf[1]
        expand = params.get("expand", "")

        if method == "GET" and tail == "/search":
            return self._paged("/search", self._run_cql(params.get("cql", "")), params, expand)

        if method == "GET" and tail == "":
            # GET /content?title=...  (daily 腳本與精確標題查詢都走這裡)
            title = params.get("title")
            matched = [p for p in self.pages.values() if p["title"] == title] if title else []
            return self._paged("", matched, params, expand)

        move = re.match(r"^/(\d+)/move/append/(\d+)$", tail)
        if method == "PUT" and move:
            if not self.move_supported:
                return FakeResponse(404, {"message": "move endpoint unavailable"})
            page_id, target_id = move.group(1), move.group(2)
            if page_id not in self.pages or target_id not in self.pages:
                return FakeResponse(404, {"message": "page not found"})
            self.pages[page_id]["parent_id"] = target_id
            return FakeResponse(200, {"pageId": page_id})

        child = re.match(r"^/(\d+)/child/page$", tail)
        if method == "GET" and child:
            kids = sorted(self.children_of(child.group(1)), key=lambda p: p["created"])
            return self._paged(f"/{child.group(1)}/child/page", kids, params, expand)

        prop = re.match(r"^/(\d+)/property(/editor)?$", tail)
        if prop:
            return FakeResponse(200, {"key": "editor", "value": "v2", "version": {"number": 1}})

        single = re.match(r"^/(\d+)$", tail)
        if single:
            page = self.pages.get(single.group(1))
            if not page:
                return FakeResponse(404, {"message": "page not found"})
            if method == "GET":
                return FakeResponse(200, self._serialize(page, expand))
            if method == "PUT":
                return self._update(page, payload or {})

        if method == "POST" and tail == "":
            return self._create(payload or {})

        return FakeResponse(404, {"message": f"unhandled {method} {path}"})

    def _update(self, page, payload):
        if payload.get("title") and self.by_title(payload["title"]) not in (None, page):
            return FakeResponse(400, {"message": "title already exists"})
        page["title"] = payload.get("title", page["title"])
        body = (payload.get("body") or {}).get("storage", {}).get("value")
        if body is not None:
            page["body"] = body
        # 只有明確帶 ancestors 才會改變父節點；沒帶就保留原位置。
        ancestors = payload.get("ancestors")
        if ancestors:
            page["parent_id"] = str(ancestors[-1]["id"])
        page["version"] = (payload.get("version") or {}).get("number", page["version"] + 1)
        return FakeResponse(200, self._serialize(page, "version"))

    def _create(self, payload):
        title = payload.get("title", "")
        if self.by_title(title):
            return FakeResponse(400, {"message": "A page with this title already exists"})
        ancestors = payload.get("ancestors") or []
        parent_id = str(ancestors[-1]["id"]) if ancestors else None
        body = (payload.get("body") or {}).get("storage", {}).get("value", "")
        page_id = self.add_page(title, parent_id, body)
        return FakeResponse(200, self._serialize(self.pages[page_id], "version,space"))

    def _run_cql(self, cql):
        matched = list(self.pages.values())
        exact = re.search(r'title\s*=\s*"([^"]+)"', cql)
        if exact:
            matched = [p for p in matched if p["title"] == exact.group(1)]
        fuzzy = re.search(r'title\s*~\s*"([^"]+)"', cql)
        if fuzzy:
            prefix = fuzzy.group(1).rstrip("*")
            matched = [p for p in matched if p["title"].startswith(prefix)]
        if "ORDER BY created DESC" in cql:
            matched.sort(key=lambda p: p["created"], reverse=True)
        else:
            matched.sort(key=lambda p: p["title"])
        return matched

    def _handle_jira(self, method, path):
        if "/remotelink" in path:
            return FakeResponse(200, []) if method == "GET" else FakeResponse(204, {})
        if "/user/search" in path:
            return FakeResponse(200, [])
        if "/search/jql" in path:
            return FakeResponse(200, {"issues": [], "isLast": True})
        return FakeResponse(200, {})


FAKE = FakeConfluence()


def install_fake_transport():
    def route(method):
        def call(url, **kwargs):
            payload = kwargs.get("json")
            if payload is None and kwargs.get("data"):
                try:
                    payload = jsonlib.loads(kwargs["data"])
                except (TypeError, ValueError):
                    payload = None
            return FAKE.handle(method, url, kwargs.get("params"), payload)
        return call

    requests.get = route("GET")
    requests.put = route("PUT")
    requests.post = route("POST")
    requests.delete = route("DELETE")
    # 腳本為了等 Atlassian 背景連動會 sleep，測試裡沒有任何邏輯依賴真實等待
    api.time.sleep = lambda seconds: None


# ---------------------------------------------------------------------------
# Part A: 純函式
# ---------------------------------------------------------------------------

def test_pure_functions():
    section("Part A: 純函式（日期解析、半年邊界、總目錄判定、搬移計畫）")

    check("parse_report_date 解析正常週報",
          api.parse_report_date("WeeklyReport_20250822") == date(2025, 8, 22))
    check("parse_report_date 容忍前後空白",
          api.parse_report_date("  WeeklyReport_20250822  ") == date(2025, 8, 22))
    check("parse_report_date 排除總目錄頁 WeeklyReport",
          api.parse_report_date("WeeklyReport") is None)
    check("parse_report_date 排除分類頁 2025H1",
          api.parse_report_date("2025H1") is None)
    check("parse_report_date 排除帶前綴的分類頁",
          api.parse_report_date("WeeklyReport_2025H2") is None)
    check("parse_report_date 排除非法日期",
          api.parse_report_date("WeeklyReport_20251345") is None)
    check("parse_report_date 排除尾端有雜訊的標題",
          api.parse_report_date("WeeklyReport_20250822_backup") is None)

    for d, expected in [
        (date(2025, 1, 1), "2025H1"),
        (date(2025, 6, 30), "2025H1"),
        (date(2025, 7, 1), "2025H2"),
        (date(2025, 12, 31), "2025H2"),
        (date(2026, 9, 18), "2026H2"),
    ]:
        check(f"half_year_group_title({d}) == {expected}",
              api.half_year_group_title(d) == expected)

    check("分類頁標題不會被週報樣式誤判",
          all(api.GROUP_TITLE_PATTERN.match(t) for t in ("2025H1", "2026H2"))
          and not any(api.GROUP_TITLE_PATTERN.match(t)
                      for t in ("2025H3", "WeeklyReport_2025H1", "12025H1", "2025h1")))

    check("calculate_next_report_date 推進一週",
          api.calculate_next_report_date("WeeklyReport_20260918") == date(2026, 9, 25))
    check("calculate_next_report_date 跨年推進",
          api.calculate_next_report_date("WeeklyReport_20251226") == date(2026, 1, 2))
    check("跨半年邊界會落到下半年分類",
          api.half_year_group_title(api.calculate_next_report_date("WeeklyReport_20260626")) == "2026H2")

    root = {"id": "102", "title": "WeeklyReport"}
    check("resolve_root_page 取直接父頁面",
          api.resolve_root_page({"ancestors": [{"id": "100", "title": "Home"}, root]})["id"] == "102")
    check("resolve_root_page 會跳過半年分類頁",
          api.resolve_root_page({"ancestors": [
              {"id": "100", "title": "Home"}, root, {"id": "200", "title": "2026H2"}]})["id"] == "102")
    check("resolve_root_page 無祖先時回傳 None",
          api.resolve_root_page({"ancestors": []}) is None)
    check("resolve_root_page 只有分類頁祖先時回傳 None",
          api.resolve_root_page({"ancestors": [{"id": "200", "title": "2026H2"}]}) is None)

    reports = [
        {"id": "1", "title": "WeeklyReport_20250704", "ancestors": [{"id": "900"}]},
        {"id": "2", "title": "WeeklyReport_20250711", "ancestors": [{"id": "102"}]},
        {"id": "3", "title": "WeeklyReport_20260109", "ancestors": []},
        {"id": "4", "title": "WeeklyReport", "ancestors": [{"id": "100"}]},
    ]
    plan = api.build_organize_plan(reports, {"2025H2": "900"})
    titles = [p["title"] for p, _ in plan]
    check("已在正確分類的頁面不會被搬移", "WeeklyReport_20250704" not in titles)
    check("掛錯父頁面的漏網之魚會被納入計畫", "WeeklyReport_20250711" in titles)
    check("完全沒有父頁面的頁面也會被納入計畫（避開 None==None 陷阱）",
          "WeeklyReport_20260109" in titles)
    check("非週報標題不會被納入計畫", "WeeklyReport" not in titles)
    check("計畫依標題排序", titles == sorted(titles), f"實際: {titles}")
    check("計畫的目標分類正確",
          dict((p["title"], g) for p, g in plan).get("WeeklyReport_20260109") == "2026H1")


# ---------------------------------------------------------------------------
# Part B: confluence_api2 端到端
# ---------------------------------------------------------------------------

def build_tree():
    """重建使用者實際的頁面樹：WeeklyReport 底下一整排 2025-06-20 起的週報。"""
    FAKE.pages.clear()
    FAKE.requests.clear()
    FAKE.clock = 0
    FAKE.next_id = 1000
    home = FAKE.add_page("Space Home", None, page_id=100)
    FAKE.add_page("Project_Info", home, page_id=101)
    root = FAKE.add_page("WeeklyReport", home, page_id=102)

    cursor, last = date(2025, 6, 20), date(2026, 9, 18)
    titles = []
    while cursor <= last:
        title = f"WeeklyReport_{cursor.strftime('%Y%m%d')}"
        FAKE.add_page(title, root, body=weekly_body(cursor))
        titles.append(title)
        cursor += timedelta(days=7)
    return root, titles


def weekly_body(report_date):
    """模擬真實週報版面：上方是排版表格與嵌入區，下方才是各人的 mention 與日誌。

    順序很重要：run_clear_logic() 掃到 [YYYY/MM/DD] 備註後會一路清到下一個
    日期節點，所以表格必須放在 mention 區塊之前（與實際頁面一致）。
    """
    tag = report_date.strftime("%Y-%m-%d")
    return (
        '<table><tbody><tr><td>週報排版表格</td></tr></tbody></table>'
        '<h1><ac:link><ri:user ri:account-id="acc-bob" />Bob Lin</ac:link></h1>'
        f'<p>[{report_date.strftime("%Y/%m/%d")}] 既有備註 PBY-11</p>'
        f'<div class="daily-worklog-{tag}"><p>上週日誌 RFQ-93</p></div>'
    )


def test_confluence_api2_end_to_end():
    section("Part B: confluence_api2.py 端到端（建立分類、歸位、冪等、漏網之魚）")

    root, titles = build_tree()
    api.main()

    root_children = sorted(p["title"] for p in FAKE.children_of(root))
    check("總目錄底下只剩 4 個半年分類頁",
          root_children == ["2025H1", "2025H2", "2026H1", "2026H2"],
          f"實際: {root_children}")

    misplaced = [
        t for t in titles
        if FAKE.parent_title_of(t) != api.half_year_group_title(api.parse_report_date(t))
    ]
    check(f"全部 {len(titles)} 份既有週報都歸到正確半年分類",
          not misplaced, f"未歸位: {misplaced[:5]}")

    check("新週報 WeeklyReport_20260925 已建立",
          FAKE.by_title("WeeklyReport_20260925") is not None)
    check("新週報直接建立在 2026H2 底下",
          FAKE.parent_title_of("WeeklyReport_20260925") == "2026H2",
          f"實際父頁面: {FAKE.parent_title_of('WeeklyReport_20260925')}")

    new_body = FAKE.by_title("WeeklyReport_20260925")["body"]
    check("新週報保留表格排版", "週報排版表格" in new_body)
    check("新週報已清掉上週 daily-worklog 區塊", "daily-worklog-" not in new_body)

    group_body = FAKE.by_title("2026H2")["body"]
    check("分類頁內含 children 巨集方便瀏覽", 'ac:name="children"' in group_body)

    # 分類頁本身絕不能被當成複製範本
    baseline = api.find_latest_report()
    check("基準頁搜尋只會選到真正的週報（不會選到分類頁或總目錄）",
          api.parse_report_date(baseline["title"]) is not None,
          f"實際選到: {baseline['title']}")

    # 第二次執行：應完全無動作
    moves_before = sum(1 for m, p, *_ in FAKE.requests if m == "PUT" and "/move/" in p)
    api.main()
    moves_after = sum(1 for m, p, *_ in FAKE.requests if m == "PUT" and "/move/" in p)
    check("重複執行不會再搬移任何頁面（冪等）", moves_after == moves_before,
          f"多出 {moves_after - moves_before} 次搬移")
    check("重複執行不會重建分類頁",
          sorted(p["title"] for p in FAKE.children_of(root)) == ["2025H1", "2025H2", "2026H1", "2026H2"])

    # 漏網之魚：有人把頁面拖到總目錄外面
    stray = FAKE.by_title("WeeklyReport_20250822")
    stray["parent_id"] = "101"          # 被拖到 Project_Info 底下
    orphan = FAKE.by_title("WeeklyReport_20260109")
    orphan["parent_id"] = None          # 被拖到空間根層
    api.main()
    check("被拖到別處的週報會被收回 2025H2",
          FAKE.parent_title_of("WeeklyReport_20250822") == "2025H2",
          f"實際: {FAKE.parent_title_of('WeeklyReport_20250822')}")
    check("被拖到空間根層的週報會被收回 2026H1",
          FAKE.parent_title_of("WeeklyReport_20260109") == "2026H1",
          f"實際: {FAKE.parent_title_of('WeeklyReport_20260109')}")


def test_move_fallback():
    section("Part B2: move API 不可用時的備援搬移")

    root, titles = build_tree()
    FAKE.move_supported = False
    try:
        api.main()
        misplaced = [
            t for t in titles
            if FAKE.parent_title_of(t) != api.half_year_group_title(api.parse_report_date(t))
        ]
        check("move endpoint 不可用時，備援路徑仍能完成歸位",
              not misplaced, f"未歸位: {misplaced[:5]}")
        sample = FAKE.by_title("WeeklyReport_20250822")
        check("備援搬移保留原始內文（未改寫一個位元）",
              "週報排版表格" in sample["body"] and "daily-worklog-" in sample["body"])
    finally:
        FAKE.move_supported = True


def test_switches():
    section("Part B4: DRY RUN 與總開關")

    check("_env_flag 空值沿用預設", api._env_flag("__NOT_SET__", True) is True)
    os.environ["__FLAG_TEST__"] = "false"
    check("_env_flag 讀得到 false", api._env_flag("__FLAG_TEST__", True) is False)
    os.environ["__FLAG_TEST__"] = "true"
    check("_env_flag 讀得到 true", api._env_flag("__FLAG_TEST__", False) is True)
    os.environ["__FLAG_TEST__"] = "   "
    check("_env_flag 空白視為未設定", api._env_flag("__FLAG_TEST__", True) is True)
    os.environ.pop("__FLAG_TEST__")

    root, titles = build_tree()
    api.ORGANIZE_DRY_RUN = True
    try:
        api.main()
        children = sorted(p["title"] for p in FAKE.children_of(root))
        check("DRY RUN 不會建立任何分類頁",
              not [t for t in children if api.GROUP_TITLE_PATTERN.match(t)],
              f"實際: {children}")
        check("DRY RUN 不會搬移任何既有週報",
              all(FAKE.parent_title_of(t) == "WeeklyReport" for t in titles))
        check("DRY RUN 仍會建立新週報（暫掛舊位置）",
              FAKE.parent_title_of("WeeklyReport_20260925") == "WeeklyReport",
              f"實際: {FAKE.parent_title_of('WeeklyReport_20260925')}")
    finally:
        api.ORGANIZE_DRY_RUN = False

    root, titles = build_tree()
    api.ORGANIZE_ONLY = True
    try:
        api.main()
        check("只分類模式不會建立新週報",
              FAKE.by_title("WeeklyReport_20260925") is None)
        misplaced = [
            t for t in titles
            if FAKE.parent_title_of(t) != api.half_year_group_title(api.parse_report_date(t))
        ]
        check("只分類模式仍會把既有週報全部歸位", not misplaced, f"未歸位: {misplaced[:5]}")
        check("只分類模式會建立需要的分類頁",
              sorted(p["title"] for p in FAKE.children_of(root))
              == ["2025H1", "2025H2", "2026H1", "2026H2"])
    finally:
        api.ORGANIZE_ONLY = False

    # 首次上線會用的組合：dry_run + organize_only 必須完全不寫入任何東西。
    # 這裡重現目前的實際狀態：舊版已先把 20260925 建在總目錄下（未分類）。
    root, titles = build_tree()
    FAKE.add_page("WeeklyReport_20260925", root, body=weekly_body(date(2026, 9, 25)))
    titles.append("WeeklyReport_20260925")
    api.ORGANIZE_ONLY = True
    api.ORGANIZE_DRY_RUN = True
    try:
        mark = len(FAKE.requests)
        api.main()
        writes = [r for r in FAKE.requests[mark:] if r[0] in ("POST", "PUT", "DELETE")]
        check("dry_run + organize_only 完全沒有任何寫入請求", not writes,
              f"意外的寫入: {[(m, p) for m, p, *_ in writes][:5]}")
        check("dry_run + organize_only 不會建立新週報",
              FAKE.by_title("WeeklyReport_20261002") is None
              and FAKE.by_title("WeeklyReport_20260925") is not None)
        check("dry_run + organize_only 不會搬移任何頁面",
              all(FAKE.parent_title_of(t) == "WeeklyReport" for t in titles))
    finally:
        api.ORGANIZE_ONLY = False
        api.ORGANIZE_DRY_RUN = False

    root, titles = build_tree()
    api.ORGANIZE_ONLY = True
    api.GROUP_BY_HALF = False
    try:
        api.main()
        check("只分類模式搭配總開關關閉時完全不動作",
              FAKE.by_title("WeeklyReport_20260925") is None
              and all(FAKE.parent_title_of(t) == "WeeklyReport" for t in titles))
    finally:
        api.ORGANIZE_ONLY = False
        api.GROUP_BY_HALF = True

    root, titles = build_tree()
    api.GROUP_BY_HALF = False
    try:
        api.main()
        children = sorted(p["title"] for p in FAKE.children_of(root))
        check("總開關關閉時完全不分類",
              not [t for t in children if api.GROUP_TITLE_PATTERN.match(t)],
              f"實際: {children}")
        check("總開關關閉時維持原本行為（新頁面掛在基準頁同層）",
              FAKE.parent_title_of("WeeklyReport_20260925") == "WeeklyReport")
    finally:
        api.GROUP_BY_HALF = True


def test_pagination():
    section("Part B3: 分頁走訪（避免頁數過多時靜默漏抓）")

    FAKE.pages.clear()
    FAKE.clock = 0
    FAKE.next_id = 1000
    parent = FAKE.add_page("Bulk Parent", None, page_id=500)
    for i in range(250):
        FAKE.add_page(f"Bulk_{i:03d}", parent)

    fetched = api.list_child_pages(parent)
    check("250 個子頁面可跨 3 頁完整取回", len(fetched) == 250, f"實際取回 {len(fetched)}")
    check("跨頁結果不重複", len({p["id"] for p in fetched}) == 250)


# ---------------------------------------------------------------------------
# Part C: daily_worklog_to_confluence 在分類後仍能正確寫入
# ---------------------------------------------------------------------------

def test_daily_worklog_write_path():
    section("Part C: daily_worklog_to_confluence.py 在分類後的寫入路徑")

    import daily_worklog_to_confluence as daily

    build_tree()
    api.main()

    selected = daily.get_selected_dates()
    if not selected:
        check("daily 腳本能選出要更新的日期", False, "get_selected_dates() 回傳空清單")
        return
    target_title = daily.get_target_report_title(selected[0])

    # 讓測試不受執行當天影響：目標頁面若不在樹裡就補建並先歸位
    if not FAKE.by_title(target_title):
        report_date = api.parse_report_date(target_title)
        group = FAKE.by_title(api.half_year_group_title(report_date))
        FAKE.add_page(target_title, group["id"], body=weekly_body(report_date))

    target = FAKE.by_title(target_title)
    expected_id = target["id"]
    parent_before = FAKE.parent_title_of(target_title)
    version_before = target["version"]
    depth_before = len(FAKE._ancestor_chain(target))

    check(f"目標頁面 {target_title} 位於半年分類頁底下",
          parent_before == api.half_year_group_title(api.parse_report_date(target_title)),
          f"實際父頁面: {parent_before}")
    check("目標頁面已是三層深（Home > WeeklyReport > 分類頁）", depth_before == 3,
          f"實際深度: {depth_before}")

    mark = len(FAKE.requests)
    daily.run_sync_logic()
    calls = FAKE.requests[mark:]

    lookups = [r for r in calls
               if r[0] == "GET" and r[1].endswith("/wiki/rest/api/content")
               and r[2].get("title") == target_title]
    check("巢狀後仍以標題查詢找到頁面（查詢無父頁面/深度條件）", bool(lookups),
          f"未發現 title={target_title} 的查詢")

    writes = [r for r in calls if r[0] == "PUT" and r[1].endswith(f"/content/{expected_id}")]
    check(f"寫入目標為正確頁面 ID {expected_id}", bool(writes),
          f"實際 PUT: {[r[1] for r in calls if r[0] == 'PUT']}")

    if writes:
        payload = writes[-1][3] or {}
        check("寫入 payload 不含 ancestors（不會主動改動父頁面）",
              "ancestors" not in payload, f"payload keys: {sorted(payload)}")
        check("寫入 payload 帶正確標題與型別",
              payload.get("title") == target_title and payload.get("type") == "page")
        check("寫入 payload 有遞增版本號",
              (payload.get("version") or {}).get("number") == version_before + 1)

    after = FAKE.by_title(target_title)
    check("寫入後頁面仍留在原本的半年分類頁底下",
          FAKE.parent_title_of(target_title) == parent_before,
          f"寫入前: {parent_before} / 寫入後: {FAKE.parent_title_of(target_title)}")
    check("寫入後頁面 ID 未改變", after["id"] == expected_id)
    check("寫入後階層深度未改變", len(FAKE._ancestor_chain(after)) == depth_before)
    check("寫入後 mention 區塊上方的表格排版仍保留", "週報排版表格" in after["body"])
    check("寫入後舊日誌區塊已被清除", "daily-worklog-" not in after["body"])
    check("寫入後舊的日期備註已被清除（確認清除邏輯真的跑到）",
          "既有備註" not in after["body"])

    stray_moves = [r for r in calls if r[0] == "PUT" and "/move/" in r[1]]
    check("daily 腳本全程沒有呼叫任何搬移 API", not stray_moves,
          f"意外的搬移: {[r[1] for r in stray_moves]}")

    # 分類頁與總目錄頁都不該被 daily 腳本碰到
    protected = {FAKE.by_title("WeeklyReport")["id"]} | {
        FAKE.by_title(t)["id"] for t in ("2025H1", "2025H2", "2026H1", "2026H2")
        if FAKE.by_title(t)
    }
    touched = [r[1] for r in calls
               if r[0] in ("PUT", "POST") and any(f"/content/{pid}" in r[1] for pid in protected)]
    check("daily 腳本不會寫入總目錄頁或分類頁", not touched, f"被寫入: {touched}")


def main():
    install_fake_transport()
    print("=== WeeklyReport 半年分類驗證 (不需憑證) ===")
    test_pure_functions()
    test_confluence_api2_end_to_end()
    test_move_fallback()
    test_switches()
    test_pagination()
    test_daily_worklog_write_path()

    print(f"\n{'=' * 72}")
    print(f"結果: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("失敗項目:")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
