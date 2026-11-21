from datetime import date
import unittest
from unittest.mock import patch

from research import CitationReceipts, crossref_metadata, compare_book, retrieve
from workspace import add_account, has_project, permitted, pilot_summary, purge_retained, Workspace
from workflow import WorkflowError, seed, decide_issues
from server import snapshot
from pathlib import Path
from tempfile import TemporaryDirectory
import json

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.data = seed()
        self.item = self.data["manuscripts"][0]
        self.entry = self.data["library"][0]

    def test_receipt_is_bound_to_current_evidence_project_and_anchor(self):
        receipts = CitationReceipts()
        version = self.item["versions"][0]
        hit = retrieve([self.entry], "缺陷", "2026-11-01", self.item)[0]
        ticket = receipts.issue(hit, self.item["id"], version["id"], 2, "2026-11-01")
        self.assertEqual(receipts.verify(ticket, self.data, self.item, version, 2)["id"], self.entry["id"])
        with self.assertRaises(WorkflowError):
            receipts.verify(ticket, self.data, self.item, version, 0)
        self.entry["text"] += "换版"
        with self.assertRaises(WorkflowError):
            receipts.verify(ticket, self.data, self.item, version, 2)

    def test_crossref_only_uses_doi_and_flags_retraction_metadata(self):
        sample = {"DOI":"10.1234/test","title":["Item"],"update-to":[{"type":"retraction","DOI":"10.1234/notice"}]}
        with patch("research.crossref_request", return_value=sample) as request:
            hit = crossref_metadata("https://doi.org/10.1234/test")
        request.assert_called_once_with("10.1234/test")
        self.assertEqual(hit["status"], "found")
        self.assertTrue(hit["retracted"])

    def test_full_text_comparison_locates_reuse(self):
        line = "确认统计口径相同后，再对比不同工艺设备在同一生产条件下的不良品数量。"
        hit = compare_book([{"text": line, "kind":"text", "location":"第 3 章"}], line)
        self.assertEqual(hit[0]["anchor"], 0)
        self.assertEqual(hit[0]["kind"], "连续原文")

    def test_sqlite_migrates_legacy_json_and_keeps_revisions(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "workspace.json").write_text(json.dumps(self.data), encoding="utf-8")
        first = Workspace(root)
        first.data["manuscripts"][0]["title"] = "持久化"
        first.save(first.data)
        self.assertEqual(Workspace(root).data["manuscripts"][0]["title"], "持久化")
        self.assertTrue((root / "workspace.json").is_file())

    def test_trial_counts_and_cost_units(self):
        summary = pilot_summary([{"words":10000,"truePositive":8,"falsePositive":2,"falseNegative":1,
                                  "traditionalMinutes":1200,"assistedMinutes":840,"hourly":120,
                                  "runtimeCost":180,"fixedCost":54000,"volume":300}])
        self.assertEqual(summary["precision"], .8)
        self.assertEqual(summary["netSavings"],360)
        self.assertEqual(summary["savedMinutesPer10k"],360)

    def test_project_roles_and_login_do_not_expose_foreign_manuscripts(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        store = Workspace(Path(temporary.name))
        item = store.data["manuscripts"][0]
        secret = "test-session-password"
        add_account(store.data,"subject","学科编辑",secret,"reviewer",item["id"])
        store.save(store.data)
        self.assertIsNone(store.identity(""))
        token = store.login("subject",secret)
        account = store.identity("wenheng="+token)
        self.assertTrue(has_project(account,item))
        self.assertFalse(permitted(account,"flow"))
        frame = snapshot(store.data,account)
        self.assertEqual([one["id"] for one in frame["manuscripts"]],[item["id"]])
        self.assertTrue(all(issue["manuscript"] == item["id"] for issue in frame["issues"]))

    def test_expired_version_is_hidden_and_can_be_retired(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        store = Workspace(Path(temporary.name))
        item = next(item for item in store.data["manuscripts"] if item["status"]=="archived")
        version = item["versions"][0]
        original = store.directory/"0123456789abcd.txt"
        original.write_text("测试留档",encoding="utf-8")
        version.update(retentionUntil="2010-01-01",file=original.name)
        store.save(store.data)
        result = next(entry for entry in snapshot(store.data)["manuscripts"] if entry["id"]==item["id"])
        self.assertFalse(result["versions"][0]["download"])
        self.assertEqual(purge_retained(store),[(item["id"],version["id"])])
        self.assertTrue(original.exists())
        purge_retained(store,apply=True)
        self.assertFalse(original.exists())
        archived = next(entry for entry in store.data["manuscripts"] if entry["id"]==item["id"])
        self.assertEqual(archived["versions"][0]["blocks"],[])

    def test_global_batch_same_rule_across_projects(self):
        first, second = self.data["issues"][-2:]
        second["manuscript"] = self.data["manuscripts"][0]["id"]
        second["version"] = self.data["manuscripts"][0]["versions"][0]["id"]
        ids = [first["id"],second["id"]]
        with self.assertRaises(WorkflowError):
            decide_issues(self.data,ids,"resolved","全书一致")
        decide_issues(self.data,ids,"resolved","全书一致",global_batch=True,actor="测试编辑")
        self.assertEqual(first["decidedBy"],"测试编辑")
        self.assertEqual(second["state"],"resolved")
