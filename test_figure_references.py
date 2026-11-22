import unittest

from proofs import figure_contexts, figure_links, inspect_proof
from server import snapshot
from workflow import issue_data, make_issue, screen, seed


class FigureReferenceTests(unittest.TestCase):
    def setUp(self):
        self.blocks = [
            {"kind":"text", "page":19, "text":"MCP 服务器设计见附录 D 图 9。本研究评估多个工具。"},
            {"kind":"text", "page":28, "text":"图9. MCP 版本的设计示意图。该图展示模块调用关系。"},
            {"kind":"text", "page":29, "text":"图 10 所示结果与实验吻合。"},
            {"kind":"text", "page":30, "text":"流程参见图 3.2。"},
            {"kind":"text", "page":31, "text":"图3－2．流程图。补充数据见附表。"},
        ]

    def test_recognizes_caption_after_dot_and_links_appendix_page(self):
        captions, refs = figure_contexts(self.blocks)
        self.assertEqual(captions, {"9":1,"3-2":4})
        self.assertEqual(refs["9"], [0])
        self.assertEqual(figure_links(self.blocks)[0], [{"number":"9","anchor":1,"page":28}])
        self.assertEqual([row["title"] for row in screen(self.blocks)], ["缺少图 10 对应图题"])

    def test_legacy_missing_caption_is_removed_from_active_queue_if_caption_is_present(self):
        data = seed()
        item = data["manuscripts"][0]
        version = item["versions"][0]
        version["blocks"] = self.blocks
        data["issues"] = [make_issue(item,version["id"],
                                    issue_data("图文核验", f"缺少图 {number} 对应图题",
                                               "提取的正文里没有匹配的图题。", anchor, "high"))
                          for number,anchor in (("9",0),("10",2))]
        self.assertEqual([row["title"] for row in snapshot(data)["issues"]], ["缺少图 10 对应图题"])

    def test_named_appendix_reference_does_not_trigger_page_distance_warning(self):
        self.blocks.insert(1, {"kind":"heading","page":27,"text":"附录 D. MCP 函数"})
        findings = inspect_proof(self.blocks,name="sample.pdf")
        self.assertFalse(any("图 9 首次指代" in issue["title"] for issue in findings))
        self.assertFalse(any(issue["title"] == "缺少图 9 对应图题" for issue in screen(self.blocks)))
