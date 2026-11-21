import unittest

from editorial_checks import affected_by_changes, check_blocks, citation_alignment


def blocks(*lines):
    return [{"text": line, "kind": "text", "location": "正文"} for line in lines]


class EditorialChecksTests(unittest.TestCase):
    def test_language_and_unit_findings_include_anchored_context(self):
        issues = check_blocks(blocks(
            "既使存在误差，也应按步就班地处理。其质量为12kg。",
            "该装置重12公斤。", "通过调整参数，使设备更稳定。",
        ))
        titles = " ".join(item["title"] for item in issues)
        self.assertIn("既使", titles)
        self.assertIn("按步就班", titles)
        self.assertIn("单位写法不一致", titles)
        self.assertIn("数值与单位", titles)
        self.assertIn("主语待核对", titles)
        self.assertTrue(all(item["evidence"][0]["anchor"] == item["anchor"] for item in issues))
        self.assertEqual(check_blocks(blocks("示例代码：参数为12kg。", "引例：“既使如此”")), [])
        self.assertEqual(check_blocks(blocks("该团队既使用了机床，也补充了原料。")), [])

    def test_numbering_uses_observed_groups_and_anchors(self):
        issues = check_blocks(blocks("第一章 概述", "第三章 分析",
                                     "图 3-1  概况", "图 3-3  构造",
                                     "表 3-1：口径", "表 3-3：统计",
                                     "公式（3-1）", "公式（3-3）"))
        issues = [item for item in issues if item["category"] == "编号检查"]
        self.assertEqual(len(issues), 4)
        self.assertEqual({item["anchor"] for item in issues}, {1, 3, 5, 7})
        ordered = check_blocks(blocks("图 3-1 概况", "图 3-3 构造", "图 3-2 分区"))
        self.assertEqual([item["title"] for item in ordered], ["图编号顺序待核对"])

    def test_text_headings_define_chapter_boundaries_for_caption_and_style(self):
        source = blocks(
            "第一章 概述", "图1：生产流程",
            "随着生产技术的发展，现场的质量管理越来越重要。",
            "第二章 分析", "图1：行业统计",
            "随着统计方法的发展，班组的反馈效率越来越重要。",
            "第三章 讨论", "图1：产品分析",
            "随着生产规模的发展，部门的质量改进越来越重要。",
        )
        issues = check_blocks(source)
        self.assertEqual([item["title"] for item in issues], ["跨章段落沿用同一表述模板"])

    def test_reference_alignment_ranges_and_duplicate_numbers(self):
        source = blocks("已有论述见［１，３－４］。", "参考文献", "[1] 甲著。",
                        "[3] 乙著。", "[3] 丙著。", "[5] 丁著。")
        issues = check_blocks(source)
        self.assertEqual([item["anchor"] for item in issues], [0, 4, 5])
        self.assertIn("[4]", issues[0]["detail"])
        self.assertIn("重复", issues[1]["title"])
        self.assertIn("未见对应", issues[2]["title"])
        self.assertTrue(all(item["stage"] == 2 and not item["blocking"] for item in issues))

    def test_citation_alignment_restricts_targets_to_the_relevant_chapter(self):
        source = blocks("第一章 概述", "参见[1, 2]。", "参考文献", "[1] 第一章资料。",
                        "第二章 讨论", "文献[1]另有说明。", "参考文献", "[1] 第二章资料。", "[1] 同号资料。")
        alignment = citation_alignment(source)
        self.assertEqual(alignment["total"], 2)
        first, second = alignment["citations"]
        self.assertEqual(first["anchor"], 1)
        self.assertEqual(first["matches"][0]["targets"][0]["anchor"], 3)
        self.assertEqual(first["matches"][1]["targets"], [])
        self.assertEqual([target["anchor"] for target in second["matches"][0]["targets"]], [7, 8])

    def test_reference_removal_rechecks_unchanged_body(self):
        source = blocks("有关论述见[1，2]。", "参考文献", "[1] 甲著。")
        changes = [{"kind": "删除", "before": ["[2] 乙著。"], "after": [], "anchor": None}]
        impacted = affected_by_changes(source, changes, set())
        self.assertIn(0, impacted)
        self.assertEqual(check_blocks(source, set()), [])
        self.assertIn("[2]", check_blocks(source, impacted)[0]["detail"])

    def test_inline_removal_rechecks_unused_number_and_footnotes_count(self):
        source = blocks("文中已有说明[1]。", "参考文献", "[1] 甲著。", "[2] 乙著。")
        changes = [{"kind": "删除", "before": ["详见[2]。"], "after": [], "anchor": 1}]
        impacted = affected_by_changes(source, changes, set())
        self.assertIn(3, impacted)
        self.assertEqual(check_blocks(source, impacted)[0]["title"], "文末条目未见对应正文引用")
        source.append({"text": "脚注中引用文献[2]。", "kind": "footnote", "location": "脚注 1"})
        self.assertEqual(check_blocks(source), [])

    def test_explicit_year_sequence_detects_reversal_with_units(self):
        source = blocks("2022 年、2023 年和 2024 年的缺陷率分别为 4.2%、3.5% 和 3.8%。",
                        "因此，缺陷率呈逐年下降趋势。")
        issues = [item for item in check_blocks(source, {0}) if item["category"] == "数据矛盾"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["anchor"], 1)
        self.assertIn("0.3 个百分点", issues[0]["detail"])
        self.assertIn(0, [evidence["anchor"] for evidence in issues[0]["evidence"]])
        valid = blocks(source[0]["text"], "因此，缺陷率总体下降，但并非逐年下降。")
        self.assertFalse(any(item["category"] == "数据矛盾" for item in check_blocks(valid)))
        different = blocks(source[0]["text"], "产品的合格率呈逐年下降趋势。")
        self.assertFalse(any(item["category"] == "数据矛盾" for item in check_blocks(different)))

    def test_linked_year_statements_and_implicit_trend_subject(self):
        text = "合格率在2022年为4.2%、2023年为3.5%、2024年为3.8%，呈逐年下降。"
        issues = check_blocks(blocks(text))
        self.assertEqual(len(issues), 1)
        self.assertIn("0.3 个百分点", issues[0]["detail"])
        for line in (
            "2022至2024年合格率分别为4.2%、3.5%、3.8%。不合格率呈逐年下降趋势。",
            "2022至2024年合格率分别为4.2%、3.5%、3.8%。合格率逐年下降的说法不成立。",
        ):
            self.assertEqual(check_blocks(blocks(line)), [])

    def test_comparable_claims_require_same_object_year_metric_unit(self):
        result = check_blocks(blocks(
            "甲车间2024年的缺陷率为3.8%。", "乙车间2024年的缺陷率为3.5%。",
            "甲车间2023年的缺陷率为3.5%。", "甲车间2024年的缺陷率为3.5%。",
        ))
        contradictions = [item for item in result if item["category"] == "数据矛盾"]
        self.assertEqual(len(contradictions), 1)
        self.assertEqual(contradictions[0]["anchor"], 3)
        self.assertEqual(contradictions[0]["evidence"][1]["anchor"], 0)

    def test_citation_structure_is_advisory(self):
        result = check_blocks(blocks(
            "正文引用见[1-5]。", "参考文献",
            "[1] 张明. 题名甲[J]. 质量研究, 2020.",
            "[2] 张明. 题名乙[J]. 质量研究, 2021.",
            "[3] 张明. 题名丙[J]. 质量研究, 2022.",
            "[4] 李芳. 题名丁[J]. 应用统计, 2023.",
            "[5] 张明明. 题名戊[J]. 应用统计, 2024.",
        ), author="张明", watchlist=[{"title": "质量研究", "version": "2026 试点名录"}])
        details = " ".join(item["detail"] for item in result)
        titles = " ".join(item["title"] for item in result)
        self.assertIn("2026 试点名录", details)
        self.assertIn("同名作者", titles)
        self.assertIn("期刊来源集中", titles)
        self.assertFalse(any(item["blocking"] for item in result))


if __name__ == "__main__":
    unittest.main()
