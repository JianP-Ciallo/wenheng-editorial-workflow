import unittest

from editorial_checks import check_blocks, citation_alignment
from server import snapshot
from workflow import issue_data, make_issue, screen, seed


def pdf(text, page=1, top=100):
    return {"part":"pdf","kind":"text","text":text,"page":page,"pageHeight":800,
            "bbox":[70,top,500,top+10],"location":f"第 {page} 页"}


class PdfBibliographyTests(unittest.TestCase):
    def test_double_columns_in_extraction_order_and_doi_without_period(self):
        def column(text,page,top,left):
            block = pdf(text,page,top)
            block.update(pageWidth=600,bbox=[left,top,left+240,top+20])
            return block

        blocks = [
            column("Prior work evaluates several engineering tasks [1–6] and [8].",1,120,36),
            column("[4] Chen, Model evaluation, 2024. https://doi.org/10.1000/alpha.1 "
                   "[5] Liu, Mesh studies, 2025. https://doi.org/10.1000/beta.2 "
                   "[6] Gao, Test data, 2026.",14,55,307),
            column("References",14,533,36),
            column("[1] Soori, Robotics, 2023, https://doi.org/10.1000/ref.1 "
                   "[2] Pryamikov, Signal processing, 2024, https://doi.org/10.1000/ref.2 "
                   "[3] Lei, Fault diagnosis, 2019.",14,550,36),
        ]
        row = citation_alignment(blocks)["citations"][0]
        hits = {ref["number"]:ref["targets"] for ref in row["matches"]}
        self.assertEqual([hits[n][0]["anchor"] for n in range(1,7)],[3,3,3,1,1,1])
        self.assertEqual(hits[8],[])
        self.assertTrue(hits[2][0]["text"].startswith("[2] Pryamikov"))
        self.assertNotIn("[3]",hits[2][0]["text"])
        data = seed()
        item = data["manuscripts"][0]
        version = item["versions"][0]
        version["blocks"] = blocks
        data["issues"] = [make_issue(item,version["id"],issue_data(
            "引文核验","正文引文缺少对应条目",f"正文引用 {numbers} 未在可解析的参考文献表中找到。",anchor))
            for anchor,numbers in [(0,"[2]、[8]"),(1,"[4]、[5]、[6]")]]
        remaining = snapshot(data)["issues"]
        self.assertEqual([issue["anchor"] for issue in remaining],[0])
        self.assertIn("[8]",remaining[0]["detail"])
        self.assertNotIn("[2]",remaining[0]["detail"])

    def test_bracketed_number_inside_title_is_not_a_reference_entry(self):
        blocks = [pdf("Comparison draws on [1] and [2]."),pdf("References",2),
                  pdf("[1] Zhang, Experimental comparison [2] and failure modeling, Journal of Mechanics, 2024. "
                      "[2] Wang, Engineering methods, 2025.",2)]
        refs = citation_alignment(blocks)["citations"][0]["matches"]
        self.assertEqual([ref["targetCount"] for ref in refs],[1,1])
        self.assertTrue(refs[1]["targets"][0]["text"].startswith("[2] Wang"))

    def test_multiple_references_in_a_pdf_block_and_two_pages(self):
        blocks = [pdf("Test cases cover several prior approaches [1,2], [4–5], and [7]."),
                  pdf("References",27),
                  pdf("[1] Zhang, CFD methods, 2001. [2] Wang, Wing design, 2002. [3] Li, Flow separation, 2003.",27),
                  pdf("[4] Zhao, Offshore wind, 2004. [5] Chen, Wind turbine dynamics, 2005.",28)]
        alignment = citation_alignment(blocks)
        refs = {row["number"]:row["targets"] for row in alignment["citations"][0]["matches"]}
        self.assertEqual(refs[2][0]["text"],"[2] Wang, Wing design, 2002.")
        self.assertEqual(refs[5][0]["anchor"],3)
        self.assertEqual(refs[7],[])
        gaps = [finding["detail"] for finding in check_blocks(blocks) if finding["title"]=="正文引文缺少对应条目"]
        self.assertEqual(len(gaps),1)
        self.assertIn("[7]",gaps[0])
        self.assertNotIn("[2]",gaps[0])

    def test_existing_false_reports_do_not_reappear(self):
        data = seed()
        item = data["manuscripts"][0]
        version = item["versions"][0]
        version["blocks"] = [pdf("Prior work supports this observation [2]."),pdf("References",27),
                             pdf("[1] Zhang, CFD methods, 2001. [2] Wang, Wing design, 2002.",27),
                             pdf("Example User Requirement",22),pdf("Example User Requirement",23)]
        data["issues"] = [make_issue(item,version["id"],issue_data(
            "引文核验","正文引文缺少对应条目","正文引用 [2] 未在对应的可解析参考文献表中找到。",0)),
            make_issue(item,version["id"],issue_data("内容重复","正文存在相同段落","核对各处语境",4,
                                                    evidence=[{"label":"正文","anchor":3,"text":"Example User Requirement"}]))]
        self.assertFalse(snapshot(data)["issues"])
        self.assertFalse(any(issue["category"]=="内容重复" for issue in screen(version["blocks"])))
