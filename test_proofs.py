"""Focused checks for layout, images, and accompanying data."""
from io import BytesIO
from shutil import which
import struct
import unittest
from zipfile import ZipFile
from zlib import compress

from proofs import ProofError, inspect_proof, parse_pdf, parse_spreadsheet, render_pdf_page


def sample_pdf():
    def stream(data, attributes=b""):
        return b"<< /Length " + str(len(data)).encode() + b" " + attributes + b" >>\nstream\n" + data + b"\nendstream"

    image = compress(bytes(150 * 80 * 3))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 3 /Kids [3 0 R 4 0 R 5 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 500] /Resources << /Font << /F1 6 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 500] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 500] /Resources << /Font << /F1 6 0 R >> /XObject << /Im1 9 0 R >> >> /Contents 8 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        stream(b"BT /F1 12 Tf 50 420 Td (See Figure 1.) Tj ET"),
        stream(b"q 144 0 0 72 50 300 cm /Im1 Do Q BT /F1 12 Tf 50 280 Td (Figure 1 Test photograph) Tj ET"),
        stream(image, b"/Type /XObject /Subtype /Image /Width 150 /Height 80 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode"),
    ]
    contents = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(contents))
        contents.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(contents)
    contents.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    contents.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    contents.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(contents)


class ProofTests(unittest.TestCase):
    @unittest.skipUnless(which("pdftotext") and which("pdfimages"), "Poppler is not on PATH")
    def test_pdf_location_distance_and_ppi(self):
        blocks = parse_pdf(sample_pdf())
        mention = next(block for block in blocks if "See Figure 1." in block["text"])
        self.assertEqual(mention["page"], 1)
        self.assertAlmostEqual(mention["bbox"][0], 50)
        image = next(image for block in blocks for image in block.get("images", []))
        self.assertAlmostEqual(image["xPpi"], 75)
        self.assertAlmostEqual(image["yPpi"], 80)
        if which("pdftohtml"):
            self.assertEqual(image["bbox"], [50.0, 128.0, 194.0, 200.0])
        issues = inspect_proof(blocks, name="proof.pdf")
        self.assertTrue(any("页距" in issue["title"] for issue in issues))
        self.assertTrue(any("分辨率" in issue["title"] for issue in issues))

    @unittest.skipUnless(which("pdftoppm"), "Poppler is not on PATH")
    def test_pdf_page_render_matches_page_aspect_ratio(self):
        png = render_pdf_page(sample_pdf(), 3)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(struct.unpack(">II", png[16:24]), (1120, 1400))
        with self.assertRaises(ProofError):
            render_pdf_page(sample_pdf(), 0)

    def test_word_raster_dimensions_use_placement(self):
        content = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <w:body><w:p><w:r><w:drawing><wp:inline><wp:extent cx="914400" cy="457200"/>
            <a:blip r:embed="rId1"/></wp:inline></w:drawing></w:r></w:p></w:body></w:document>"""
        stream = BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", content)
            archive.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="media/image1.png"/></Relationships>')
            archive.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 200, 100) + b"\x08")
        blocks = [{"text": "[图像]", "kind": "image", "part": "document"}]
        issues = inspect_proof(blocks, name="sample.docx", raw=stream.getvalue())
        self.assertIn("200.0 × 200.0 PPI", issues[0]["detail"])
        self.assertEqual(issues[0]["anchor"], 0)
        inspect_proof(blocks, name="sample.docx", raw=stream.getvalue())
        self.assertEqual(len(blocks[0]["images"]), 1)

    def test_word_table_recalculates_without_image_relationships(self):
        from workflow import parse_document

        rows = [
            ("项目", "数量"), ("甲", "10"), ("乙", "15"), ("合计", "30"),
        ]
        row_xml = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>"
                                            for cell in row) + "</w:tr>" for row in rows)
        stream = BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                             "<w:body><w:tbl>" + row_xml + "</w:tbl></w:body></w:document>")
        raw = stream.getvalue()
        blocks = parse_document("sample.docx", raw)
        issues = inspect_proof(blocks, name="sample.docx", raw=raw)
        summary = blocks[issues[0]["anchor"]]
        self.assertEqual(summary["tableRow"], 4)
        self.assertEqual(summary["cells"], ["合计", "30"])
        self.assertEqual(summary["calculations"][0]["expected"], "25")

    def test_figure_revision_rechecks_all_body_mentions(self):
        def figure_version(caption):
            return [{"text": text, "kind": "text", "location": "正文"} for text in [
                "见图 2-1。", caption, "数值见图 2-1。",
            ]]
        before = figure_version("图 2-1　原始趋势")
        after = figure_version("图 2-1　修订趋势")
        issues = inspect_proof(after, name="revision.docx", affected={1}, prior_blocks=before)
        change = next(issue for issue in issues if "版本有变化" in issue["title"])
        self.assertEqual([item["anchor"] for item in change["evidence"] if "anchor" in item], [1, 0, 2])

    def test_map_and_third_party_are_review_candidates(self):
        blocks = [
            {"text": "图 2-4　行政区划地图", "location": "正文", "kind": "text"},
            {"text": "来源：某研究机构，转载自专题报告", "location": "正文", "kind": "text"},
        ]
        issues = inspect_proof(blocks, name="sample.docx")
        self.assertEqual({issue["category"] for issue in issues}, {"地图审核", "图片授权"})
        self.assertTrue(all(not issue["blocking"] for issue in issues))

    def test_csv_recalculates_explicit_totals_without_summing_rates(self):
        sample = "项目,原始量,占比\n甲,10,40%\n乙,15,60%\n合计,30,100%\n".encode()
        blocks = parse_spreadsheet("sample.csv", sample)
        calc = blocks[-1]["calculations"]
        self.assertEqual(len(calc), 1)
        self.assertEqual(calc[0]["expected"], "25")
        self.assertEqual(calc[0]["observed"], "30")
        self.assertEqual(calc[0]["status"], "conflict")
        self.assertEqual(inspect_proof(blocks, name="sample.csv")[0]["anchor"], 3)

    def test_xlsx_calculates_cached_sum_from_source_cells(self):
        stream = BytesIO()
        worksheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
            <row r="1"><c r="A1"><v>0.1</v></c></row>
            <row r="2"><c r="A2"><v>0.2</v></c></row>
            <row r="3"><c r="A3"><f>SUM(A1:A2)</f><v>0.4</v></c></row>
            </sheetData></worksheet>"""
        with ZipFile(stream, "w") as archive:
            archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Source" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        blocks = parse_spreadsheet("sample.xlsx", stream.getvalue())
        self.assertEqual(blocks[-1]["calculations"][0]["expected"], "0.3")
        self.assertEqual(blocks[-1]["location"], "Source · 第 3 行")


if __name__ == "__main__":
    unittest.main()
