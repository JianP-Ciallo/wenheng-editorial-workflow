import base64
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_jobs import approve, claim, enqueue, review_one, rule, stale
from server import Handler, create_manuscript
from workflow import WorkflowError, issue_data, make_issue
from workspace import LOCAL, Workspace


def fake_review(data, item, version, raw, check, progress):
    progress("structure", "已解析正文")
    return {
        "status": "escalation_required", "characters": 6, "paragraphs": 1,
        "fixes": [], "checks": [], "citations": [],
        "cases": [{"id": "clarity", "title": "叙述待核", "detail": "口径需说明",
                   "options": [{"id": option} for option in "ABC"]}],
    }


class AgentJobTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.store = Workspace(Path(directory.name))
        self.handler = Handler.__new__(Handler)
        self.handler.store = self.store
        self.item = create_manuscript(self.store.data, {
            "title": "审校任务", "author": "作者", "editor": "责任编辑", "category": "工程技术",
        })
        self.project = self.item["id"]
        self.store.save(self.store.data)

    def upload(self, kind="原稿", name="manuscript.txt", text="审校原稿正文。", **fields):
        self.data = self.store.load()
        payload = {"kind":kind,"name":name,"content":base64.b64encode(text.encode()).decode(),**fields}
        result, _ = self.handler.mutate(self.data, f"/api/manuscripts/{self.project}/upload", payload, LOCAL)
        self.store.save(self.data)
        return result

    def load_run(self, ident):
        data = self.store.load()
        item = next(item for item in data["manuscripts"] if item["id"] == self.project)
        run = next(run for run in data["agent_runs"] if run["id"] == ident)
        return data, item, run

    def run_upload(self):
        result = self.upload()
        self.assertTrue(review_one(self.store, None, fake_review))
        return self.load_run(result["agentRun"])

    def test_upload_is_queued_once_and_worker_persists_result(self):
        result = self.upload()
        data, item, run = self.load_run(result["agentRun"])
        self.assertEqual(run["status"], "queued")
        self.assertEqual(enqueue(data, item, "local")["id"], run["id"])
        self.assertTrue(review_one(self.store, None, fake_review))
        _, _, completed = self.load_run(run["id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["progress"], 100)
        self.assertTrue(any("已解析正文" in record["message"] for record in completed["trace"]))
        self.assertFalse(review_one(self.store, None, fake_review))

    def test_old_job_can_be_recovered_after_lease_expires(self):
        result = self.upload()
        first = claim(self.store)
        self.assertIsNone(claim(self.store))
        self.store.data["agent_runs"][-1]["heartbeat"] = 0
        self.store.save(self.store.data)
        self.assertTrue(review_one(self.store, None, fake_review))
        self.assertNotEqual(first[-1], self.load_run(result["agentRun"])[2].get("lease"))

    def test_only_explicit_ruling_and_approval_can_release_version(self):
        data, item, run = self.run_upload()
        with self.assertRaises(WorkflowError):
            approve(data, item, run, "负责人", "确认")
        rule(data, item, run, "clarity", "B", "责任编辑", "接受作者给出的口径")
        self.assertEqual(item["status"], "active")
        approve(data, item, run, "负责人", "核对完毕，进入印制")
        self.assertEqual((item["status"], run["brief"]["status"]), ("archived", "approved"))
        self.assertEqual(run["brief"]["approval"]["actor"], "负责人")

    def test_new_upload_or_manual_changes_invalidate_old_approval(self):
        data, item, run = self.run_upload()
        rule(data, item, run, "clarity", "B", "编辑", "接受差异")
        item["proofSettings"] = {"photoPpi": 450}
        self.assertTrue(stale(data, item, run))
        with self.assertRaises(WorkflowError):
            approve(data, item, run, "负责人", "确认")
        self.upload("返修稿")
        new_data, new_item, run = self.load_run(run["id"])
        self.assertTrue(stale(new_data, new_item, run))

    def test_signoff_rechecks_date_limited_rights(self):
        request = self.upload()
        data, item, run = self.load_run(request["agentRun"])
        issue = make_issue(item,run["version"],issue_data("版权核验","封面","授权到期核对"))
        issue["rights"] = {"decision":"licensed","version":run["version"],"term":"dated",
                           "expiry":date.today().isoformat(),"planned":["纸书 / 封面"],"allowed":["纸书 / 封面"]}
        data["issues"].append(issue)
        run = enqueue(data,item,"local")
        self.store.save(data)
        self.assertTrue(review_one(self.store,None,fake_review))
        data, item, run = self.load_run(run["id"])
        rule(data,item,run,"clarity","B","编辑","经核对已有证据")
        with patch("agent_jobs.date") as clock:
            clock.today.return_value = date.today() + timedelta(days=1)
            self.assertTrue(stale(data,item,run))
            with self.assertRaises(WorkflowError):
                approve(data,item,run,"负责人","确认")

    def test_return_and_rejection_have_distinct_artifact_records(self):
        for option, expected, state in (("A","returned","revision"), ("C","rejected","rejected")):
            with self.subTest(option=option):
                data, item, run = self.run_upload()
                rule(data, item, run, "clarity", option, "编辑", "有待作者解释")
                self.assertEqual((item["status"],run["brief"]["status"]),(state,expected))
                self.assertEqual(run["brief"]["cases"][0]["ruling"]["note"],"有待作者解释")

    def test_data_supplement_can_be_replaced_without_deleting_old_evidence(self):
        self.upload()
        old = self.upload("补件","old.csv","项目,数量\n甲,2\n乙,3\n合计,99\n")
        new = self.upload("补件","new.csv","项目,数量\n甲,2\n乙,3\n合计,5\n",supersedes=old["id"])
        _, item, run = self.load_run(new["agentRun"])
        versions = {version["id"]:version for version in item["versions"]}
        self.assertEqual(versions[old["id"]]["supersededBy"],new["id"])
        self.assertEqual(versions[new["id"]]["supersedes"],old["id"])
        self.assertTrue((self.store.directory / versions[old["id"]]["file"]).is_file())
        from server import agent_checks
        self.assertTrue(review_one(self.store,agent_checks))
        _, _, run = self.load_run(run["id"])
        self.assertFalse(any(case["title"] == "补件计算待核" for case in run["brief"]["cases"]))


if __name__ == "__main__":
    unittest.main()
