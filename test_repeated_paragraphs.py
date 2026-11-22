import unittest

from server import snapshot
from workflow import decide_issues, issue_data, make_issue, screen, seed, WorkflowError


HEADER = "同一家期刊每页重复排印的页眉信息与出版物识别编号及卷期"
BODY = "这是正文中需要逐段核对的一句话，作者在三处引用了同一段操作说明。"


def block(text, page, top):
    return {"text": text, "kind": "text", "page": page, "pageHeight": 800,
            "bbox": [50, top, 440, top + 12]}


class RepeatedParagraphTests(unittest.TestCase):
    def test_excludes_page_headers_and_groups_body_positions(self):
        blocks = [entry for page in (1, 2, 3)
                  for entry in [block(HEADER, page, 28), block(BODY, page, 200)]]
        findings = [issue for issue in screen(blocks) if issue["category"] == "内容重复"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["repeatAnchors"], [1, 3, 5])
        self.assertEqual(screen(blocks, {5})[0]["repeatAnchors"], [1, 3, 5])

    def test_decimal_and_punctuation_differences_are_not_exact_matches(self):
        blocks = [block(f"这一段包含同样的表述，但文稿最后记录的本次不良率为 {value}。", index, 210)
                  for index, value in enumerate(("3.5%", "35%"), 1)]
        self.assertEqual(screen(blocks), [])

    def test_legacy_headers_are_filtered_and_only_the_same_repeat_group_can_be_decided(self):
        data = seed()
        item = data["manuscripts"][0]
        version = item["versions"][0]
        version["blocks"] = [block(HEADER, page, 28) for page in (1, 2, 3)]
        version["blocks"].extend(block(BODY, page, 200) for page in (1, 2, 3, 4))
        def old_issue(anchor, reference):
            return make_issue(item, version["id"], issue_data(
                "内容重复", "正文存在相同段落", "核对两处语境", anchor, evidence=[
                    {"label": "原文", "text": version["blocks"][reference]["text"], "anchor": reference},
                ]))
        header, first, second, different = (old_issue(*pair) for pair in [(1, 0), (4, 3), (5, 3), (6, 5)])
        data["issues"] = [header, first, second, different]
        views = {row["id"]: row for row in snapshot(data)["issues"]}
        self.assertNotIn(header["id"], views)
        self.assertEqual(views[first["id"]]["repeatGroup"], views[second["id"]]["repeatGroup"])
        self.assertNotEqual(views[first["id"]]["repeatGroup"], views[different["id"]]["repeatGroup"])
        with self.assertRaises(WorkflowError):
            decide_issues(data, [first["id"], different["id"]], "resolved", "已核对语境")
        decide_issues(data, [first["id"], second["id"]], "resolved", "已核对语境")
        self.assertEqual([row["state"] for row in data["issues"]], ["pending", "resolved", "resolved", "pending"])
