import unittest

from server import Handler, snapshot
from workflow import issue_data, make_issue, screen, seed
from workspace import Forbidden, LOCAL


def line(text, top, left=44):
    return {"kind":"text","part":"pdf","page":2,"pageHeight":800,
            "bbox":[left,top,540,top+10],"text":text}


class PdfParagraphReviewTests(unittest.TestCase):
    def test_quotation_mark_can_close_on_next_layout_line(self):
        blocks = [line("这段材料说明所谓的“AI 智能体",167),
                  line("”可以把任务分成多个步骤。",184)]
        self.assertEqual(screen(blocks, {1}), [])
        self.assertEqual(screen(blocks[:1])[0]["anchor"], 0)

    def test_matching_line_in_two_different_paragraphs_is_not_a_duplicate(self):
        common = "outlet、walls、airfoil和frontAndBack。inlet与outlet的类型为freestream。"
        blocks = [line("对多段翼型执行二维不可压缩流动模拟。边界名称包括",486,56),
                  line(common,502,56), line("frontAndBack面设为empty。",518,56),
                  line("对串列机翼执行三维不可压缩流动模拟。边界名称包括",656,56),
                  line(common,672,56), line("frontAndBack面设为wall类型。",688,56)]
        self.assertEqual(screen(blocks), [])
        repeated = screen([*blocks,line(blocks[0]["text"],740,56),line(common,756,56),
                           line("frontAndBack面设为empty。",772,56)])
        self.assertEqual([issue["repeatAnchors"] for issue in repeated],[[0,6]])

    def test_older_false_reports_disappear_without_reuploading(self):
        data = seed()
        item = data["manuscripts"][0]
        ver = item["versions"][0]
        ver["blocks"] = [line("术语“需要结合下一行一起读",40),line("”以还原排版。",56)]
        data["issues"] = [make_issue(item,ver["id"],issue_data(
            "体例用语","成对标点数量不一致","跨段引语可人工标记为例外。",index,"low",group="punctuation"))
            for index in (0,1)]
        self.assertEqual(snapshot(data)["issues"], [])
        self.assertEqual(len(data["issues"]), 2)

    def test_trash_hides_manuscript_and_restores_its_previous_state(self):
        data = seed()
        item = data["manuscripts"][0]
        item["status"] = "paused"
        handler = Handler.__new__(Handler)
        route = f"/api/manuscripts/{item['id']}"
        handler.mutate(data,route+"/trash",{},LOCAL)
        view = snapshot(data,LOCAL)
        self.assertFalse(any(row["id"]==item["id"] for row in view["manuscripts"]))
        self.assertFalse(any(row["manuscript"]==item["id"] for row in view["issues"]))
        self.assertEqual(view["trash"][0]["id"],item["id"])
        with self.assertRaises(Forbidden):
            handler.project(data,item["id"],LOCAL)
        handler.mutate(data,route+"/restore",{},LOCAL)
        self.assertEqual(handler.project(data,item["id"],LOCAL)["status"], "paused")
