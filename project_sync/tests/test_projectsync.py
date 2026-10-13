"""ProjectSync 單元測試（不連網路）：python -m unittest discover -s tests -v"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projectsync import plan_core as pc  # noqa: E402
from projectsync import plan_store as ps  # noqa: E402
from projectsync import plan_sync  # noqa: E402
from projectsync import profiles  # noqa: E402
from projectsync import register_sync as sr  # noqa: E402
from projectsync import plan_seed_c27 as seed  # noqa: E402


def T(tid, dur, preds="", cons="", **kw):
    return pc.PlanTask(id=tid, name=tid, duration=dur, preds_text=preds, constraint_text=cons, **kw)


class PredecessorParserTest(unittest.TestCase):
    KNOWN = ["PLN-AT-01", "PLN-FX-02", "PLN-WZ-03", "PLN-AT-010"]

    def test_types_lags_defaults_multiple(self):
        preds, errs = pc.parse_predecessors("PLN-AT-01FS+1d; PLN-FX-02SS, PLN-WZ-03FF-2d", self.KNOWN)
        self.assertEqual(errs, [])
        self.assertEqual(preds, [pc.Pred("PLN-AT-01", "FS", 1), pc.Pred("PLN-FX-02", "SS", 0),
                                 pc.Pred("PLN-WZ-03", "FF", -2)])

    def test_default_fs_and_sf_and_weeks(self):
        preds, errs = pc.parse_predecessors("PLN-AT-01；PLN-FX-02SF+1w\nPLN-WZ-03 - 3 days", self.KNOWN)
        self.assertEqual(errs, [])
        self.assertEqual(preds, [pc.Pred("PLN-AT-01"), pc.Pred("PLN-FX-02", "SF", 5), pc.Pred("PLN-WZ-03", "FS", -3)])

    def test_longest_id_wins_and_case_insensitive(self):
        preds, errs = pc.parse_predecessors("pln-at-010ss", self.KNOWN)
        self.assertEqual(errs, [])
        self.assertEqual(preds, [pc.Pred("PLN-AT-010", "SS", 0)])

    def test_unknown_and_bad_syntax(self):
        _, errs = pc.parse_predecessors("PLN-XX-99; PLN-AT-01QQ; PLN-AT-01+1.5d", self.KNOWN)
        self.assertEqual(len(errs), 3)

    def test_generic_without_known(self):
        preds, errs = pc.parse_predecessors("A-1FS+2d, B-2")
        self.assertEqual(errs, [])
        self.assertEqual(preds, [pc.Pred("A-1", "FS", 2), pc.Pred("B-2")])

    def test_format_roundtrip(self):
        preds = [pc.Pred("PLN-AT-01"), pc.Pred("PLN-FX-02", "SS", 0), pc.Pred("PLN-WZ-03", "FF", -2)]
        text = pc.format_predecessors(preds)
        self.assertEqual(text, "PLN-AT-01; PLN-FX-02SS; PLN-WZ-03FF-2d")
        self.assertEqual(pc.parse_predecessors(text, self.KNOWN)[0], preds)


class CalendarTest(unittest.TestCase):
    def test_weekend_and_holiday(self):
        cal = pc.WorkCalendar(date(2026, 10, 23), holidays=[date(2026, 10, 26)])  # Fri; Mon holiday
        self.assertEqual(cal.date_of(0), date(2026, 10, 23))
        self.assertEqual(cal.date_of(1), date(2026, 10, 27))
        self.assertEqual(cal.index_of(date(2026, 10, 24)), 1)  # Sat → rolls to Tue
        self.assertEqual(cal.workdays_between(date(2026, 10, 23), date(2026, 10, 30)), 5)
        self.assertEqual(cal.date_of(-1), date(2026, 10, 22))
        self.assertEqual(cal.index_of(date(2026, 10, 21)), -2)

    def test_start_on_weekend_rolls_forward(self):
        cal = pc.WorkCalendar(date(2026, 10, 17))
        self.assertEqual(cal.base, date(2026, 10, 19))


class SchedulerTest(unittest.TestCase):
    def cal(self, holidays=()):
        return pc.WorkCalendar(date(2026, 10, 19), holidays=holidays)  # Monday

    def run_tasks(self, tasks, cal=None):
        pc.resolve_preds(tasks)
        warnings = pc.compute_schedule(tasks, cal or self.cal())
        return {t.id: t for t in tasks}, warnings

    def test_fs_chain_and_holiday(self):
        r, _ = self.run_tasks([T("A", 5), T("B", 3, "A")], self.cal([date(2026, 10, 26)]))
        self.assertEqual((r["A"].start, r["A"].finish), (date(2026, 10, 19), date(2026, 10, 23)))
        self.assertEqual((r["B"].start, r["B"].finish), (date(2026, 10, 27), date(2026, 10, 29)))

    def test_ss_ff_sf_and_lags(self):
        r, _ = self.run_tasks([
            T("A", 5), T("B", 2, "ASS+2d"), T("C", 3, "AFF"), T("D", 2, "AFS-2d"), T("E", 2, "ASF+3d"),
        ])
        self.assertEqual(r["B"].start, date(2026, 10, 21))
        self.assertEqual(r["C"].finish, r["A"].finish)
        self.assertEqual(r["D"].start, date(2026, 10, 22))
        self.assertEqual(r["E"].finish, date(2026, 10, 21))  # SF：E 結束 >= A.start+3d

    def test_milestones(self):
        r, _ = self.run_tasks([T("A", 5), T("M", 0, "A"), T("B", 1, "M"), T("G", 0, "", "2026-10-21")])
        self.assertEqual(r["M"].finish, r["A"].finish)  # 里程碑顯示在前置完成日
        self.assertEqual(r["B"].start, date(2026, 10, 26))
        self.assertEqual(r["G"].start, date(2026, 10, 21))

    def test_constraints(self):
        r, w = self.run_tasks([T("A", 5), T("B", 2, "A", "2026-11-02"), T("C", 2, "A", "MSO 2026-10-20")])
        self.assertEqual(r["B"].start, date(2026, 11, 2))
        self.assertEqual(r["C"].start, date(2026, 10, 20))
        self.assertTrue(any("MSO" in x for x in w))
        r, _ = self.run_tasks([T("A", 5), T("B", 2, "A", "2026-10-01")])
        self.assertEqual(r["B"].start, date(2026, 10, 26))  # SNET 早於相依 → 相依優先

    def test_cycle_detection(self):
        tasks = [T("A", 1, "C"), T("B", 1, "A"), T("C", 1, "B")]
        pc.resolve_preds(tasks)
        with self.assertRaises(pc.PlanError) as cm:
            pc.compute_schedule(tasks, self.cal())
        self.assertIn("循環相依", str(cm.exception))
        self.assertIn("A", str(cm.exception))

    def test_duplicate_ids(self):
        with self.assertRaises(pc.PlanError):
            pc.compute_schedule([T("A", 1), T("A", 2)], self.cal())

    def test_self_reference_is_error_not_cycle(self):
        r, _ = self.run_tasks([T("A", 1, "A")])
        self.assertTrue(any("自己" in e for e in r["A"].errors))


class SeedTest(unittest.TestCase):
    def test_locked_totals_and_schedule(self):
        rows = seed.build_seed_tasks()
        tasks = []
        for d in rows:
            dur = int(d["Duration (d)"])
            tasks.append(pc.PlanTask(id=d["ID"], name=d["Task Name"], group=d["Group"],
                                     type=pc.norm_type(d["Type"], dur), duration=dur,
                                     preds_text=d["Predecessors"], constraint_text=d["Start Constraint"]))
        pc.resolve_preds(tasks)
        self.assertEqual([e for t in tasks for e in t.errors], [])
        holidays = [pc.parse_date_any(h[0]) for h in seed.HOLIDAYS]
        pc.compute_schedule(tasks, pc.WorkCalendar(date(2026, 10, 19), holidays=holidays))
        totals = {c: (e, a) for c, e, a, ok in pc.validate_group_totals(tasks, {"AT": 25, "PT": 15, "FX": 40, "WZ": 20})}
        self.assertEqual(totals, {"AT": (25, 25), "PT": (15, 15), "FX": (40, 40), "WZ": (20, 20)})
        by = {t.id: t for t in tasks}
        # 四組依序、不重疊
        self.assertLess(by["PLN-AT-08"].finish, by["PLN-PT-01"].start)
        self.assertLess(by["PLN-PT-06"].finish, by["PLN-FX-01"].start)
        self.assertLess(by["PLN-FX-09"].finish, by["PLN-WZ-01"].start)

    def test_plan_ids_never_match_register_regex(self):
        for d in seed.build_seed_tasks():
            self.assertTrue(pc.PLAN_ID_RE.match(d["ID"]), d["ID"])
            self.assertFalse(sr.REGISTER_ID_RE.match(d["ID"]), d["ID"])
            self.assertIsNone(sr.parse_register_id_from_summary(f"{d['ID']}_{d['Task Name']}"))


class FakeClient:
    def __init__(self, issues):
        self.issues = issues

    def jira_search(self, jql, *, max_results=100, fields=None, next_page_token=None):
        return {"issues": self.issues, "isLast": True}


class RegisterCollisionGuardTest(unittest.TestCase):
    def test_index_skips_plan_issues(self):
        issues = [
            {"key": "PMWC-10", "fields": {"summary": "RF-03_XO trim", "status": {"name": "待辦事項"}, "labels": []}},
            {"key": "PMWC-11", "fields": {"summary": "AT-01_Test tree", "status": {"name": "待辦事項"},
                                          "labels": ["c27-plan"]}},
            {"key": "PMWC-12", "fields": {"summary": "PLN-01_x", "status": {"name": "待辦事項"}, "labels": []}},
            {"key": "PMWC-13", "fields": {"summary": "DOE-01_y", "status": {"name": "待辦事項"},
                                          "labels": ["c99-plan-doe"]}},
        ]
        index = sr.load_jira_register_index(FakeClient(issues), "PMWC", "任務")
        self.assertEqual(set(index), {"RF-03"})

    def test_custom_exclude_label(self):
        issues = [{"key": "PMWC-20", "fields": {"summary": "ECO-01_z", "status": {"name": "x"},
                                                "labels": ["special"]}}]
        self.assertEqual(sr.load_jira_register_index(FakeClient(issues), "PMWC", "任務", exclude_labels=["special"]), {})

    def test_plan_side_detects_register_summary(self):
        rx = plan_sync.plan_id_regex("PLN")
        self.assertTrue(plan_sync.is_register_summary("RF-03_XO", rx))
        self.assertFalse(plan_sync.is_register_summary("PLN-AT-01_x", rx))

    def test_register_adapter_passes_exclude_label(self):
        from projectsync.register_adapter import build_register_config

        p = profiles.default_profile("c27")
        p["jira"]["project_key"] = "PMWC"
        cfg = build_register_config(p, {})
        self.assertEqual(cfg["jira"]["exclude_labels"], ["c27-plan"])


class MergeTest(unittest.TestCase):
    def test_scalar_rules(self):
        m = pc.merge_scalar
        self.assertEqual(m("name", "a", "a", "x"), "same")
        self.assertEqual(m("name", "b", "a", "a"), "push")
        self.assertEqual(m("name", "a", "b", "a"), "pull")
        self.assertEqual(m("name", "b", "c", "a"), "conflict")
        self.assertEqual(m("name", "b", "c", "a", "sheet"), "push")
        self.assertEqual(m("name", "b", "c", "a", "jira"), "pull")
        self.assertEqual(m("name", "b", "c", None), "push")  # 無快照：計畫欄位 Sheet 為主
        self.assertEqual(m("status", "b", "c", None), "pull")  # 無快照：執行欄位 Jira 為主

    def test_set_merge(self):
        res, after_push, after_pull = pc.merge_sets({"A", "C"}, {"A", "B", "D"}, {"A", "B"})
        # Sheet 移除 B、加 C；Jira 加 D
        self.assertEqual(res, {"A", "C", "D"})
        self.assertEqual(after_push, {"A", "C"})
        self.assertEqual(after_pull, {"A", "B", "D"})
        res, _, _ = pc.merge_sets({"A"}, {"B"}, None)
        self.assertEqual(res, {"A", "B"})

    def test_resolution_parse(self):
        self.assertEqual(pc.parse_resolution("use sheet"), "sheet")
        self.assertEqual(pc.parse_resolution("USE JIRA please"), "jira")
        self.assertEqual(pc.parse_resolution("Start: Sheet=..."), "")

    def test_description_roundtrip(self):
        adf = pc.build_description_adf("第一行\n第二行", ["Plan ID: X"])
        self.assertEqual(pc.description_from_adf(adf), "第一行\n第二行")


class StoreAndProfileTest(unittest.TestCase):
    def test_template_roundtrip_and_writeback(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.xlsx"
            settings = [("Project Name", "X", ""), ("Project Start", "2026-10-19", ""), ("Working Days", "Mon,Tue,Wed,Thu,Fri", "")]
            ps.build_template_xlsx(path, seed.build_seed_tasks()[:3], seed.GROUPS, seed.PEOPLE, seed.HOLIDAYS, settings)
            store = ps.XlsxStore(path)
            book = store.read()
            self.assertEqual(book.headers[:4], ["ID", "WBS", "Group", "Task Name"])
            self.assertEqual(len(book.rows), 3)
            self.assertEqual(book.settings["Project Start"], "2026-10-19")
            self.assertEqual(len(book.groups), len(seed.GROUPS))
            changes = ps.SheetChanges(cells=[(2, "Jira Key", "PMWC-1"), (2, "Start", "2026-11-02")],
                                      appends=[{"ID": "PLN-G-09", "Task Name": "new"}],
                                      state={"PMWC-1": {"id": "PLN-G-01", "fields": {"name": "x"}}})
            store.write(book, changes)
            book2 = store.read()
            self.assertEqual(book2.rows[0][book2.col("Jira Key")], "PMWC-1")
            self.assertEqual(book2.rows[0][book2.col("Start")], "2026-11-02")
            self.assertEqual(book2.rows[-1][0], "PLN-G-09")
            self.assertEqual(book2.state["PMWC-1"]["id"], "PLN-G-01")

    def test_auto_id_and_register_like_id_blocked(self):
        book = ps.PlanBook(headers=["ID", "Group", "Task Name", "Duration (d)", "Sync to Jira"],
                           rows=[["PLN-AT-03", "AT", "x", "1", "Y"], ["", "AT", "new", "2", "Y"],
                                 ["AT-01", "AT", "bad", "1", "Y"]])
        tasks, _ = plan_sync.tasks_from_book(book, "PLN")
        self.assertEqual(tasks[1].id, "PLN-AT-04")
        self.assertFalse(tasks[2].sync)

    def test_profile_save_load(self):
        old = profiles.PROJECTS_DIR
        with tempfile.TemporaryDirectory() as d:
            profiles.PROJECTS_DIR = Path(d)
            try:
                p = profiles.default_profile("demo", "Demo")
                p["jira"]["project_key"] = "DEMO"
                profiles.save_profile(p)
                self.assertEqual(profiles.list_projects(), ["demo"])
                q = profiles.load_profile("demo")
                self.assertEqual(q["jira"]["project_key"], "DEMO")
                self.assertEqual(q["plan"]["label"], "demo-plan")
                self.assertIn("Jira project_key 未設定", profiles.validate_profile(profiles.default_profile("x")))
                profiles.delete_profile("demo")
                self.assertEqual(profiles.list_projects(), [])
            finally:
                profiles.PROJECTS_DIR = old

    def test_protected_confluence_page(self):
        with self.assertRaises(RuntimeError):
            plan_sync.upsert_confluence_page(None, space_id="1", parent_id="1", title="mail_checking",
                                             page_id="", body="", dry_run=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
