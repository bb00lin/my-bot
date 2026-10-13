"""排程核心：工作日曆、MS Project 式前置任務解析、CPM 前推排程、三方合併（Sheet ↔ Jira）。

純函式、無網路，供單元測試直接使用。
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

# 計畫 ID：PLN-<群組>-<序號>。兩段連字號，故 summary 前綴不會符合
# Register 的 ^[A-Z][A-Z0-9]*-\d+$，Register 同步不會把計畫單當成自己的而刪除。
PLAN_ID_RE = re.compile(r"^PLN-[A-Z][A-Z0-9]*-\d{2,3}$")
REGISTER_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$", re.IGNORECASE)

REL_TYPES = ("FS", "SS", "FF", "SF")
_SPLIT_RE = re.compile(r"[;,，；\n]+")
_REMAINDER_RE = re.compile(
    r"^\s*(?P<kind>FS|SS|FF|SF)?\s*"
    r"(?:(?P<sign>[+-])\s*(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>d|day|days|wd|w|wk|wks|week|weeks|工作天|天|週)?)?\s*$",
    re.IGNORECASE,
)
_GENERIC_KIND_RE = re.compile(r"^(?P<id>.+?)(?:FS|SS|FF|SF)(?:\s*[+-].*)?$", re.IGNORECASE)
_GENERIC_LAG_RE = re.compile(
    r"^(?P<id>.+?)\s*[+-]\s*\d+(?:\.\d+)?\s*(?:d|day|days|wd|w|wk|wks|week|weeks|工作天|天|週)\s*$", re.IGNORECASE)


class PlanError(Exception):
    """排程無法計算（循環相依、ID 重複等）。"""


# ---------------------------------------------------------------------------
# Working-day calendar
# ---------------------------------------------------------------------------


class WorkCalendar:
    """工作日索引：index 0 = 專案開始日（若非工作日則順延到下一個工作日）。"""

    def __init__(self, start: date, workdays: Iterable[int] = (0, 1, 2, 3, 4), holidays: Iterable[date] = ()):
        self.workdays = set(workdays)
        if not self.workdays:
            raise PlanError("工作日設定為空（Working Days）")
        self.holidays = set(holidays)
        self.base = self._roll_forward(start)
        self._fwd: list[date] = [self.base]
        self._back: list[date] = []  # _back[i] = index -(i+1)

    def is_workday(self, d: date) -> bool:
        return d.weekday() in self.workdays and d not in self.holidays

    def _roll_forward(self, d: date) -> date:
        for _ in range(3700):
            if self.is_workday(d):
                return d
            d += timedelta(days=1)
        raise PlanError("十年內找不到工作日，請檢查 Working Days / Holidays")

    def _extend_fwd(self, idx: int) -> None:
        while len(self._fwd) <= idx:
            d = self._fwd[-1] + timedelta(days=1)
            self._fwd.append(self._roll_forward(d))

    def _extend_back(self, n: int) -> None:
        while len(self._back) < n:
            d = (self._back[-1] if self._back else self.base) - timedelta(days=1)
            while not self.is_workday(d):
                d -= timedelta(days=1)
            self._back.append(d)

    def date_of(self, idx: int) -> date:
        if idx >= 0:
            self._extend_fwd(idx)
            return self._fwd[idx]
        self._extend_back(-idx)
        return self._back[-idx - 1]

    def index_of(self, d: date) -> int:
        """日期 → 工作日索引；非工作日順延到下一個工作日。"""
        d = self._roll_forward(d)
        if d >= self.base:
            while self._fwd[-1] < d:
                self._extend_fwd(len(self._fwd))
            return bisect.bisect_left(self._fwd, d)
        n = 0
        while True:
            n += 1
            self._extend_back(n)
            if self._back[n - 1] == d:
                return -n

    def workdays_between(self, start: date, finish: date) -> int:
        """含頭含尾的工作天數（finish < start 回 0）。"""
        if finish < start:
            return 0
        return self.index_of(finish + timedelta(days=1)) - self.index_of(start)


# ---------------------------------------------------------------------------
# Predecessors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pred:
    id: str
    kind: str = "FS"
    lag: int = 0

    def format(self) -> str:
        if self.kind == "FS" and self.lag == 0:
            return self.id
        lag = f"{self.lag:+d}d" if self.lag else ""
        return f"{self.id}{self.kind}{lag}"


def _parse_remainder(rest: str) -> tuple[str, int] | None:
    m = _REMAINDER_RE.match(rest or "")
    if not m:
        return None
    kind = (m.group("kind") or "FS").upper()
    lag = 0
    if m.group("num"):
        num = float(m.group("num"))
        unit = (m.group("unit") or "d").lower()
        if unit in ("w", "wk", "wks", "week", "weeks", "週"):
            num *= 5
        if abs(num - round(num)) > 1e-9:
            return None
        lag = int(round(num)) * (-1 if m.group("sign") == "-" else 1)
    return kind, lag


def parse_predecessors(text: str, known_ids: Iterable[str] | None = None) -> tuple[list[Pred], list[str]]:
    """解析 `PLN-AT-01FS+1d; PLN-FX-02SS, PLN-WZ-03FF-2d`。

    - 類型省略 = FS；lag 單位 d（工作天）或 w（=5d），可正可負。
    - 給 known_ids 時以「最長已知 ID 前綴」比對，避免 ID 內連字號與負 lag 混淆。
    回傳 (preds, errors)。
    """
    preds: list[Pred] = []
    errors: list[str] = []
    known = sorted({k.upper(): k for k in (known_ids or [])}.items(), key=lambda kv: -len(kv[0]))
    for raw in _SPLIT_RE.split(text or ""):
        token = raw.strip()
        if not token:
            continue
        pid, rest = "", ""
        if known:
            up = token.upper()
            for k_up, k in known:
                if up.startswith(k_up):
                    remainder = token[len(k_up):]
                    if _parse_remainder(remainder) is not None:
                        pid, rest = k, remainder
                        break
            if not pid:
                errors.append(f"未知的前置任務或格式錯誤：{token!r}")
                continue
        else:
            # 沒有已知 ID 時：lag 必須接在類型之後（A-1FS+2d），或帶單位（A-1 -2d）
            m = _GENERIC_KIND_RE.match(token) or _GENERIC_LAG_RE.match(token)
            pid, rest = (m.group("id").strip(), token[m.end("id"):]) if m else (token, "")
            if _parse_remainder(rest) is None:
                pid, rest = token, ""
        parsed = _parse_remainder(rest)
        if parsed is None:
            errors.append(f"前置任務格式錯誤：{token!r}（支援 FS/SS/FF/SF 與 ±Nd / ±Nw）")
            continue
        kind, lag = parsed
        pred = Pred(pid.upper() if not known else pid, kind, lag)
        if pred not in preds:
            preds.append(pred)
    return preds, errors


def format_predecessors(preds: Iterable[Pred]) -> str:
    return "; ".join(p.format() for p in preds)


# ---------------------------------------------------------------------------
# Value normalisation
# ---------------------------------------------------------------------------

STATUS_CHOICES = ["Not Started", "In Progress", "Blocked", "Done", "Cancelled"]
TYPE_CHOICES = ["Task", "Milestone", "Procurement"]
PRIORITY_CHOICES = ["P1", "P2", "P3"]
SYNC_CHOICES = ["Y", "N"]

_STATUS_ALIASES = {
    "not started": "Not Started", "未開始": "Not Started", "todo": "Not Started", "to do": "Not Started",
    "待辦": "Not Started", "待辦事項": "Not Started", "open": "Not Started", "": "Not Started",
    "in progress": "In Progress", "進行中": "In Progress", "doing": "In Progress", "wip": "In Progress",
    "blocked": "Blocked", "卡關": "Blocked", "on hold": "Blocked", "暫停": "Blocked",
    "done": "Done", "完成": "Done", "closed": "Done", "completed": "Done", "已完成": "Done",
    "cancelled": "Cancelled", "canceled": "Cancelled", "取消": "Cancelled",
}
# Sheet 狀態 → Jira 狀態（比較時都換成 Jira 狀態再比，Blocked/Cancelled 視同進行中/完成）
SHEET_TO_JIRA_STATUS = {
    "Not Started": "待辦事項", "In Progress": "進行中", "Blocked": "進行中",
    "Done": "完成", "Cancelled": "完成",
}
JIRA_TO_SHEET_STATUS = {"待辦事項": "Not Started", "進行中": "In Progress", "完成": "Done"}
_JIRA_STATUS_ALIASES = {"to do": "待辦事項", "in progress": "進行中", "done": "完成"}


def norm_status(value: str) -> str:
    v = (value or "").strip()
    return _STATUS_ALIASES.get(v.casefold(), v if v in STATUS_CHOICES else "Not Started")


def norm_jira_status(value: str) -> str:
    v = (value or "").strip()
    return _JIRA_STATUS_ALIASES.get(v.casefold(), v)


def norm_type(value: str, duration: int | None = None) -> str:
    v = (value or "").strip().casefold()
    if v in ("milestone", "里程碑", "ms") or duration == 0:
        return "Milestone"
    if v in ("procurement", "採購", "purchase"):
        return "Procurement"
    return "Task"


def norm_priority(value: str) -> str:
    v = (value or "").strip().casefold()
    mapping = {"p1": "P1", "high": "P1", "高": "P1", "p2": "P2", "medium": "P2", "med": "P2", "中": "P2",
               "p3": "P3", "low": "P3", "低": "P3"}
    return mapping.get(v, "P2" if not v else (value or "").strip().upper())


def norm_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in ("y", "yes", "true", "1", "是", "v", "✓", "✔", "x")


def norm_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines).strip()


def parse_date_any(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < float(value) < 80000:
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value).strip()
    if not text:
        return None
    text = text.split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if re.fullmatch(r"\d{5}(\.\d+)?", text):
        return parse_date_any(float(text))
    return None


def parse_constraint(text: str) -> tuple[str, date | None]:
    """'2026-11-02' / 'SNET 2026-11-02' → ('SNET', date)；'MSO 2026-11-02' → 必須開始於。"""
    raw = (text or "").strip()
    if not raw:
        return "", None
    kind = "SNET"
    up = raw.upper()
    for prefix, k in (("SNET", "SNET"), ("MSO", "MSO"), ("不早於", "SNET"), ("指定", "MSO")):
        if up.startswith(prefix):
            kind, raw = k, raw[len(prefix):].strip(" :：")
            break
    d = parse_date_any(raw)
    return (kind, d) if d else ("ERR", None)


# ---------------------------------------------------------------------------
# Task model & scheduler
# ---------------------------------------------------------------------------


@dataclass
class PlanTask:
    id: str
    name: str = ""
    group: str = ""
    type: str = "Task"
    duration: int = 1
    preds_text: str = ""
    constraint_text: str = ""
    priority: str = "P2"
    owner: str = ""
    status: str = "Not Started"
    sync: bool = False
    jira_key: str = ""
    description: str = ""
    discussion: str = ""
    estimate: bool = False
    row: int = 0  # 來源列號（1-based；0 = 新列）
    cells: dict[str, Any] = field(default_factory=dict)
    preds: list[Pred] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    start_idx: int = 0
    end_idx: int = 0
    start: date | None = None
    finish: date | None = None

    @property
    def is_milestone(self) -> bool:
        return self.duration == 0


def resolve_preds(tasks: list[PlanTask]) -> None:
    ids = [t.id for t in tasks]
    for t in tasks:
        t.preds, errs = parse_predecessors(t.preds_text, ids)
        t.errors = [e for e in t.errors if not e.startswith("前置")] + [f"前置任務：{e}" for e in errs]
        if any(p.id == t.id for p in t.preds):
            t.preds = [p for p in t.preds if p.id != t.id]
            t.errors.append("前置任務：不可引用自己")


def find_cycle(tasks: list[PlanTask]) -> list[str] | None:
    graph = {t.id: [p.id for p in t.preds] for t in tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for m in graph.get(n, []):
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if m not in state:
                found = dfs(m)
                if found:
                    return found
        stack.pop()
        state[n] = 2
        return None

    for t in tasks:
        if t.id not in state:
            found = dfs(t.id)
            if found:
                # dfs 走的是「後繼 → 前置」，反轉成前置 → 後繼較好讀
                return list(reversed(found))
    return None


def compute_schedule(tasks: list[PlanTask], cal: WorkCalendar) -> list[str]:
    """CPM 前推。回傳警告；循環相依 / ID 重複時 raise PlanError。"""
    seen: dict[str, int] = {}
    for t in tasks:
        seen[t.id] = seen.get(t.id, 0) + 1
    dups = sorted(k for k, n in seen.items() if n > 1)
    if dups:
        raise PlanError(f"ID 重複：{', '.join(dups)}")
    cycle = find_cycle(tasks)
    if cycle:
        raise PlanError("偵測到循環相依：" + " → ".join(cycle))

    by_id = {t.id: t for t in tasks}
    indeg = {t.id: 0 for t in tasks}
    succs: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for p in t.preds:
            indeg[t.id] += 1
            succs[p.id].append(t.id)
    order: list[str] = []
    queue = [t.id for t in tasks if indeg[t.id] == 0]
    while queue:
        n = queue.pop(0)
        order.append(n)
        for s in succs[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)

    warnings: list[str] = []
    for tid in order:
        t = by_id[tid]
        dur = max(0, int(t.duration))
        lower = 0
        pred_bound: int | None = None
        shift = False
        for p in t.preds:
            P = by_id[p.id]
            if p.kind == "FS":
                b = P.end_idx + p.lag
            elif p.kind == "SS":
                b = P.start_idx + p.lag
            elif p.kind == "FF":
                b = P.end_idx + p.lag - dur
            else:  # SF
                b = P.start_idx + p.lag - dur
            if pred_bound is None or b > pred_bound:
                pred_bound, shift = b, p.kind in ("FS", "FF")
            elif b == pred_bound and p.kind in ("FS", "FF"):
                shift = True
        kind, cdate = parse_constraint(t.constraint_text)
        if kind == "ERR":
            t.errors.append(f"Start Constraint 無法解析：{t.constraint_text!r}")
        cidx = cal.index_of(cdate) if cdate else None
        if kind == "MSO" and cidx is not None:
            start = cidx
            driven = False
            if pred_bound is not None and pred_bound > cidx:
                warnings.append(f"{t.id}: MSO {cdate} 早於前置任務允許的日期（違反相依）")
        else:
            if kind == "SNET" and cidx is not None:
                lower = max(lower, cidx)
            start = lower if pred_bound is None else max(lower, pred_bound)
            driven = pred_bound is not None and pred_bound >= lower and pred_bound == start
            if start < 0:
                start = 0
        t.start_idx, t.end_idx = start, start + dur
        if dur > 0:
            t.start, t.finish = cal.date_of(start), cal.date_of(start + dur - 1)
        else:
            disp = start - 1 if (driven and shift and start > 0) else start
            t.start = t.finish = cal.date_of(disp)
    return warnings


def validate_group_totals(tasks: list[PlanTask], locked: dict[str, int]) -> list[tuple[str, int, int, bool]]:
    """鎖定群組（如 AT=25）只加總 Type=Task、非 Cancelled 的工期。"""
    out = []
    for code, expected in locked.items():
        actual = sum(
            t.duration for t in tasks
            if t.group == code and t.type == "Task" and t.status != "Cancelled"
        )
        out.append((code, expected, actual, actual == expected))
    return out


# ---------------------------------------------------------------------------
# Three-way merge (Sheet ↔ Jira, base = last-sync snapshot)
# ---------------------------------------------------------------------------

MERGE_FIELDS = ["name", "description", "priority", "group", "status", "owner", "start", "finish", "preds"]
FIELD_MASTER = {
    "name": "sheet", "description": "sheet", "priority": "sheet", "group": "sheet", "preds": "sheet",
    "start": "sheet", "finish": "sheet", "status": "jira", "owner": "jira",
}
FIELD_LABELS = {
    "name": "Task Name", "description": "Description", "priority": "Priority", "group": "Group",
    "status": "Status", "owner": "Owner", "start": "Start", "finish": "Finish", "preds": "Predecessors",
}


def merge_scalar(field_name: str, sheet: str, jira: str, base: str | None, resolution: str = "") -> str:
    """回傳 same | push（Sheet→Jira）| pull（Jira→Sheet）| conflict。"""
    if sheet == jira:
        return "same"
    if resolution == "sheet":
        return "push"
    if resolution == "jira":
        return "pull"
    if base is None:
        return "push" if FIELD_MASTER.get(field_name, "sheet") == "sheet" else "pull"
    if sheet == base:
        return "pull"
    if jira == base:
        return "push"
    return "conflict"


def merge_sets(sheet: set[str], jira: set[str], base: set[str] | None) -> tuple[set[str], set[str], set[str]]:
    """前置任務集合三方合併：回傳 (合併結果, Sheet 單邊變更後的集合, Jira 單邊變更後的集合)。

    沒有快照時取聯集（不刪任何一邊既有的相依）。
    """
    if base is None:
        union = sheet | jira
        return union, union, union
    add_s, rem_s = sheet - base, base - sheet
    add_j, rem_j = jira - base, base - jira
    result = (base | add_s | add_j) - rem_s - rem_j
    after_push = (base | add_s) - rem_s  # 只套用 Sheet 的變更
    after_pull = (base | add_j) - rem_j  # 只套用 Jira 的變更
    return result, after_push, after_pull


def parse_resolution(conflict_cell: str) -> str:
    v = (conflict_cell or "").strip().casefold()
    if v.startswith("use sheet") or v.startswith("以sheet") or v.startswith("以 sheet"):
        return "sheet"
    if v.startswith("use jira") or v.startswith("以jira") or v.startswith("以 jira"):
        return "jira"
    return ""


# ---------------------------------------------------------------------------
# Jira description (ADF) helpers
# ---------------------------------------------------------------------------

DESC_MARKER = "［計畫同步資訊］"


def build_description_adf(description: str, footer_lines: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for line in norm_text(description).split("\n") if norm_text(description) else []:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []})
    content.append({"type": "rule"})
    content.append({"type": "paragraph", "content": [{"type": "text", "text": DESC_MARKER, "marks": [{"type": "strong"}]}]})
    for line in footer_lines:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    return {"type": "doc", "version": 1, "content": content}


def _adf_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        if node.get("type") == "hardBreak":
            return "\n"
        return "".join(_adf_text(c) for c in node.get("content", []) or [])
    if isinstance(node, list):
        return "".join(_adf_text(c) for c in node)
    return ""


def description_from_adf(adf: Any) -> str:
    """取 marker 以上的使用者內容（比較 / 拉回 Sheet 用）。"""
    if not isinstance(adf, dict):
        return norm_text(adf if isinstance(adf, str) else "")
    blocks = adf.get("content", []) or []
    lines: list[str] = []
    for b in blocks:
        text = _adf_text(b)
        if text.strip().startswith(DESC_MARKER):
            break
        if b.get("type") == "rule":
            continue
        lines.append(text)
    return norm_text("\n".join(lines))
