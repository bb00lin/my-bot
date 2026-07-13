#!/usr/bin/env python3
"""
SharePoint Register (S) -> Confluence mail_checking (C) -> Jira PMWC sync.

S 表格為唯一資料來源；C 與 Jira 完全跟隨 S。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv
from openpyxl import load_workbook

# ---------------------------------------------------------------------------
# Config & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
# 允許 EVT2-01 這類「字母+數字」前綴（舊版僅允許純字母，導致 EVT2 項目被忽略）
REGISTER_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$", re.IGNORECASE)
JIRA_KEY_RE = re.compile(r"PMWC-\d+")
CONFLUENCE_JIRA_LINK_RE = re.compile(
    r"\[?(PMWC-\d+)\]?\(?https?://[^)\s|]+/browse/(PMWC-\d+)\)?"
)

S_COLUMNS = [
    "ID",
    "Workstream",
    "Title",
    "Description / current state",
    "Next action",
    "Ball with",
    "Priority",
    "Status",
    "Opened",
    "Target close",
    "Next milestone / due",
    "Source / reference",
]
LINK_COLUMN = "LINK"
C_COLUMNS = S_COLUMNS + [LINK_COLUMN]

# 表頭名稱 -> RegisterRow 屬性（可選欄位如 JIRA / QSI Comment 不在現行 S 表時留空）
S_COLUMN_ATTR: dict[str, str] = {
    "ID": "register_id",
    "JIRA": "jira",
    "Workstream": "workstream",
    "Title": "title",
    "Description / current state": "description",
    "Next action": "next_action",
    "Ball with": "ball_with",
    "Priority": "priority",
    "Status": "status",
    "Opened": "opened",
    "Target close": "target_close",
    "Next milestone / due": "next_milestone",
    "Source / reference": "source",
    "QSI Comment": "qsi_comment",
}

# Jira 實際狀態名稱
JIRA_EXACT_STATUSES: frozenset[str] = frozenset(
    {
        "待辦事項",
        "BLOCKED",
        "CANDIDATE",
        "RESUME",
        "進行中",
        "WAITING",
        "ABORT",
        "完成",
    }
)
# Jira 畫面英文狀態名（大小寫不敏感完全匹配）
JIRA_ENGLISH_STATUSES: frozenset[str] = frozenset(
    {"BLOCKED", "CANDIDATE", "RESUME", "WAITING", "ABORT"}
)

# 少數異體字別名（表通常用英文，但仍相容）
JIRA_STATUS_ALIASES: dict[str, str] = {
    "进行中": "進行中",
}

# 英文同義詞 → Jira 狀態（正規化後完全相符；鍵一律小寫、空格統一）
ENGLISH_STATUS_SYNONYMS: dict[str, str] = {
    # → 完成
    "done": "完成",
    "closed": "完成",
    "completed": "完成",
    "complete": "完成",
    # → 進行中
    "in progress": "進行中",
    "in-progress": "進行中",
    "inprogress": "進行中",
    "doing": "進行中",
    "working": "進行中",
    # → 待辦事項
    "open": "待辦事項",
    "todo": "待辦事項",
    "to do": "待辦事項",
    "to-do": "待辦事項",
    "backlog": "待辦事項",
    "new": "待辦事項",
    # → BLOCKED / WAITING / …
    "blocked": "BLOCKED",
    "block": "BLOCKED",
    "waiting": "WAITING",
    "wait": "WAITING",
    "candidate": "CANDIDATE",
    "resume": "RESUME",
    "abort": "ABORT",
    "aborted": "ABORT",
    "cancelled": "ABORT",
    "canceled": "ABORT",
}

# 子字串 fallback（最後手段；Blocked > Closed > In progress > Open）
STATUS_RULES: list[tuple[str, str]] = [
    ("blocked", "BLOCKED"),
    ("closed", "完成"),
    ("completed", "完成"),
    ("in progress", "進行中"),
    ("in-progress", "進行中"),
    ("open", "待辦事項"),
]
DEFAULT_JIRA_STATUS = "待辦事項"

# Team-managed Jira：畫面類型名與 JQL 可用名不一致（例：顯示「任務」但 JQL 需 Task；
# 「大型工作」反而要用中文）。搜尋時依序嘗試。
JIRA_ISSUETYPE_JQL_ALIASES: dict[str, str] = {
    "任務": "Task",
    "大型工作": "Epic",
    "史詩": "Epic",
    "故事": "Story",
    "錯誤": "Bug",
    "子任務": "Sub-task",
}


def _jql_quote(value: str) -> str:
    """Quote a JQL string literal; escape reserved wildcards."""
    escaped = (
        (value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("?", "\\u003f")
        .replace("*", "\\u002a")
    )
    return f'"{escaped}"'


def jql_issuetype_equals(type_name: str) -> str:
    """產生單一 issuetype JQL 條件。"""
    name = (type_name or "").strip()
    if not name:
        raise ValueError("issuetype name is empty")
    # ASCII simple tokens only — never leave ?/* unquoted (JQL wildcards).
    if (
        name.isascii()
        and " " not in name
        and "-" not in name
        and "?" not in name
        and "*" not in name
    ):
        return f"issuetype = {name}"
    return f"issuetype = {_jql_quote(name)}"


def jql_issuetype_name_candidates(type_name: str) -> list[str]:
    """回傳 JQL 要嘗試的類型名稱清單（設定值 + 別名 + 英文 fallback）。"""
    name = (type_name or "").strip()
    candidates: list[str] = []
    # Skip clearly corrupted names (e.g. secret encoding lost Chinese → ???)
    if name and set(name) != {"?"}:
        candidates.append(name)
    alias = JIRA_ISSUETYPE_JQL_ALIASES.get(name)
    if alias and alias not in candidates:
        candidates.append(alias)
    # CI/config mojibake fallback: still try common English types.
    if not candidates or "?" in name:
        for eng in ("Task", "Epic"):
            if eng not in candidates:
                candidates.append(eng)
    return candidates



@dataclass
class RegisterRow:
    """S 表格一列；欄位與 S 表表頭同名，LINK 僅在輸出 C 表時由 jira_key 填入。"""

    register_id: str
    jira: str = ""
    workstream: str = ""
    title: str = ""
    description: str = ""
    next_action: str = ""
    ball_with: str = ""
    priority: str = ""
    status: str = ""
    opened: str = ""
    target_close: str = ""
    next_milestone: str = ""
    source: str = ""
    qsi_comment: str = ""
    jira_key: str = ""


@dataclass
class SyncReport:
    created_epics: list[str] = field(default_factory=list)
    created_issues: list[str] = field(default_factory=list)
    updated_issues: list[str] = field(default_factory=list)
    deleted_issues: list[str] = field(default_factory=list)
    web_links_added: list[str] = field(default_factory=list)
    ranked_issues: list[str] = field(default_factory=list)
    assigned_issues: list[str] = field(default_factory=list)
    confluence_updated: bool = False
    errors: list[str] = field(default_factory=list)
    diff_log_path: Path | None = None


SNAPSHOT_FIELDS: list[tuple[str, str]] = [
    ("jira", "JIRA"),
    ("workstream", "Workstream"),
    ("title", "Title"),
    ("description", "Description / current state"),
    ("next_action", "Next action"),
    ("ball_with", "Ball with"),
    ("priority", "Priority"),
    ("status", "Status"),
    ("opened", "Opened"),
    ("target_close", "Target close"),
    ("next_milestone", "Next milestone / due"),
    ("source", "Source / reference"),
    ("qsi_comment", "QSI Comment"),
]


@dataclass
class FieldChange:
    register_id: str
    field_label: str
    old_value: str
    new_value: str


@dataclass
class RegisterDiff:
    added_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    status_changes: list[FieldChange] = field(default_factory=list)
    field_changes: list[FieldChange] = field(default_factory=list)
    is_first_run: bool = False
    snapshot_saved_at: str = ""


def load_config(path: Path) -> dict[str, Any]:
    load_dotenv(SCRIPT_DIR / ".env")
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = __import__("os").environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 ATLASSIAN_API_TOKEN，請在 .env 設定。")
    cfg["_api_token"] = token
    return cfg


def _normalize_status_key(text: str) -> str:
    """英文狀態比對用：casefold、壓縮空白、連字號／底線空白化。"""
    value = (text or "").strip().casefold()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value)
    return value


def map_status_to_jira(status: str) -> str:
    """將 S/C 表 Status（預期為英文）對應到 Jira 狀態名。

    優先順序（**所有英文比對一律大小寫不敏感**，使用 casefold）：
    1. 完全符合 Jira 英文狀態名（BLOCKED / CANDIDATE / RESUME / WAITING / ABORT）
    2. 英文同義詞 → 中文／英文 Jira 狀態（Todo/TODO/todo→待辦事項、Done/DONE→完成…）
    3. 少數非英文別名（进行中）或已是 Jira 中文狀態名
    4. 子字串 fallback（blocked / closed / in progress / open）
    """
    text = (status or "").strip()
    if not text:
        return DEFAULT_JIRA_STATUS

    # 1) Jira 英文狀態名（大小寫不敏感）
    english_by_fold = {name.casefold(): name for name in JIRA_ENGLISH_STATUSES}
    folded = text.casefold()
    if folded in english_by_fold:
        return english_by_fold[folded]

    # 2) 英文同義詞完整匹配（不含前後額外敘述）
    key = _normalize_status_key(text)
    synonym_by_fold = {k.casefold(): v for k, v in ENGLISH_STATUS_SYNONYMS.items()}
    if key in synonym_by_fold:
        return synonym_by_fold[key]
    compact = re.sub(r"[\s\-]+", "", key)
    compact_map = {
        re.sub(r"[\s\-]+", "", k.casefold()): v
        for k, v in ENGLISH_STATUS_SYNONYMS.items()
    }
    if compact in compact_map:
        return compact_map[compact]

    # 3) 已是 Jira 狀態名或別名（相容舊資料／偶然中文）
    if text in JIRA_STATUS_ALIASES:
        return JIRA_STATUS_ALIASES[text]
    if text in JIRA_EXACT_STATUSES:
        return text

    # 4) 子字串 fallback（小寫／casefold）
    lowered = text.casefold()
    for needle, jira_status in STATUS_RULES:
        if needle.casefold() in lowered:
            return jira_status
    return DEFAULT_JIRA_STATUS


def normalize_jira_status_name(status: str) -> str:
    """正規化為 Jira transition 目標名稱。"""
    return map_status_to_jira(status) if status else DEFAULT_JIRA_STATUS


def _excel_serial_to_datetime(value: float) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=float(value))


def parse_flexible_date(value: Any, *, default_year: int | None = None) -> str | None:
    """將各種日期格式轉為 YYYY-MM-DD；無法解析則回傳 None。

    無年份的格式（如 7月17日、7/17）以 default_year 或當年度補齊。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        return _excel_serial_to_datetime(float(value)).strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _excel_serial_to_datetime(float(text)).strftime("%Y-%m-%d")

    year_default = default_year if default_year is not None else datetime.now().year

    # 2026年7月17日 / 2026年07月17号
    m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})[日号]?", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            return None

    # 7月17日 / 07月17号（無年份）
    m = re.fullmatch(r"(\d{1,2})月\s*(\d{1,2})[日号]?", text)
    if m:
        try:
            return datetime(year_default, int(m.group(1)), int(m.group(2))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            return None

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%m/%d", "%m-%d-%Y", "%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt in ("%m/%d", "%m-%d"):
                parsed = parsed.replace(year=year_default)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def excel_serial_to_date(value: Any) -> str:
    return parse_flexible_date(value) or (str(value).strip() if value not in (None, "") else "")


def normalize_register_id(raw: str) -> str:
    return strip_markdown_cell(raw).strip("_").upper()


def strip_markdown_cell(text: str) -> str:
    """移除 Confluence/Markdown 的粗斜體標記。"""
    value = (text or "").strip()
    value = re.sub(r"^\*+|\*+$", "", value)
    value = re.sub(r"^_+|_+$", "", value)
    return value.strip()


def is_valid_jira_key(key: str) -> bool:
    return bool(JIRA_KEY_RE.fullmatch((key or "").strip()))


def is_blank_table_row(cells: list[str]) -> bool:
    return not any(strip_markdown_cell(cell) for cell in cells)


def escape_html(text: str) -> str:
    return html.escape((text or "").replace("\n", " "), quote=True)


def normalize_cell_text(value: Any) -> str:
    """Excel 儲存格轉顯示用文字；全形空白視為空。"""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text or text.replace("\u3000", "").strip() == "":
        return ""
    return text


def s_row_to_register(row: dict[str, Any]) -> RegisterRow | None:
    rid = normalize_register_id(str(row.get("ID", "")))
    if not rid or not REGISTER_ID_RE.match(rid):
        return None

    return RegisterRow(
        register_id=rid,
        jira=normalize_cell_text(row.get("JIRA")),
        workstream=normalize_cell_text(row.get("Workstream")),
        title=normalize_cell_text(row.get("Title")),
        description=normalize_cell_text(row.get("Description / current state")),
        next_action=normalize_cell_text(row.get("Next action")),
        ball_with=normalize_cell_text(row.get("Ball with")),
        priority=normalize_cell_text(row.get("Priority")),
        status=normalize_cell_text(row.get("Status")),
        opened=excel_serial_to_date(row.get("Opened")),
        target_close=excel_serial_to_date(row.get("Target close")),
        next_milestone=normalize_cell_text(row.get("Next milestone / due")),
        source=normalize_cell_text(row.get("Source / reference")),
        qsi_comment=normalize_cell_text(row.get("QSI Comment")),
    )


def jira_epic_priority(row: RegisterRow) -> str:
    """S 表 Priority 欄 → P1/P2/P3 Epic 標籤。"""
    return row.priority or "P1"


def jira_status_source(row: RegisterRow) -> str:
    """S 表 Status 欄 → Open/Closed 等狀態字串。"""
    return row.status


# ---------------------------------------------------------------------------
# Snapshot & diff
# ---------------------------------------------------------------------------


def register_row_to_snapshot_dict(row: RegisterRow) -> dict[str, str]:
    return {attr: str(getattr(row, attr) or "") for attr, _ in SNAPSHOT_FIELDS}


def load_last_snapshot(path: Path) -> tuple[dict[str, dict[str, str]], str]:
    if not path.exists():
        return {}, ""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows", {})
    saved_at = str(data.get("saved_at", ""))
    return rows, saved_at


def save_last_snapshot(path: Path, rows: list[RegisterRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "rows": {r.register_id: register_row_to_snapshot_dict(r) for r in rows},
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compute_register_diff(
    old_rows: dict[str, dict[str, str]],
    new_rows: list[RegisterRow],
    *,
    snapshot_saved_at: str = "",
) -> RegisterDiff:
    diff = RegisterDiff()
    diff.is_first_run = not old_rows
    diff.snapshot_saved_at = snapshot_saved_at

    old_ids = set(old_rows.keys())
    new_ids = {r.register_id for r in new_rows}

    if diff.is_first_run:
        diff.added_ids = sorted(new_ids)
        return diff

    diff.added_ids = sorted(new_ids - old_ids)
    diff.removed_ids = sorted(old_ids - new_ids)

    new_map = {r.register_id: register_row_to_snapshot_dict(r) for r in new_rows}
    for rid in sorted(new_ids & old_ids):
        old_row = old_rows[rid]
        new_row = new_map[rid]
        for attr, label in SNAPSHOT_FIELDS:
            old_val = old_row.get(attr, "")
            new_val = new_row.get(attr, "")
            if old_val == new_val:
                continue
            change = FieldChange(rid, label, old_val, new_val)
            if attr == "status":
                diff.status_changes.append(change)
            else:
                diff.field_changes.append(change)

    return diff


def _truncate_diff_value(text: str, max_len: int = 80) -> str:
    value = (text or "").replace("\n", " ")
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def format_diff_report(diff: RegisterDiff, *, dry_run: bool = False) -> str:
    lines: list[str] = ["========== S 表格變更摘要 =========="]

    if diff.is_first_run:
        lines.append("比對基準: 首次執行（無前次快照）")
    else:
        saved = diff.snapshot_saved_at or "未知"
        lines.append(f"比對基準: 上次同步快照 ({saved})")

    if dry_run:
        lines.append("模式: dry-run（預覽，不寫入）")

    lines.append("")

    if diff.is_first_run:
        lines.append(f"【首次快照】目前 S 表格共 {len(diff.added_ids)} 筆有效 ID")
        for rid in diff.added_ids:
            lines.append(f"  • {rid}")
        return "\n".join(lines)

    lines.append(f"【新增】{len(diff.added_ids)} 筆")
    if diff.added_ids:
        for rid in diff.added_ids:
            lines.append(f"  + {rid}")
    else:
        lines.append("  （無）")

    lines.append("")
    lines.append(f"【移除】{len(diff.removed_ids)} 筆")
    if diff.removed_ids:
        for rid in diff.removed_ids:
            lines.append(f"  - {rid}")
    else:
        lines.append("  （無）")

    lines.append("")
    lines.append(f"【狀態變更】{len(diff.status_changes)} 筆")
    if diff.status_changes:
        for change in diff.status_changes:
            old = change.old_value or "（空）"
            new = change.new_value or "（空）"
            lines.append(f"  {change.register_id}: {old} → {new}")
    else:
        lines.append("  （無）")

    lines.append("")
    lines.append(f"【欄位變更】{len(diff.field_changes)} 筆")
    if diff.field_changes:
        for change in diff.field_changes:
            old = _truncate_diff_value(change.old_value) or "（空）"
            new = _truncate_diff_value(change.new_value) or "（空）"
            lines.append(
                f"  {change.register_id} / {change.field_label}: "
                f"{old!r} → {new!r}"
            )
    else:
        lines.append("  （無）")

    total = (
        len(diff.added_ids)
        + len(diff.removed_ids)
        + len(diff.status_changes)
        + len(diff.field_changes)
    )
    lines.append("")
    if total == 0:
        lines.append("本次 S 表格與上次快照完全相同，無任何變更。")

    return "\n".join(lines)


def _format_sync_result_section(report: SyncReport) -> str:
    lines = ["========== 同步執行結果 =========="]
    if report.created_epics:
        lines.append(f"新建 Epic: {', '.join(report.created_epics)}")
    if report.created_issues:
        lines.append(f"新建任務: {', '.join(report.created_issues)}")
    if report.updated_issues:
        lines.append(f"更新任務: {', '.join(report.updated_issues)}")
    if report.deleted_issues:
        lines.append(f"刪除任務: {', '.join(report.deleted_issues)}")
    if report.web_links_added:
        lines.append(f"新增 Web 連結: {', '.join(report.web_links_added)}")
    if report.ranked_issues:
        lines.append(f"重排 Rank: {len(report.ranked_issues)} 筆")
    if report.assigned_issues:
        lines.append(f"指派受託人: {', '.join(report.assigned_issues)}")
    lines.append(
        f"Confluence 已更新: {'是' if report.confluence_updated else '否（dry-run 或未變更）'}"
    )
    if report.errors:
        lines.append("")
        lines.append("錯誤:")
        for err in report.errors:
            lines.append(f"  - {err}")
    return "\n".join(lines)


def write_sync_log(
    log_dir: Path,
    diff: RegisterDiff,
    diff_text: str,
    report: SyncReport,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"sync_{timestamp}.log"
    content = "\n\n".join([diff_text, _format_sync_result_section(report)])
    log_path.write_text(content + "\n", encoding="utf-8")

    summary_path = log_dir / "sync_log.txt"
    summary_line = (
        f"{datetime.now().isoformat(timespec='seconds')} | {log_path.name} | "
        f"新增={len(diff.added_ids)} 移除={len(diff.removed_ids)} "
        f"狀態={len(diff.status_changes)} 欄位={len(diff.field_changes)} "
        f"錯誤={len(report.errors)}\n"
    )
    with summary_path.open("a", encoding="utf-8") as f:
        f.write(summary_line)

    return log_path


def print_diff_report(diff: RegisterDiff, *, dry_run: bool = False) -> str:
    text = format_diff_report(diff, dry_run=dry_run)
    print(f"\n{text}")
    return text


# ---------------------------------------------------------------------------
# SharePoint / Excel
# ---------------------------------------------------------------------------


def download_sharepoint_excel(url: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "register_latest.xlsx"
    resp = requests.get(url, timeout=120, allow_redirects=True)
    resp.raise_for_status()
    if len(resp.content) < 1000:
        raise RuntimeError("下載的檔案過小，可能不是有效的 xlsx。")
    dest.write_bytes(resp.content)
    return dest


def _cell_column_letter(cell: Any) -> str | None:
    letter = getattr(cell, "column_letter", None)
    return letter if letter else None


def build_column_letter_header_map(header_cells: tuple[Any, ...]) -> dict[str, str]:
    """表頭列：欄位字母 (A, B, C…) -> 表頭名稱。"""
    mapping: dict[str, str] = {}
    for cell in header_cells:
        letter = _cell_column_letter(cell)
        if not letter or cell.value is None:
            continue
        label = str(cell.value).strip()
        if label:
            mapping[letter] = label
    return mapping


def excel_row_to_dict_by_column_letter(
    row_cells: tuple[Any, ...],
    col_letter_to_header: dict[str, str],
) -> dict[str, Any]:
    """以儲存格欄位字母對應表頭，避免 read_only 稀疏列造成 tuple 索引錯位。"""
    row_dict: dict[str, Any] = {}
    for cell in row_cells:
        letter = _cell_column_letter(cell)
        if not letter:
            continue
        header = col_letter_to_header.get(letter)
        if header:
            row_dict[header] = cell.value
    return row_dict


def load_s_column_headers(header_cells: tuple[Any, ...]) -> list[str]:
    """依 Excel 表頭列順序（A, B, C…）回傳欄位名稱。"""
    headers: list[str] = []
    for cell in header_cells:
        if cell.value is None:
            continue
        label = str(cell.value).strip()
        if label:
            headers.append(label)
    return headers


def c_columns_for(s_columns: list[str]) -> list[str]:
    return s_columns + [LINK_COLUMN]


def cell_value_for_s_column(row: RegisterRow, column: str) -> str:
    if column == "ID":
        return row.register_id
    attr = S_COLUMN_ATTR.get(column)
    if not attr:
        return ""
    return str(getattr(row, attr) or "")


def load_register_rows_from_excel(
    xlsx_path: Path, sheet_name: str
) -> tuple[list[str], list[RegisterRow]]:
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise RuntimeError(f"找不到工作表 {sheet_name!r}，現有：{wb.sheetnames}")
    ws = wb[sheet_name]
    rows_iter = ws.iter_rows()
    header_cells = next(rows_iter, None)
    if not header_cells:
        wb.close()
        return [], []
    s_columns = load_s_column_headers(header_cells)
    col_letter_to_header = build_column_letter_header_map(header_cells)
    result: list[RegisterRow] = []
    for row_cells in rows_iter:
        row_dict = excel_row_to_dict_by_column_letter(row_cells, col_letter_to_header)
        item = s_row_to_register(row_dict)
        if item:
            result.append(item)
    wb.close()
    return s_columns, result


# ---------------------------------------------------------------------------
# Atlassian HTTP client
# ---------------------------------------------------------------------------


class AtlassianClient:
    def __init__(self, site_url: str, cloud_id: str, email: str, api_token: str):
        self.site_url = site_url.rstrip("/")
        self.cloud_id = cloud_id
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json"})
        self.jira_base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        self.jira_agile_base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/agile/1.0"
        self.confluence_v2 = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2"

    def jira_get(self, path: str, **params: Any) -> Any:
        r = self.session.get(f"{self.jira_base}{path}", params=params, timeout=60)
        if not r.ok:
            raise RuntimeError(f"Jira GET {path} failed: {r.status_code} {r.text}")
        return r.json() if r.text else {}

    def jira_search(
        self,
        jql: str,
        *,
        max_results: int = 100,
        fields: list[str] | None = None,
        next_page_token: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields or ["summary", "status", "parent"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        return self.jira_post("/search/jql", payload)

    def jira_post(self, path: str, payload: dict) -> Any:
        r = self.session.post(
            f"{self.jira_base}{path}", json=payload, timeout=60
        )
        if not r.ok:
            raise RuntimeError(f"Jira POST {path} failed: {r.status_code} {r.text}")
        return r.json() if r.text else {}

    def jira_put(self, path: str, payload: dict) -> None:
        r = self.session.put(
            f"{self.jira_base}{path}", json=payload, timeout=60
        )
        if not r.ok:
            raise RuntimeError(f"Jira PUT {path} failed: {r.status_code} {r.text}")

    def jira_delete(self, path: str) -> None:
        r = self.session.delete(f"{self.jira_base}{path}", timeout=60)
        if not r.ok:
            raise RuntimeError(f"Jira DELETE {path} failed: {r.status_code} {r.text}")

    def jira_agile_put(self, path: str, payload: dict) -> None:
        r = self.session.put(
            f"{self.jira_agile_base}{path}", json=payload, timeout=60
        )
        if not r.ok:
            raise RuntimeError(
                f"Jira Agile PUT {path} failed: {r.status_code} {r.text}"
            )

    def confluence_get(self, path: str, **params: Any) -> Any:
        r = self.session.get(f"{self.confluence_v2}{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def confluence_put(self, path: str, payload: dict) -> Any:
        r = self.session.put(
            f"{self.confluence_v2}{path}", json=payload, timeout=60
        )
        if not r.ok:
            raise RuntimeError(f"Confluence PUT failed: {r.status_code} {r.text}")
        return r.json()


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------


def _extract_jira_key_from_cell(cell_content: str) -> str | None:
    href_match = re.search(
        r'href="[^"]*/browse/(PMWC-\d+)"', cell_content, flags=re.IGNORECASE
    )
    if href_match:
        return href_match.group(1)
    text = re.sub(r"<[^>]+>", "", cell_content)
    m = CONFLUENCE_JIRA_LINK_RE.search(text) or JIRA_KEY_RE.search(text)
    return m.group(1) if m else None


def parse_jira_map_from_confluence_body(body: str) -> dict[str, str]:
    """從 C 表格 markdown/HTML 解析 ID -> PMWC key。

    優先讀 LINK 欄（最後一欄）；相容舊版將 JIRA 放在第 2 欄的頁面。
    """
    mapping: dict[str, str] = {}
    if "<table" in body.lower():
        for row_html in re.findall(r"<tr>(.*?)</tr>", body, flags=re.DOTALL | re.IGNORECASE):
            if re.search(r"<th\b", row_html, flags=re.IGNORECASE):
                continue
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.DOTALL | re.IGNORECASE)
            if len(cells) < 2 or is_blank_table_row(cells):
                continue
            rid = normalize_register_id(re.sub(r"<[^>]+>", "", cells[0]))
            if not REGISTER_ID_RE.match(rid):
                continue
            key = _extract_jira_key_from_cell(cells[-1])
            if not key and len(cells) >= 2:
                key = _extract_jira_key_from_cell(cells[1])
            if key:
                mapping[rid] = key
        if mapping:
            return mapping

    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [strip_markdown_cell(c) for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or is_blank_table_row(cells):
            continue
        rid = normalize_register_id(cells[0])
        if not REGISTER_ID_RE.match(rid):
            continue
        key = _extract_jira_key_from_cell(cells[-1])
        if not key and len(cells) >= 2:
            key = _extract_jira_key_from_cell(cells[1])
        if key:
            mapping[rid] = key
    return mapping


def fetch_confluence_page(client: AtlassianClient, page_id: str) -> dict[str, Any]:
    return client.confluence_get(
        f"/pages/{page_id}",
        **{"body-format": "markdown"},
    )


def extract_confluence_body_text(page: dict[str, Any]) -> str:
    """從 Confluence v2 page JSON 取出正文（markdown / storage 等格式）。"""
    body = page.get("body") or {}
    if isinstance(body, str):
        return body
    for fmt in ("markdown", "storage", "view"):
        section = body.get(fmt)
        if isinstance(section, str):
            return section
        if isinstance(section, dict):
            value = section.get("value")
            if isinstance(value, str):
                return value
    value = body.get("value")
    return value if isinstance(value, str) else ""


def register_row_to_s_cell_values(
    row: RegisterRow, s_columns: list[str] | None = None
) -> list[str]:
    """依 S 表欄位順序輸出 C 表儲存格值（不含 LINK）。"""
    columns = s_columns if s_columns is not None else S_COLUMNS
    return [cell_value_for_s_column(row, col) for col in columns]


def register_row_to_c_cells(
    row: RegisterRow, s_columns: list[str] | None = None
) -> list[str]:
    """S 欄位 + LINK 欄文字值（LINK 為 PMWC key 或空）。"""
    cells = register_row_to_s_cell_values(row, s_columns)
    cells.append(row.jira_key if is_valid_jira_key(row.jira_key) else "")
    return cells


def jira_browse_url(key: str, site_url: str) -> str:
    return f"{site_url.rstrip('/')}/browse/{key}"


def jira_cell_html(key: str, site_url: str) -> str:
    url = jira_browse_url(key, site_url)
    return f'<a href="{escape_html(url)}">{escape_html(key)}</a>'


def jira_cell_markdown(key: str, site_url: str) -> str:
    url = jira_browse_url(key, site_url)
    return f"[{key}]({url})"


def build_confluence_html_table(
    rows: list[RegisterRow], site_url: str, s_columns: list[str] | None = None
) -> str:
    """以 Confluence storage HTML 輸出表格：S 欄位順序 + 最後 LINK 欄。"""
    columns = c_columns_for(s_columns) if s_columns is not None else C_COLUMNS
    data_columns = s_columns if s_columns is not None else S_COLUMNS
    parts = ['<table data-layout="full-width"><tbody>']
    header_cells = "".join(
        f"<th><p><strong>{escape_html(col)}</strong></p></th>" for col in columns
    )
    parts.append(f"<tr>{header_cells}</tr>")
    for row in rows:
        row_cells = [
            f"<td><p>{escape_html(value)}</p></td>"
            for value in register_row_to_s_cell_values(row, data_columns)
        ]
        link_inner = (
            jira_cell_html(row.jira_key, site_url)
            if is_valid_jira_key(row.jira_key)
            else ""
        )
        row_cells.append(f"<td><p>{link_inner}</p></td>")
        parts.append(f"<tr>{''.join(row_cells)}</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def resolve_sharepoint_source_link(cfg: dict[str, Any]) -> tuple[str, str]:
    """回傳 Confluence 頁首 S 表連結 (url, title)。"""
    conf = cfg.get("confluence", {})
    sp = cfg.get("sharepoint", {})
    url = (
        conf.get("source_link_url")
        or sp.get("view_url")
        or sp.get("download_url")
        or ""
    ).strip()
    title = (conf.get("source_link_title") or "Open Issues Register (SharePoint)").strip()
    return url, title


def resolve_sync_action_link(cfg: dict[str, Any]) -> tuple[str, str]:
    """回傳 Confluence 頁首「立即同步」連結 (url, title)。

    建議指向 GitHub Actions workflow 頁面（手動 Run workflow），
    勿把 webhook / PAT 密鑰寫進 Confluence URL。
    """
    conf = cfg.get("confluence", {})
    url = (conf.get("sync_action_url") or "").strip()
    title = (conf.get("sync_action_title") or "立即同步").strip()
    return url, title


def build_confluence_source_link_html(
    url: str,
    title: str,
    *,
    sync_action_url: str = "",
    sync_action_title: str = "立即同步",
) -> str:
    """Confluence 頁首：S 表來源連結 + 可選「立即同步」按鈕樣式連結。"""
    if not url and not sync_action_url:
        return ""
    parts: list[str] = ["<p>"]
    if url:
        parts.append(
            f"<strong>S 表格：</strong>"
            f'<a href="{escape_html(url)}">{escape_html(title)}</a>'
        )
    if sync_action_url:
        label = (sync_action_title or "立即同步").strip() or "立即同步"
        # 紅色按鈕外觀，置於 S 表格連結旁（對應頁首紅框區域）
        margin = "margin-left:12px;" if url else ""
        parts.append(
            f'<a href="{escape_html(sync_action_url)}" '
            f'style="display:inline-block;{margin}padding:4px 12px;'
            f"background-color:#DE350B;color:#FFFFFF;text-decoration:none;"
            f'border-radius:3px;font-weight:bold;">'
            f"{escape_html(label)}</a>"
        )
    parts.append("</p>")
    return "".join(parts)


def build_confluence_page_html(
    rows: list[RegisterRow],
    site_url: str,
    s_columns: list[str] | None = None,
    *,
    source_link_url: str = "",
    source_link_title: str = "",
    sync_action_url: str = "",
    sync_action_title: str = "立即同步",
) -> str:
    """Confluence 頁面正文：S 表連結、立即同步按鈕 + 資料表格。"""
    prefix = build_confluence_source_link_html(
        source_link_url,
        source_link_title,
        sync_action_url=sync_action_url,
        sync_action_title=sync_action_title,
    )
    table = build_confluence_html_table(rows, site_url, s_columns)
    return prefix + table


def build_confluence_markdown(
    rows: list[RegisterRow], site_url: str, s_columns: list[str] | None = None
) -> str:
    """保留 markdown 輸出供除錯；正式更新請用 build_confluence_html_table。"""
    columns = c_columns_for(s_columns) if s_columns is not None else C_COLUMNS
    data_columns = s_columns if s_columns is not None else S_COLUMNS
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, sep]
    for row in rows:
        cells = register_row_to_s_cell_values(row, data_columns)
        link_cell = (
            jira_cell_markdown(row.jira_key, site_url)
            if is_valid_jira_key(row.jira_key)
            else ""
        )
        cells.append(link_cell)
        escaped = [cell.replace("|", "\\|").replace("\n", " ") for cell in cells]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines) + "\n"


def update_confluence_page(
    client: AtlassianClient,
    page_id: str,
    title: str,
    table_html: str,
    version_message: str,
    dry_run: bool,
) -> bool:
    page = fetch_confluence_page(client, page_id)
    version = page["version"]["number"]
    payload = {
        "id": page_id,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": table_html},
        "version": {"number": version + 1, "message": version_message},
    }
    if dry_run:
        print(
            f"[dry-run] Confluence page {page_id} would update to v{version + 1} "
            f"({len(table_html)} bytes HTML, {table_html.count('<tr>') - 1} data rows)"
        )
        return False
    client.confluence_put(f"/pages/{page_id}", payload)
    return True


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


def build_timeline_fields(
    row: RegisterRow, sync_date: str, cfg: dict[str, Any]
) -> dict[str, str]:
    """Target close 有值時，設定 Jira Timeline：起始日 = Opened，截止日 = Target close。"""
    due = parse_flexible_date(row.target_close)
    if not due:
        return {}

    start = parse_flexible_date(row.opened) or sync_date
    due_dt = datetime.strptime(due, "%Y-%m-%d")
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    if due_dt < start_dt:
        due_dt = due_dt.replace(year=start_dt.year + 1)
        due = due_dt.strftime("%Y-%m-%d")

    jira_cfg = cfg.get("jira", {})
    start_field = jira_cfg.get("start_date_field", "customfield_10015")
    due_field = jira_cfg.get("due_date_field", "duedate")
    return {due_field: due, start_field: start}


def row_has_timeline(row: RegisterRow) -> bool:
    """Target close 有值（會寫入起訖日）即視為有時間軸。"""
    return bool(parse_flexible_date(row.target_close))


def timeline_rank_group(row: RegisterRow) -> int:
    """時間軸排序群組：0=有時間軸 → 1=進行中 → 2=其他開放 → 3=完成。

    已完成永遠置底（即使曾有 Target close）。
    """
    jira_status = map_status_to_jira(jira_status_source(row))
    if jira_status == "完成":
        return 3
    if row_has_timeline(row):
        return 0
    if jira_status == "進行中":
        return 1
    return 2


def sort_rows_for_jira_rank(rows: list[RegisterRow]) -> list[RegisterRow]:
    """依群組 + register_id 排序，供 Jira Rank（越小越上面）。"""
    return sorted(
        rows,
        key=lambda r: (timeline_rank_group(r), r.register_id.upper()),
    )


def apply_jira_rank_order(
    client: AtlassianClient,
    ordered_keys: list[str],
    dry_run: bool,
) -> None:
    """將 issues 依 ordered_keys 順序排 Rank（前者在上）。Agile API 單次最多 50 筆。"""
    keys = [k for k in ordered_keys if is_valid_jira_key(k)]
    if len(keys) < 2:
        return
    if dry_run:
        print(f"[dry-run] Rank {len(keys)} issues: {keys[0]} ... {keys[-1]}")
        return
    head = keys[0]
    rest = keys[1:]
    # 先把第一筆排到第二筆前面，確保 head 在相對頂部
    client.jira_agile_put(
        "/issue/rank",
        {"issues": [head], "rankBeforeIssue": rest[0]},
    )
    for i in range(0, len(rest), 50):
        chunk = rest[i : i + 50]
        anchor = head if i == 0 else rest[i - 1]
        client.jira_agile_put(
            "/issue/rank",
            {"issues": chunk, "rankAfterIssue": anchor},
        )


def reorder_jira_timeline_view(
    client: AtlassianClient,
    rows: list[RegisterRow],
    report: SyncReport,
    dry_run: bool,
) -> None:
    """依 Priority (Epic 群組) 分組後重排 Rank：有時間軸 → 進行中 → 其他 → 完成。"""
    by_group: dict[str, list[RegisterRow]] = {}
    for row in rows:
        if not is_valid_jira_key(row.jira_key):
            continue
        by_group.setdefault(jira_epic_priority(row), []).append(row)

    for label, group in sorted(by_group.items()):
        ordered = sort_rows_for_jira_rank(group)
        ordered_keys = [r.jira_key for r in ordered]
        try:
            apply_jira_rank_order(client, ordered_keys, dry_run)
            report.ranked_issues.extend(ordered_keys)
            print(
                f"Jira Rank ({label}): "
                f"時間軸={sum(1 for r in ordered if timeline_rank_group(r) == 0)}, "
                f"進行中={sum(1 for r in ordered if timeline_rank_group(r) == 1)}, "
                f"其他={sum(1 for r in ordered if timeline_rank_group(r) == 2)}, "
                f"完成={sum(1 for r in ordered if timeline_rank_group(r) == 3)}"
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Jira Rank ({label}): {exc}")


def resolve_default_assignee_account_id(
    client: AtlassianClient, cfg: dict[str, Any]
) -> str:
    """取得預設受託人 accountId（設定檔優先，否則以 displayName 搜尋）。"""
    jira_cfg = cfg.get("jira", {})
    account_id = str(jira_cfg.get("default_assignee_account_id") or "").strip()
    if account_id:
        return account_id
    query = str(jira_cfg.get("default_assignee_query") or "shannon").strip()
    if not query:
        return ""
    users = client.jira_get("/user/search", query=query)
    if not isinstance(users, list):
        return ""
    lowered = query.lower()
    for user in users:
        if not user.get("active", True):
            continue
        display = str(user.get("displayName") or "").lower()
        email = str(user.get("emailAddress") or "").lower()
        if lowered in display or lowered in email:
            return str(user.get("accountId") or "")
    if users:
        return str(users[0].get("accountId") or "")
    return ""


def ensure_default_assignee(
    client: AtlassianClient,
    issue_key: str,
    account_id: str,
    report: SyncReport,
    dry_run: bool,
) -> None:
    """受託人為空時指派預設帳號（Shannon）。"""
    if not account_id or not is_valid_jira_key(issue_key):
        return
    if dry_run:
        print(f"[dry-run] Assign {issue_key} -> {account_id}")
        return
    try:
        data = client.jira_get(f"/issue/{issue_key}", fields="assignee")
        assignee = (data.get("fields") or {}).get("assignee")
        if assignee and assignee.get("accountId"):
            return
        client.jira_put(f"/issue/{issue_key}/assignee", {"accountId": account_id})
        report.assigned_issues.append(issue_key)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"指派受託人 {issue_key}: {exc}")


def assign_unassigned_register_issues(
    client: AtlassianClient,
    rows: list[RegisterRow],
    cfg: dict[str, Any],
    report: SyncReport,
    dry_run: bool,
) -> None:
    """同步後：未指派的 register 任務一律指派給預設受託人。"""
    account_id = resolve_default_assignee_account_id(client, cfg)
    if not account_id:
        report.errors.append("找不到預設受託人（請設 jira.default_assignee_account_id）")
        return
    print(f"預設受託人 accountId: {account_id}")
    for row in rows:
        if is_valid_jira_key(row.jira_key):
            ensure_default_assignee(
                client, row.jira_key, account_id, report, dry_run
            )


def jira_summary(register_id: str, title: str) -> str:
    """Jira 任務名稱 = C 表 ID + Title，以 _ 連接。"""
    safe_title = title.replace("\n", " ").strip()
    return f"{register_id}_{safe_title}"[:255]


def text_to_adf(text: str) -> dict[str, Any]:
    """將純文字轉為 Jira Atlassian Document Format (ADF)。"""
    paragraphs: list[dict[str, Any]] = []
    for block in text.splitlines():
        block = block.strip()
        if not block:
            continue
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": block}],
            }
        )
    if not paragraphs:
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": ""}],
            }
        )
    return {"type": "doc", "version": 1, "content": paragraphs}


def build_jira_description(row: RegisterRow) -> dict[str, Any]:
    lines = [f"Register ID: {row.register_id}"]
    if row.title:
        lines.append(f"Title: {row.title}")
    if row.description:
        lines.append(row.description)
    return text_to_adf("\n".join(lines))


def build_confluence_page_url(cfg: dict[str, Any], page: dict[str, Any] | None = None) -> str:
    """組出 mail_checking 等 Confluence 頁面 URL（供 Jira Web 連結）。"""
    if page:
        links = page.get("_links") or {}
        base = str(links.get("base", "")).rstrip("/")
        webui = links.get("webui", "")
        if base and webui:
            return f"{base}{webui}".split("#")[0]
        web_url = page.get("webUrl")
        if isinstance(web_url, str) and web_url.startswith("http"):
            return web_url.split("#")[0]

    site = cfg["atlassian"]["site_url"].rstrip("/")
    conf = cfg["confluence"]
    page_id = conf["page_id"]
    title = conf.get("page_title", "mail_checking")
    space = conf.get("space_key", "projectmod")
    return f"{site}/wiki/spaces/{space}/pages/{page_id}/{title}"


def ensure_confluence_web_link(
    client: AtlassianClient,
    issue_key: str,
    page_url: str,
    link_title: str,
    report: SyncReport,
    dry_run: bool,
    *,
    page_id: str = "",
) -> None:
    """在 Jira issue 新增指向 Confluence C 表的 Web 連結（remotelink）。"""
    if dry_run:
        print(f"[dry-run] Web 連結 {issue_key} -> {link_title}: {page_url}")
        return
    try:
        existing = client.jira_get(f"/issue/{issue_key}/remotelink")
        links = existing if isinstance(existing, list) else []
        stale_ids: list[int | str] = []
        has_correct = False
        for link in links:
            obj = link.get("object") or {}
            url = obj.get("url") or ""
            title = obj.get("title") or ""
            if url == page_url:
                if title == link_title:
                    has_correct = True
                else:
                    link_id = link.get("id")
                    if link_id is not None:
                        stale_ids.append(link_id)
                continue
            if title == link_title or (page_id and page_id in url):
                link_id = link.get("id")
                if link_id is not None:
                    stale_ids.append(link_id)
        for link_id in stale_ids:
            client.jira_delete(f"/issue/{issue_key}/remotelink/{link_id}")
        if has_correct:
            return
        client.jira_post(
            f"/issue/{issue_key}/remotelink",
            {"object": {"url": page_url, "title": link_title}},
        )
        report.web_links_added.append(issue_key)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"Web 連結 {issue_key}: {exc}")


def parse_register_id_from_summary(summary: str) -> str | None:
    if "_" not in summary:
        return None
    prefix = summary.split("_", 1)[0].strip().upper()
    if REGISTER_ID_RE.match(prefix):
        return prefix
    return None


def _paginate_jira_search(
    client: AtlassianClient,
    jql: str,
    *,
    fields: list[str],
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """分頁蒐集 JQL 搜尋結果。"""
    collected: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        data = client.jira_search(
            jql,
            max_results=max_results,
            fields=fields,
            next_page_token=next_token,
        )
        issues = data.get("issues", [])
        collected.extend(issues)
        if data.get("isLast", True) or not issues:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return collected


def load_jira_register_index(
    client: AtlassianClient, project_key: str, task_type: str
) -> dict[str, dict[str, Any]]:
    """register_id -> {key, status, parent}"""
    index: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for type_name in jql_issuetype_name_candidates(task_type):
        jql = f"project = {project_key} AND {jql_issuetype_equals(type_name)}"
        issues = _paginate_jira_search(
            client, jql, fields=["summary", "status", "parent"]
        )
        if issues:
            break
    for issue in issues:
        rid = parse_register_id_from_summary(issue["fields"]["summary"])
        if rid:
            parent = issue["fields"].get("parent") or {}
            index[rid] = {
                "key": issue["key"],
                "status": issue["fields"]["status"]["name"],
                "parent_key": parent.get("key", ""),
            }
    return index


def load_epic_map(
    client: AtlassianClient, project_key: str, epic_type: str
) -> dict[str, str]:
    """priority label (P1) -> epic key"""
    mapping: dict[str, str] = {}
    issues: list[dict[str, Any]] = []
    for type_name in jql_issuetype_name_candidates(epic_type):
        jql = f"project = {project_key} AND {jql_issuetype_equals(type_name)}"
        issues = _paginate_jira_search(client, jql, fields=["summary"])
        if issues:
            break
    for issue in issues:
        summary = issue["fields"]["summary"].strip()
        mapping[summary] = issue["key"]
    return mapping


def find_jira_key_by_register_id(
    client: AtlassianClient,
    project_key: str,
    task_type: str,
    register_id: str,
) -> str:
    """以 summary 前綴搜尋 Jira 任務（補 issue_index 遺漏）。"""
    for type_name in jql_issuetype_name_candidates(task_type):
        jql = (
            f"project = {project_key} AND {jql_issuetype_equals(type_name)} "
            f'AND summary ~ "{register_id}"'
        )
        try:
            data = client.jira_search(jql, max_results=20, fields=["summary"])
        except Exception:
            continue
        for issue in data.get("issues", []):
            rid = parse_register_id_from_summary(issue["fields"]["summary"])
            if rid == register_id:
                return issue["key"]
    return ""


def ensure_epic(
    client: AtlassianClient,
    project_key: str,
    epic_type: str,
    priority: str,
    epic_map: dict[str, str],
    report: SyncReport,
    dry_run: bool,
) -> str:
    if priority in epic_map:
        return epic_map[priority]
    if dry_run:
        fake = f"PMWC-NEW-{priority}"
        print(f"[dry-run] Would create epic: {priority} -> {fake}")
        epic_map[priority] = fake
        report.created_epics.append(priority)
        return fake
    created = client.jira_post(
        "/issue",
        {
            "fields": {
                "project": {"key": project_key},
                "issuetype": {"name": epic_type},
                "summary": priority,
            }
        },
    )
    key = created["key"]
    epic_map[priority] = key
    report.created_epics.append(f"{priority} ({key})")
    return key


def get_transition_id(client: AtlassianClient, issue_key: str, target_status: str) -> str | None:
    target = normalize_jira_status_name(target_status)
    data = client.jira_get(f"/issue/{issue_key}/transitions")
    for tr in data.get("transitions", []):
        to_name = tr.get("to", {}).get("name", "")
        if to_name == target:
            return tr["id"]
        # 簡繁／大小寫相容
        if normalize_jira_status_name(to_name) == target:
            return tr["id"]
    return None


def transition_issue(
    client: AtlassianClient,
    issue_key: str,
    target_status: str,
    dry_run: bool,
) -> None:
    if dry_run:
        print(f"[dry-run] Transition {issue_key} -> {target_status}")
        return
    tr_id = get_transition_id(client, issue_key, target_status)
    if not tr_id:
        raise RuntimeError(
            f"無法將 {issue_key} 轉換到狀態 {target_status!r}（找不到 transition）"
        )
    client.jira_post(f"/issue/{issue_key}/transitions", {"transition": {"id": tr_id}})


def delete_jira_issue(
    client: AtlassianClient, issue_key: str, dry_run: bool
) -> None:
    """刪除 C 表已不存在的 Jira 任務。"""
    if dry_run:
        print(f"[dry-run] Delete Jira issue {issue_key}")
        return
    client.jira_delete(f"/issue/{issue_key}?deleteSubtasks=false")


def sync_jira_for_rows(
    client: AtlassianClient,
    rows: list[RegisterRow],
    cfg: dict[str, Any],
    existing_jira_map: dict[str, str],
    report: SyncReport,
    dry_run: bool,
    sync_date: str,
    confluence_page_url: str,
    web_link_title: str,
) -> dict[str, str]:
    project = cfg["jira"]["project_key"]
    epic_type = cfg["jira"]["epic_issue_type"]
    task_type = cfg["jira"]["task_issue_type"]

    epic_map = load_epic_map(client, project, epic_type)
    issue_index = load_jira_register_index(client, project, task_type)

    current_ids = {r.register_id for r in rows}
    id_to_key: dict[str, str] = dict(existing_jira_map)

    # 刪除 C 表已不存在的項目（Jira 與 C 表保持一致）
    for rid, info in issue_index.items():
        if rid not in current_ids:
            if dry_run:
                print(f"[dry-run] Delete removed register {rid} ({info['key']})")
                report.deleted_issues.append(info["key"])
            else:
                try:
                    delete_jira_issue(client, info["key"], dry_run=False)
                    report.deleted_issues.append(info["key"])
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"刪除 {info['key']}: {exc}")

    for row in rows:
        priority = jira_epic_priority(row)
        epic_key = ensure_epic(
            client, project, epic_type, priority, epic_map, report, dry_run
        )
        target_status = map_status_to_jira(jira_status_source(row))
        summary = jira_summary(row.register_id, row.title)
        description = build_jira_description(row)

        existing = issue_index.get(row.register_id)
        known_key = id_to_key.get(row.register_id) or (
            existing["key"] if existing else ""
        )
        if not known_key:
            known_key = find_jira_key_by_register_id(
                client, project, task_type, row.register_id
            )

        timeline_fields = build_timeline_fields(row, sync_date, cfg)

        if not known_key:
            if dry_run:
                fake_key = f"PMWC-NEW-{row.register_id}"
                timeline_msg = (
                    f", timeline {timeline_fields}" if timeline_fields else ""
                )
                print(
                    f"[dry-run] Create {row.register_id} under {epic_key} "
                    f"-> {fake_key}{timeline_msg}"
                )
                id_to_key[row.register_id] = fake_key
                report.created_issues.append(fake_key)
                row.jira_key = fake_key
                ensure_confluence_web_link(
                    client,
                    fake_key,
                    confluence_page_url,
                    web_link_title,
                    report,
                    dry_run,
                    page_id=str(cfg["confluence"]["page_id"]),
                )
                continue
            create_fields: dict[str, Any] = {
                "project": {"key": project},
                "issuetype": {"name": task_type},
                "summary": summary,
                "description": description,
                "parent": {"key": epic_key},
            }
            create_fields.update(timeline_fields)
            try:
                created = client.jira_post("/issue", {"fields": create_fields})
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"建立 {row.register_id}: {exc}")
                continue
            key = created["key"]
            id_to_key[row.register_id] = key
            row.jira_key = key
            report.created_issues.append(key)
            issue_index[row.register_id] = {
                "key": key,
                "status": DEFAULT_JIRA_STATUS,
                "parent_key": epic_key,
            }
            if target_status != DEFAULT_JIRA_STATUS:
                try:
                    transition_issue(client, key, target_status, dry_run=False)
                    issue_index[row.register_id]["status"] = target_status
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"設定狀態 {key}: {exc}")
            ensure_confluence_web_link(
                client,
                key,
                confluence_page_url,
                web_link_title,
                report,
                dry_run,
                page_id=str(cfg["confluence"]["page_id"]),
            )
            continue

        if not is_valid_jira_key(known_key):
            known_key = find_jira_key_by_register_id(
                client, project, task_type, row.register_id
            )
        if not known_key:
            report.errors.append(f"{row.register_id}: 找不到有效 JIRA key")
            continue

        row.jira_key = known_key
        id_to_key[row.register_id] = known_key
        fields: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "parent": {"key": epic_key},
        }
        fields.update(timeline_fields)
        if dry_run:
            timeline_msg = f", timeline={timeline_fields}" if timeline_fields else ""
            print(
                f"[dry-run] Update {known_key}: parent={epic_key}, "
                f"status->{target_status}{timeline_msg}"
            )
        else:
            try:
                client.jira_put(f"/issue/{known_key}", {"fields": fields})
                report.updated_issues.append(known_key)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"更新 {known_key}: {exc}")

            current_status = issue_index.get(row.register_id, {}).get("status", "")
            if current_status != target_status:
                try:
                    transition_issue(client, known_key, target_status, dry_run=False)
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"轉換狀態 {known_key}: {exc}")

        ensure_confluence_web_link(
            client,
            known_key,
            confluence_page_url,
            web_link_title,
            report,
            dry_run,
            page_id=str(cfg["confluence"]["page_id"]),
        )

    return id_to_key


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_sync(config_path: Path, dry_run: bool) -> SyncReport:
    cfg = load_config(config_path)
    report = SyncReport()

    cache_dir = SCRIPT_DIR / ".register_cache"
    snapshot_path = cache_dir / "last_snapshot.json"
    log_dir = SCRIPT_DIR / "logs"

    old_snapshot, snapshot_saved_at = load_last_snapshot(snapshot_path)

    xlsx = download_sharepoint_excel(cfg["sharepoint"]["download_url"], cache_dir)
    print(f"已下載 S 表格: {xlsx} ({xlsx.stat().st_size} bytes)")

    s_columns, s_rows = load_register_rows_from_excel(xlsx, cfg["sharepoint"]["sheet_name"])
    print(f"S 表格欄位 ({len(s_columns)}): {', '.join(s_columns)}")
    print(f"S 表格有效項目: {len(s_rows)}")
    if not s_rows:
        raise SystemExit("S 表格沒有可同步的項目。")

    register_diff = compute_register_diff(
        old_snapshot, s_rows, snapshot_saved_at=snapshot_saved_at
    )

    client = AtlassianClient(
        cfg["atlassian"]["site_url"],
        cfg["atlassian"]["cloud_id"],
        cfg["atlassian"]["email"],
        cfg["_api_token"],
    )

    page_id = str(cfg["confluence"]["page_id"])
    page = fetch_confluence_page(client, page_id)
    body = extract_confluence_body_text(page)
    existing_jira_map = parse_jira_map_from_confluence_body(body)
    print(f"從 C 表格讀到 {len(existing_jira_map)} 筆 JIRA 對照")

    confluence_page_url = build_confluence_page_url(cfg, page)
    web_link_title = (
        cfg["confluence"].get("web_link_title")
        or cfg["confluence"].get("page_title", "mail_checking")
    )
    print(f"Jira Web 連結目標: {web_link_title} ({confluence_page_url})")

    sync_date = datetime.now().strftime("%Y-%m-%d")
    print(f"同步執行日（無 Opened 時的 fallback）: {sync_date}")

    id_to_key = sync_jira_for_rows(
        client,
        s_rows,
        cfg,
        existing_jira_map,
        report,
        dry_run,
        sync_date,
        confluence_page_url,
        web_link_title,
    )

    site_url = cfg["atlassian"]["site_url"]
    source_link_url, source_link_title = resolve_sharepoint_source_link(cfg)
    sync_action_url, sync_action_title = resolve_sync_action_link(cfg)
    if source_link_url:
        print(f"S 表連結: {source_link_title} ({source_link_url[:60]}...)")
    if sync_action_url:
        print(f"立即同步連結: {sync_action_title} ({sync_action_url})")
    for row in s_rows:
        key = row.jira_key or id_to_key.get(row.register_id, "")
        if not is_valid_jira_key(key):
            key = find_jira_key_by_register_id(
                client,
                cfg["jira"]["project_key"],
                cfg["jira"]["task_issue_type"],
                row.register_id,
            )
        if is_valid_jira_key(key):
            row.jira_key = key
            id_to_key[row.register_id] = key

    reorder_jira_timeline_view(client, s_rows, report, dry_run)
    assign_unassigned_register_issues(client, s_rows, cfg, report, dry_run)

    missing_jira = [
        row.register_id
        for row in s_rows
        if not is_valid_jira_key(row.jira_key)
    ]
    if missing_jira and not dry_run:
        msg = f"下列項目缺少 JIRA key: {', '.join(missing_jira)}"
        report.errors.append(msg)
        print(f"警告: {msg}")
        print("Confluence 未更新（需先補齊 JIRA）。")
        diff_text = print_diff_report(register_diff, dry_run=dry_run)
        report.diff_log_path = write_sync_log(log_dir, register_diff, diff_text, report)
        print(f"變更紀錄已寫入: {report.diff_log_path}")
        return report

    page_body = build_confluence_page_html(
        s_rows,
        site_url,
        s_columns,
        source_link_url=source_link_url,
        source_link_title=source_link_title,
        sync_action_url=sync_action_url,
        sync_action_title=sync_action_title,
    )
    print(f"C 表格將寫入 {len(s_rows)} 列 × {len(c_columns_for(s_columns))} 欄（無空白列）")
    if source_link_url:
        print("C 表頂部已加入 S 表格連結")
    if sync_action_url:
        print("C 表頂部已加入「立即同步」連結")
    report.confluence_updated = update_confluence_page(
        client,
        page_id,
        cfg["confluence"].get("page_title", page.get("title", "mail_checking")),
        page_body,
        cfg["confluence"].get("version_message", "Synced from SharePoint register"),
        dry_run,
    )

    diff_text = print_diff_report(register_diff, dry_run=dry_run)
    report.diff_log_path = write_sync_log(log_dir, register_diff, diff_text, report)
    print(f"變更紀錄已寫入: {report.diff_log_path}")

    if not dry_run and report.confluence_updated:
        save_last_snapshot(snapshot_path, s_rows)
        print(f"已更新快照: {snapshot_path}")

    return report


