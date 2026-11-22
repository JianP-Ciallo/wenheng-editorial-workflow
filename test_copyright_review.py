import unittest

from copyright_review import copyright_findings, copyright_status
from server import Handler, snapshot
from workflow import WorkflowError, make_issue, issue_data, seed
from workspace import LOCAL


class CopyrightReviewTests(unittest.TestCase):
    def setUp(self):
        self.data = seed()
        self.item = self.data["manuscripts"][0]
        self.item.update(stage=4, status="active")
        self.version = self.item["versions"][0]
        self.issue = make_issue(self.item, self.version["id"],
                                issue_data("版权核验", "封面照片", "核对使用依据", 0))
        self.issue["state"] = "resolved"
        self.data.update(issues=[self.issue], tasks=[])
        self.handler = Handler.__new__(Handler)
        self.record = {
            "id": self.issue["id"], "version": self.version["id"], "decision": "licensed",
            "source": "图片库 A，作品 B", "rightsOwner": "摄影者",
            "planned": "电子书 / 封面", "allowed": "纸书 / 封面\n电子书 / 内页",
            "basis": "授权书第 2 条", "term": "indefinite",
        }

    def mutate(self, operation, payload):
        return self.handler.mutate(self.data, f"/api/manuscripts/{self.item['id']}/{operation}",
                                   payload, LOCAL)

    def test_signoff_requires_coverage_of_each_media_and_purpose_pair(self):
        self.mutate("rights", self.record)
        self.assertEqual(copyright_status(self.issue, self.item)["missing"], ["电子书 / 封面"])
        with self.assertRaisesRegex(WorkflowError, "版权核验"):
            self.mutate("action", {"action": "advance", "note": "终校签核"})
        self.mutate("rights", {**self.record, "allowed": "电子书 / 封面"})
        self.assertEqual(len(self.issue["rights"]["history"]), 1)
        self.mutate("action", {"action": "advance", "note": "终校签核"})
        self.assertEqual(self.item["status"], "archived")

    def test_latest_proof_requires_a_fresh_editorial_decision(self):
        self.mutate("rights", {**self.record, "allowed": self.record["planned"]})
        self.item["versions"].append({**self.version, "id": "proof", "type": "校样"})
        self.assertEqual(copyright_status(self.issue, self.item)["status"], "stale")
        with self.assertRaises(WorkflowError):
            self.mutate("action", {"action": "advance", "note": "终校签核"})

    def test_review_record_checks_expiry_and_hides_retired_evidence(self):
        evidence = {**self.version, "type": "补件", "id": "signed-agreement"}
        self.item["versions"].append(evidence)
        self.mutate("rights", {**self.record, "allowed": self.record["planned"], "term": "dated",
                               "expiry": "2026-11-22", "evidenceVersion": evidence["id"]})
        self.assertEqual(copyright_status(self.issue, self.item, "2026-11-22")["status"], "ready")
        self.assertEqual(copyright_status(self.issue, self.item, "2026-11-23")["status"], "expired")
        evidence["retired"] = True
        public = snapshot(self.data)["issues"][0]
        self.assertEqual(public["rights"], {})
        self.assertEqual(public["copyright"]["status"], "unavailable")

    def test_candidate_extraction_preserves_source_and_incremental_scope(self):
        blocks = [{"text": "图 3-2。图片来源：网络"}, {"text": "本节译自国外手册第 3 版"},
                  {"text": "正文讨论图书翻译的历史。"}]
        findings = copyright_findings(blocks, {1})
        self.assertEqual([row["anchor"] for row in findings], [1])
        self.assertEqual(findings[0]["evidence"][0]["text"], blocks[1]["text"])
        self.assertFalse(findings[0]["blocking"])
