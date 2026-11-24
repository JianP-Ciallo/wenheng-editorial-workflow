import unittest
from threading import Event
from unittest.mock import patch

from agent_documents import text_docx
from editor_agent import lookup_doi, run_review
from workflow import parse_document


def paragraph(text, kind="text", **extra):
    return {"text": text, "kind": kind, "location": "试验篇", "part": "document", **extra}


class EditorialAgentTests(unittest.TestCase):
    def review(self, blocks, lookup=None, findings=(), issues=(), library=(), **item_fields):
        version = {"id": "v1", "name": "fixture.txt", "type": "原稿", "blocks": blocks}
        item = {"id": "MS-TEST", "title": "日程说明", "author": "Test",
                "versions": [version], **item_fields}
        steps = []
        callback = lookup or (lambda doi: self.fail("No reference needs an online lookup"))
        brief = run_review({"issues": issues, "library": library}, item, version, None,
                           lambda *_: findings, lambda step, message: steps.append(step),
                           doi_lookup=callback)
        return brief, steps

    def test_clear_prose_can_reach_signoff_without_an_applicable_reference_or_figure(self):
        brief, steps = self.review([paragraph("本册说明编辑会议的日程和流程。")])
        self.assertEqual(brief["status"], "ready_for_signoff")
        self.assertEqual(brief["cases"], [])
        self.assertEqual(brief["checks"][2]["status"], "not_applicable")
        self.assertEqual(steps, ["structure", "editorial", "citations", "proofs",
                                 "copyright", "academic", "documents"])

    def test_real_doi_identity_does_not_clear_retraction_or_coverage_errors(self):
        blocks = [paragraph("参见 [1,2]。官网地址 https://doi.org/10.1000/not-a-reference。"),
                  paragraph("参考文献", "heading"),
                  paragraph("[1] Jones, Thoughtful test methods, 2021. https://doi.org/10.1000/test."),
                  paragraph("[2] Zhang, Basics of publishing, 2020.")]
        requested = []

        def lookup(doi):
            requested.append(doi)
            return {"status": "retracted", "title": "Thoughtful test methods",
                    "notices": [{"doi": "10.1000/withdrawal", "type": "retraction", "source": "publisher"}]}

        brief, _ = self.review(blocks, lookup)
        self.assertEqual(requested, ["10.1000/test"])
        self.assertEqual(brief["status"], "escalation_required")
        cases = [case for case in brief["cases"] if case["stage"] == "citations"]
        self.assertEqual({case["anchor"] for case in cases}, {0, 2, 3})
        self.assertTrue(all("B" not in {option["id"] for option in case["options"]} for case in cases if case["anchor"]))
        retraction = next(case for case in cases if case["anchor"] == 2)
        self.assertEqual(retraction["evidence"][-1]["url"], "https://doi.org/10.1000/withdrawal")

    def test_local_proof_tools_can_run_while_crossref_is_in_flight(self):
        proof_started = Event()
        blocks = [paragraph("参见 [1]。"), paragraph("参考文献", "heading"),
                  paragraph("[1] Peng, Work on pigments, 2020. doi:10.1000/test")]

        def lookup(doi):
            self.assertTrue(proof_started.wait(2))
            return {"status": "found", "title": "Work on pigments"}

        def proof(*args):
            proof_started.set()
            return [], False, "无图表"

        with patch("editor_agent._proof_review",side_effect=proof):
            brief, _ = self.review(blocks,lookup)
        self.assertEqual(brief["status"], "ready_for_signoff")

    def test_doi_exists_but_title_mismatch_requires_review(self):
        blocks = [paragraph("参见 [1]。"), paragraph("参考文献", "heading"),
                  paragraph("[1] Peng, Work on pigments, 2020. doi:10.1000/wrong")]
        brief, _ = self.review(blocks, lambda doi: {"status": "found", "title": "Herbarium management",
                                                    "year": 2020, "authors": ["Wei Peng"]})
        self.assertEqual(brief["citations"][0]["status"], "mismatch")
        self.assertEqual(brief["status"], "escalation_required")

    def test_citation_without_doi_can_be_adjudicated_with_two_matching_sources(self):
        blocks = [paragraph("参见 [1]。"), paragraph("参考文献", "heading"),
                  paragraph("[1] 著作出版流程实务，2021。")]
        record = {"chapter": "参考文献", "number": 1, "key": "著作出版流程实务",
                  "status": "verified", "sourceA": "出版目录", "sourceB": "馆藏书目"}
        brief, _ = self.review(blocks, bibliography=[record])
        case = brief["cases"][0]
        self.assertIn("B", {option["id"] for option in case["options"]})
        self.assertIn("馆藏书目", case["evidence"][1]["text"])
        brief, _ = self.review(blocks, bibliography=[{**record, "number": 2}])
        self.assertNotIn("B", {option["id"] for option in brief["cases"][0]["options"]})

    def test_missing_bibliographic_scope_and_unverified_graphics_are_cases(self):
        blocks = [paragraph("参见 [4]。"), paragraph("图 1 工作台访问示意"),
                  paragraph("参考文献", "heading")]
        brief, _ = self.review(blocks)
        self.assertEqual({case["stage"] for case in brief["cases"]}, {"citations", "proofs"})

    def test_active_human_decisions_surface_with_issue_identifier(self):
        manual = {"id": "manual-1", "manuscript": "MS-TEST", "version": "v1", "origin": "manual",
                  "category": "体例用语", "title": "核对社内体例", "detail": "从业务术语表核对名称。",
                  "state": "waiting", "severity": "normal", "anchor": 0}
        brief, _ = self.review([paragraph("本册说明编辑会议的日程和流程。")], issues=[manual])
        self.assertEqual(brief["cases"][0]["issueId"], "manual-1")
        self.assertEqual({option["id"] for option in brief["cases"][0]["options"]}, {"A", "B", "C"})

    def test_manual_blocker_matching_a_screen_finding_cannot_be_waived(self):
        finding = {"category":"体例用语","title":"成对标点数量不一致","detail":"请核对全角括号。",
                   "severity":"low","anchor":0,"evidence":[],"blocking":False}
        manual = {**finding,"id":"manual-1","manuscript":"MS-TEST","version":"v1","origin":"manual",
                  "state":"pending","blocking":True}
        brief,_ = self.review([paragraph("正文（余下内容。")],findings=[finding],issues=[manual])
        case = next(case for case in brief["cases"] if case["issueId"] == manual["id"])
        self.assertNotIn("B",[choice["id"] for choice in case["options"]])

    def test_citations_without_a_parsable_bibliography_are_reviewed(self):
        for text in ("Published work supports this (Smith 2020).","Smith (2020) observed some details.",
                     "参见李明（2020）。","来源 https://doi.org/10.1000/source。"):
            with self.subTest(text=text):
                brief,_ = self.review([paragraph(text)])
                self.assertTrue(any(case["stage"] == "citations" for case in brief["cases"]))

    def test_unmeasured_images_and_unsupported_formulas_do_not_pass(self):
        blocks = [paragraph("本册介绍编辑台。"), paragraph("[图像]", "image"),
                  paragraph("[公式]", "formula", formulaCheck=True)]
        brief, _ = self.review(blocks)
        self.assertEqual({case["anchor"] for case in brief["cases"]}, {1, 2})
        self.assertEqual(brief["status"], "escalation_required")

    def test_explicit_standard_needs_an_applicable_project_authorized_record(self):
        blocks = [paragraph("抽样实施依据 GB/T 2828.1-2012。")]
        evidence = {"id": "e1", "type": "evidence", "identifier": "GB/T 2828.1", "edition": "2012 版",
                    "projects": "MS-TEST", "status": "approved", "title": "抽样", "text": "教材示例条文",
                    "chapter": "第 1 章", "page": "1", "validFrom": "2012-01-01"}
        brief, _ = self.review(blocks, library=[{**evidence, "validUntil": "2019-01-01"}])
        self.assertEqual(brief["cases"][0]["stage"], "academic")
        self.assertNotIn("教材示例", str(brief["cases"]))
        brief, _ = self.review(blocks, library=[{**evidence, "projects": "MS-OTHER"}])
        self.assertEqual(brief["status"], "escalation_required")
        brief, _ = self.review(blocks, library=[evidence])
        self.assertEqual(brief["status"], "ready_for_signoff")

    def test_word_is_rechecked_after_a_fix_and_only_matching_old_rule_is_resolved(self):
        raw = text_docx("材料清单", ["两件样品的质量分别为 25KG 和 40 千克。"])
        blocks = parse_document("draft.docx", raw)
        version = {"id": "v1", "type": "原稿", "name": "draft.docx", "blocks": blocks}
        item = {"id": "MS-TEST", "versions": [version]}
        issue = {"id": "old-rule", "manuscript": "MS-TEST", "version": "v1", "origin": "screen",
                 "category": "体例用语", "state": "pending", "group": "unit-spacing:kg",
                 "anchor": 1, "title": "数值与单位「kg」之间缺少空格", "detail": "",
                 "severity": "low"}
        manual = {**issue, "id": "manual", "origin": "manual", "title": "作者须确认数量"}
        data = {"issues": [issue, manual], "library": []}

        def recheck(data, item, revised, content):
            after = parse_document("draft.docx", content)
            self.assertEqual(revised["blocks"], after)
            self.assertIn("25 kg", after[1]["text"])
            self.assertIn("40 kg", after[1]["text"])
            return []

        brief = run_review(data, item, version, raw, recheck, lambda *_: None)
        self.assertEqual(brief["autoFixCount"], 2)
        self.assertEqual(brief["fixedIssueIds"], ["old-rule"])
        self.assertEqual([case["issueId"] for case in brief["cases"]], ["manual"])
        self.assertIn("25KG", version["blocks"][1]["text"])

    @patch("editor_agent._updates_for_doi")
    @patch("editor_agent.crossref_metadata")
    def test_notices_are_checked_against_the_cited_doi(self, metadata, updates):
        metadata.return_value = {"status": "found", "title": "Source work", "doi": "10.1000/test"}
        updates.return_value = [{"doi": "10.1000/notice", "type": "retraction", "source": "publisher"}]
        self.assertEqual(lookup_doi("10.1000/test")["status"], "retracted")
        updates.assert_called_once_with("10.1000/test")
        updates.return_value = None
        self.assertEqual(lookup_doi("10.1000/test")["status"], "unavailable")
