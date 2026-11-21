import unittest
from datetime import date
from io import BytesIO
from zipfile import ZipFile

from workflow import (WorkflowError, apply_transition, lookup_evidence, parse_document,
                      screen, compare_paragraphs, annotate_docx, seed, decide_issues)
from server import save_bibliography, snapshot

DOC = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter 1</w:t></w:r></w:p>
<w:p><w:r><w:t>See figure 7. DOI 10.1234/example.</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>'''

def docx():
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", DOC)
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
    return out.getvalue()

class WorkflowTests(unittest.TestCase):
    def test_docx_original_paragraph_and_comment_anchor(self):
        parsed = parse_document("sample.docx", docx())
        self.assertEqual(parsed[0]["kind"], "heading")
        self.assertEqual(parsed[2]["kind"], "table")
        issue = {"title": "Evidence needed", "detail": "Check the primary source", "anchor": 1, "state": "pending"}
        with ZipFile(BytesIO(annotate_docx(docx(), [issue]))) as archive:
            self.assertIn(b"Evidence needed", archive.read("word/comments.xml"))
            self.assertIn(b"commentRangeStart", archive.read("word/document.xml"))

    def test_screening_and_targeted_recheck(self):
        line = '这是同一份质量记录中的重复段落，用于判断跨节拼接造成的重复问题。'
        original = parse_document("sample.txt", (line + '\n' + line + '\n图7').encode())
        self.assertEqual({x["category"] for x in screen(original)}, {"内容重复", "图文核验"})
        updated = parse_document("sample.txt", (line + '\n图7').encode())
        changes, affected = compare_paragraphs(original, updated)
        self.assertEqual(changes[0]["kind"], "删除")
        self.assertEqual(affected, set())

    def test_evidence_date_project_and_expiry(self):
        base = {"title": "术语手册", "text": "定义缺陷", "keywords": "缺陷", "status": "approved",
                "projects": "M1", "region": "", "validFrom": "2010-01-01",
                "validUntil": "2020-01-01", "approvalUntil": "", "chapter": "1", "page": "5"}
        self.assertEqual(lookup_evidence([base], "缺陷", "2020-01-01", "M1", ""), [])
        self.assertEqual(lookup_evidence([base], "缺陷", "2019-12-31", "M2", ""), [])
        self.assertEqual(lookup_evidence([base], "缺陷", "2019-12-31", "M1", ""), [base])

    def test_hold_and_signoff_gates(self):
        manuscript = {"stage": 4, "status": "active", "events": []}
        hold = {"state": "pending", "blocking": True}
        with self.assertRaises(WorkflowError):
            apply_transition(manuscript, [hold], "advance", "同意签核")
        apply_transition(manuscript, [hold], "pause", "核对原刊")
        with self.assertRaises(WorkflowError):
            apply_transition(manuscript, [hold], "resume", "原刊缺失")
        hold["state"] = "resolved"
        apply_transition(manuscript, [hold], "resume", "证据已验证")
        apply_transition(manuscript, [hold], "advance", "同意签核")
        self.assertEqual(manuscript["status"], "archived")

    def test_group_decision_disallows_merging_individual_judgments(self):
        data = seed()
        issues = data["issues"][:2]
        before = [issue["state"] for issue in issues]
        with self.assertRaises(WorkflowError):
            decide_issues(data, [issue["id"] for issue in issues], "confirmed", "")
        self.assertEqual([issue["state"] for issue in issues], before)

    def test_citation_hold_ignores_lookup_failures(self):
        data = seed()
        manuscript = data["manuscripts"][0]
        template = {"chapter": "第 3 章", "sourceA": "原刊目录", "sourceB": "独立目录",
                    "note": "记录字段差异", "covered": "yes"}
        for number, status in enumerate(("conflict", "unavailable", "unverified"), 1):
            save_bibliography(data, manuscript, {**template, "number": number,
                              "key": f"引用 {number}", "status": status})
        self.assertEqual(manuscript["status"], "active")
        save_bibliography(data, manuscript, {**template, "number": 2,
                          "key": "引用 2", "status": "conflict"})
        self.assertEqual(manuscript["status"], "paused")
        self.assertTrue(data["issues"][0]["blocking"])

    def test_expired_library_record_does_not_expose_original_text(self):
        data = seed()
        entry = data["library"][0]
        entry["approvalUntil"] = "2000-01-01"
        restricted = snapshot(data)["library"][0]
        self.assertTrue(restricted["restricted"])
        self.assertEqual(restricted["text"], "")
        self.assertEqual(lookup_evidence(data["library"], "缺陷", "2026-10-01", "MS-2026-018", ""), [])

if __name__ == "__main__":
    unittest.main()