def print_report(report: SyncReport) -> None:
    print("\n========== 同步結果 ==========")
    if report.created_epics:
        print(f"新建 Epic: {', '.join(report.created_epics)}")
    if report.created_issues:
        print(f"新建任務: {', '.join(report.created_issues)}")
    if report.updated_issues:
        print(f"更新任務: {', '.join(report.updated_issues)}")
    if report.deleted_issues:
        print(f"刪除任務: {', '.join(report.deleted_issues)}")
    if report.web_links_added:
        print(f"新增 Web 連結: {', '.join(report.web_links_added)}")
    if report.ranked_issues:
        print(f"重排 Rank: {len(report.ranked_issues)} 筆")
    if report.assigned_issues:
        print(f"指派受託人: {', '.join(report.assigned_issues)}")
    print(f"Confluence 已更新: {'是' if report.confluence_updated else '否（dry-run 或未變更）'}")
    if report.diff_log_path:
        print(f"變更紀錄: {report.diff_log_path}")
    if report.errors:
        print("\n錯誤:")
        for err in report.errors:
            print(f"  - {err}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SharePoint Register -> Confluence -> Jira 同步"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=SCRIPT_DIR / "config.yaml",
        help="設定檔路徑（預設 ./config.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="預覽變更，不寫入 Confluence / Jira",
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(f"找不到設定檔 {args.config}")
        print("請複製 config.example.yaml 為 config.yaml 並填入帳號資訊。")
        sys.exit(1)

    try:
        report = run_sync(args.config, dry_run=args.dry_run)
        print_report(report)
    except Exception as exc:  # noqa: BLE001
        print(f"同步失敗: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
