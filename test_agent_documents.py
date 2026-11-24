import unittest
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from agent_documents import revise_docx, suggest_fixes, text_docx
from workflow import W, WorkflowError, _w, annotate_docx, parse_document


def word(body):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml",
                         f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        archive.writestr("[Content_Types].xml",
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/media/fixture.bin", b"untouched package member")
    return output.getvalue()


def root(raw, path="word/document.xml"):
    with ZipFile(BytesIO(raw)) as archive:
        return ET.fromstring(archive.read(path))


class AgentDocumentsTests(unittest.TestCase):
    def test_tracked_and_clean_versions_preserve_format_and_comment_anchors(self):
        raw = word('<w:p><w:r><w:t>正文开头。</w:t></w:r></w:p>'
                   '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>设备重 25 KG，净重24千克。</w:t></w:r>'
                   '<w:r><w:t>右侧文字。</w:t></w:r></w:p>'
                   '<w:p><w:ins w:id="3" w:author="Editor"><w:r><w:t>既有修订。</w:t></w:r></w:ins></w:p>')
        blocks = parse_document("sample.docx", raw)
        fixes = suggest_fixes(raw, blocks)
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["anchor"], 1)
        self.assertEqual(fixes[0]["before"], blocks[1]["text"])
        self.assertEqual(fixes[0]["after"], "设备重 25 kg，净重24 kg。右侧文字。")
        self.assertEqual(fixes[0]["count"], 2)
        tracked = revise_docx(raw, fixes, tracked=True)
        clean = revise_docx(raw, fixes, tracked=False)
        edited = root(tracked)
        inserted = list(edited.iter(_w("ins")))
        deleted = list(edited.iter(_w("del")))
        self.assertEqual([node.text for node in edited.iter(_w("delText"))], [" KG", "千克"])
        self.assertEqual([node.text for node in edited.iter(_w("t")) if node.text == " kg"], [" kg", " kg"])
        self.assertEqual(len({node.get(_w("id")) for node in inserted + deleted}), 5)
        for revision in inserted + deleted:
            if revision.get(_w("id")) != "3":
                self.assertIsNotNone(revision.find(f"{_w('r')}/{_w('rPr')}/{_w('b')}"))
        self.assertEqual([node.get(_w("id")) for node in root(clean).iter(_w("ins"))], ["3"])
        for generated in (tracked, clean):
            self.assertEqual(parse_document("sample.docx", generated)[1]["text"], fixes[0]["after"])
            with ZipFile(BytesIO(generated)) as archive:
                self.assertEqual(archive.read("word/media/fixture.bin"), b"untouched package member")
        issue = {"title": "稿件数据需说明", "detail": "请核对原始称重数据", "anchor": 1, "state": "pending"}
        marked = annotate_docx(tracked, [issue])
        commented = list(root(marked).iter(_w("p")))[1]
        self.assertIsNotNone(commented.find(_w("commentRangeStart")))
        self.assertIn("稿件数据需说明", "".join(root(marked, "word/comments.xml").itertext()))
        self.assertEqual(parse_document("sample.docx", raw), blocks)

    def test_unsafe_passages_and_stale_fixes(self):
        raw = word('<w:p><w:r><w:t>空箱重10kg。</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>称重 25 </w:t></w:r><w:r><w:t>KG。</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>面密度为 25 KG</w:t></w:r><w:r><w:t>/m²。</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>铭牌写着“25KG”。</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>本次承载 5千克力。标识 AB-25 KG。</w:t></w:r></w:p>'
                   '<w:p><w:hyperlink><w:r><w:t>25 KG</w:t></w:r></w:hyperlink></w:p>'
                   '<w:p><w:r><w:t>设备重 25 KG</w:t><w:footnoteReference w:id="2"/></w:r></w:p>'
                   '<w:p><w:r><w:t>参考文献</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>[1] Technical dataset 25 KG.</w:t></w:r></w:p>')
        fixes = suggest_fixes(raw, parse_document("sample.docx", raw))
        self.assertEqual([fix["anchor"] for fix in fixes], [0])
        self.assertEqual(fixes[0]["after"], "空箱重10 kg。")
        with self.assertRaises(WorkflowError):
            revise_docx(raw, [{**fixes[0], "after": "空箱重11 kg。"}], tracked=True)
        with self.assertRaises(WorkflowError):
            revise_docx(raw, fixes + fixes, tracked=False)
        with self.assertRaises(WorkflowError):
            revise_docx(raw, [{"anchor": 200, "before": None, "after": None}], tracked=True)

    def test_report_is_openable_ooxml_and_preserves_paragraphs(self):
        raw = text_docx("来稿补正说明", ["请核对第 3 段的引文。\n请附适用依据。", "谢谢。"])
        with ZipFile(BytesIO(raw)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("_rels/.rels", archive.namelist())
            self.assertIn("word/_rels/document.xml.rels", archive.namelist())
        blocks = parse_document("report.docx", raw)
        self.assertEqual([block["text"] for block in blocks],
                         ["来稿补正说明", "请核对第 3 段的引文。", "请附适用依据。", "谢谢。"])
        self.assertIsNotNone(root(raw, "word/styles.xml").find(f"{_w('style')}[@{_w('styleId')}='Title']"))
