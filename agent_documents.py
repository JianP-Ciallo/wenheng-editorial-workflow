"""Conservative Word corrections and editable review documents."""
from bisect import bisect_right
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
from itertools import count
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from editorial_checks import NUMBER, _reference_sections
from workflow import C, R, REL, WorkflowError, _paragraphs, _serialize_xml, _w, _xml

DOCUMENT = "word/document.xml"
SPACE = "{http://www.w3.org/XML/1998/namespace}space"
WEIGHT = re.compile(
    rf"(?<![A-Za-z0-9_./+\-]){NUMBER}(?P<unit>[ \u00a0]*(?:kg|KG|Kg|千克|公斤))"
    r"(?![ \u00a0]*[/*·×^²³⁻⁺])(?=$|[ \u00a0、，。；：！？,.;:!?）)\]])"
)
QUOTATION = re.compile(r"""["'“”‘’「」『』《》]|https?://|`""", re.I)
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


@contextmanager
def _manuscript(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            if sum(member.file_size for member in archive.infolist()) > 120 * 1024 * 1024:
                raise WorkflowError("Word 内部文件超过解压上限。")
            document = _xml(archive, DOCUMENT)
            body = document.find(_w("body"))
            if body is None:
                raise WorkflowError("Word 文件缺少正文结构。")
            yield archive, document, _paragraphs(body, "正文", "document")
    except (BadZipFile, KeyError, ET.ParseError, RuntimeError) as exc:
        raise WorkflowError("无法生成 Word 修订件。请核对原件。") from exc


def _plain_run(run):
    return run.tag == _w("r") and [child.tag for child in run] in (
        [_w("t")], [_w("rPr"), _w("t")]) and run.find(f".//{_w('rPrChange')}") is None


def _paragraph_edits(paragraph):
    """Find quantity spans in the whole paragraph, then edit within one run only."""
    if paragraph.find(f".//{_w('pPrChange')}") is not None:
        return {}
    style = paragraph.find(f"{_w('pPr')}/{_w('pStyle')}")
    if style is not None and re.search(r"code|caption|代码|题注", style.get(_w("val"), ""), re.I):
        return {}
    runs = [child for child in paragraph if child.tag != _w("pPr")]
    if not all(_plain_run(run) for run in runs):
        return {}
    chunks = [run.find(_w("t")).text or "" for run in runs]
    text = "".join(chunks)
    if QUOTATION.search(text):
        return {}
    offsets = []
    offset = 0
    for chunk in chunks:
        offsets.append(offset)
        offset += len(chunk)
    edits = {}
    for match in WEIGHT.finditer(text):
        if match["unit"] == " kg":
            continue
        start, end = match.span("unit")
        number_run = bisect_right(offsets, match.start()) - 1
        base = offsets[number_run]
        if end <= base + len(chunks[number_run]):
            edits.setdefault(runs[number_run], []).append((start - base, end - base, " kg"))
    return edits


def _edited_text(text, edits):
    parts = []
    cursor = 0
    for start, end, replacement in edits:
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _fix_plans(nodes, anchors=None):
    blocks = [block for _, block in nodes]
    for anchor in _reference_sections(blocks)[3]:
        if anchors is not None and anchor not in anchors:
            continue
        paragraph, block = nodes[anchor]
        if block["kind"] != "text" or not (edits := _paragraph_edits(paragraph)):
            continue
        new_text = []
        for run in paragraph.findall(_w("r")):
            text = run.find(_w("t")).text or ""
            new_text.append(_edited_text(text, edits.get(run, ())))
        yield anchor, {
            "anchor": anchor, "before": block["text"], "after": "".join(new_text).strip(),
            "reason": "按阿拉伯数字后的质量单位体例统一为 kg，保留原数值。",
            "count": sum(len(changes) for changes in edits.values()),
        }, edits


def suggest_fixes(raw, blocks):
    """Return suggested changes tied to the stored source paragraph indexes."""
    with _manuscript(raw) as (_, _, nodes):
        return [fix for anchor, fix, _ in _fix_plans(nodes)
                if anchor < len(blocks) and blocks[anchor]["text"] == fix["before"]
                and blocks[anchor]["kind"] == "text"]


def _run_with_text(run, text, *, deleted=False):
    copied = deepcopy(run)
    content = copied.find(_w("t"))
    content.text = text
    if deleted:
        content.tag = _w("delText")
    if text and (text[0].isspace() or text[-1].isspace()):
        content.set(SPACE, "preserve")
    return copied


def _revision(tag, item, revision_id):
    node = ET.Element(_w(tag), {_w("id"): str(revision_id), _w("author"): "文衡编辑台"})
    node.append(item)
    return node


def _tracked_runs(run, edits, revision_ids):
    text = run.find(_w("t")).text or ""
    cursor = 0
    for start, end, replacement in edits:
        if start > cursor:
            yield _run_with_text(run, text[cursor:start])
        yield _revision("del", _run_with_text(run, text[start:end], deleted=True), next(revision_ids))
        yield _revision("ins", _run_with_text(run, replacement), next(revision_ids))
        cursor = end
    if cursor < len(text):
        yield _run_with_text(run, text[cursor:])


def revise_docx(raw, fixes, *, tracked):
    """Apply the verified edit plan to a copy of the manuscript package."""
    if not fixes:
        return raw
    anchors = [fix.get("anchor") for fix in fixes]
    if any(type(anchor) is not int or anchor < 0 for anchor in anchors) or len(anchors) != len(set(anchors)):
        raise WorkflowError("Word 修订定位无效。")
    with _manuscript(raw) as (archive, document, nodes):
        plans = {anchor: (fix, edits) for anchor, fix, edits in _fix_plans(nodes, set(anchors))}
        used = {int(value) for node in document.iter()
                if (value := node.get(_w("id"), "")).isascii() and value.isdecimal()}
        revision_ids = (value for value in count() if value not in used)
        for fix in fixes:
            anchor = fix["anchor"]
            if anchor not in plans:
                raise WorkflowError("Word 修订定位已失效，请重新运行智能审校。")
            planned, edits = plans[anchor]
            if any(field not in fix or fix[field] != planned.get(field) for field in ("before", "after")):
                raise WorkflowError("原件与审校结果不一致，请重新运行智能审校。")
            paragraph = nodes[anchor][0]
            updated = []
            for run in paragraph:
                if run not in edits:
                    updated.append(run)
                elif tracked:
                    updated.extend(_tracked_runs(run, edits[run], revision_ids))
                else:
                    text = run.find(_w("t")).text or ""
                    updated.append(_run_with_text(run, _edited_text(text, edits[run])))
            paragraph[:] = updated
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as result:
            for entry in archive.infolist():
                original = archive.read(entry)
                result.writestr(entry, _serialize_xml(document, original) if entry.filename == DOCUMENT else original)
    return output.getvalue()


def _report_paragraph(body, text, style=None):
    paragraph = ET.SubElement(body, _w("p"))
    if style:
        properties = ET.SubElement(paragraph, _w("pPr"))
        ET.SubElement(properties, _w("pStyle"), {_w("val"): style})
    run = ET.SubElement(paragraph, _w("r"))
    ET.SubElement(run, _w("t"), {SPACE: "preserve"}).text = INVALID_XML.sub("", text)


def text_docx(title, paragraphs):
    """Build a Word document for a supplement request or decision record."""
    document = ET.Element(_w("document"))
    body = ET.SubElement(document, _w("body"))
    _report_paragraph(body, title, "Title")
    for paragraph in paragraphs:
        for line in paragraph.splitlines() or [""]:
            _report_paragraph(body, line)
    section = ET.SubElement(body, _w("sectPr"))
    ET.SubElement(section, _w("pgSz"), {_w("w"): "11906", _w("h"): "16838"})
    ET.SubElement(section, _w("pgMar"), {_w(side): value for side, value in (
        ("top", "1440"), ("right", "1440"), ("bottom", "1440"), ("left", "1440"),
        ("header", "720"), ("footer", "720"), ("gutter", "0"))})

    styles = ET.Element(_w("styles"))
    normal = ET.SubElement(styles, _w("style"), {_w("type"): "paragraph", _w("default"): "1", _w("styleId"): "Normal"})
    ET.SubElement(normal, _w("name"), {_w("val"): "Normal"})
    paragraph_format = ET.SubElement(normal, _w("pPr"))
    ET.SubElement(paragraph_format, _w("spacing"), {_w("after"): "120", _w("line"): "300", _w("lineRule"): "auto"})
    normal_run = ET.SubElement(normal, _w("rPr"))
    ET.SubElement(normal_run, _w("rFonts"), {_w(key): "Microsoft YaHei" for key in ("ascii", "hAnsi", "eastAsia")})
    ET.SubElement(normal_run, _w("sz"), {_w("val"): "22"})
    heading = ET.SubElement(styles, _w("style"), {_w("type"): "paragraph", _w("styleId"): "Title"})
    ET.SubElement(heading, _w("name"), {_w("val"): "Title"})
    ET.SubElement(heading, _w("basedOn"), {_w("val"): "Normal"})
    heading_format = ET.SubElement(heading, _w("pPr"))
    ET.SubElement(heading_format, _w("keepNext"))
    ET.SubElement(heading_format, _w("spacing"), {_w("after"): "280"})
    heading_run = ET.SubElement(heading, _w("rPr"))
    ET.SubElement(heading_run, _w("b"))
    ET.SubElement(heading_run, _w("color"), {_w("val"): "000000"})
    ET.SubElement(heading_run, _w("sz"), {_w("val"): "36"})

    content_types = ET.Element(f"{{{C}}}Types")
    for extension, mime in (("rels", "application/vnd.openxmlformats-package.relationships+xml"),
                            ("xml", "application/xml")):
        ET.SubElement(content_types, f"{{{C}}}Default", Extension=extension, ContentType=mime)
    for path, tag in ((DOCUMENT, "document.main"), ("word/styles.xml", "styles")):
        ET.SubElement(content_types, f"{{{C}}}Override", PartName="/" + path,
                      ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{tag}+xml")
    package_rels = ET.Element(f"{{{R}}}Relationships")
    ET.SubElement(package_rels, f"{{{R}}}Relationship", Id="rId1", Type=REL + "officeDocument", Target=DOCUMENT)
    document_rels = ET.Element(f"{{{R}}}Relationships")
    ET.SubElement(document_rels, f"{{{R}}}Relationship", Id="rId1", Type=REL + "styles", Target="styles.xml")
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, content in ((DOCUMENT, document), ("word/styles.xml", styles), ("[Content_Types].xml", content_types),
                              ("_rels/.rels", package_rels), ("word/_rels/document.xml.rels", document_rels)):
            archive.writestr(path, _serialize_xml(content, b""))
    return output.getvalue()
