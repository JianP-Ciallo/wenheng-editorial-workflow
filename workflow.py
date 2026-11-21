"""Local manuscript workflow, structured Word parsing and evidence rules."""
from datetime import date, datetime
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
import re
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr
from proofs import parse_pdf, ProofError

STAGES = [
    ("收稿初筛", "整理原始文件，检查材料、重复段落与用语。"),
    ("选题甄别", "对照同类书目、样章和读者需求，判断选题差异。"),
    ("专业核验", "检查条款、图文一致性、引文身份与权限。"),
    ("学术评估", "记录研究问题、实际增量及证据，形成送审意见。"),
    ("返修签核", "对比稿件版本，处置未结事项，完成签核归档。"),
]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
ET.register_namespace("w", W)
ET.register_namespace("m", M)

class WorkflowError(ValueError):
    pass

def new_id():
    return uuid.uuid4().hex[:14]

def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def _w(name):
    return f"{{{W}}}{name}"

def _xml(archive, name):
    info = archive.getinfo(name)
    if info.file_size > 45 * 1024 * 1024:
        raise WorkflowError("Word 内部 XML 超过解析上限。")
    raw = archive.read(info)
    for event, item in ET.iterparse(BytesIO(raw), events=("start-ns",)):
        prefix, uri = item
        if prefix and not re.fullmatch(r"ns\d+", prefix):
            ET.register_namespace(prefix, uri)
    return ET.fromstring(raw)

def _formula_latex(node):
    def children(element, slot):
        branch = element.find(f"{{{M}}}{slot}")
        return convert(branch) if branch is not None else ""
    def convert(element):
        name = element.tag.rsplit("}",1)[-1]
        if name.endswith("Pr"):
            return ""
        if name == "t":
            return (element.text or "").replace("%",r"\%")
        if name == "f":
            return r"\frac{" + children(element,"num") + "}{" + children(element,"den") + "}"
        if name in {"sSub","sSup","sSubSup"}:
            base = children(element,"e")
            if (sub := children(element,"sub")):
                base += "_{" + sub + "}"
            if (sup := children(element,"sup")):
                base += "^{" + sup + "}"
            return base
        if name == "rad":
            degree = children(element,"deg")
            return r"\sqrt" + (f"[{degree}]" if degree else "") + "{" + children(element,"e") + "}"
        return "".join(convert(child) for child in element)
    return convert(node)

def _paragraphs(element, location, part):
    def visit(node, in_table=False):
        if node.tag == _w("p"):
            text = "".join(child.text or "" if child.tag != _w("tab") else "\t"
                           for child in node.iter()
                           if child.tag in (_w("t"), _w("tab"), f"{{{M}}}t"))
            has_image = any(child.tag in (_w("drawing"), _w("pict")) for child in node.iter())
            notes = [child.get(_w("id")) for child in node.iter(_w("footnoteReference"))]
            style = node.find(f"{_w('pPr')}/{_w('pStyle')}")
            heading = style is not None and re.search(r"heading|标题", style.get(_w("val"), ""), re.I)
            formulas = list(node.iter(f"{{{M}}}oMath"))
            formula = bool(formulas)
            if text.strip() or has_image or notes or formula:
                kind = ("table" if in_table else "heading" if heading else "formula" if formula
                        else "image" if has_image else "footnote" if part == "footnotes" else "text")
                block = {"text": text.strip() or ("[图像]" if has_image else "[公式]"),
                         "kind": kind, "notes": notes, "location": location, "part": part}
                if formulas:
                    supported = {"oMath","r","t","f","num","den","sSub","sSup","sSubSup","e","sub","sup","rad","deg"}
                    names = {child.tag.rsplit("}",1)[-1] for formula in formulas for child in formula.iter()
                             if child.tag.startswith(f"{{{M}}}")}
                    block["latex"] = " ; ".join(_formula_latex(formula) for formula in formulas)
                    block["formulaCheck"] = any(name not in supported and not name.endswith("Pr") for name in names)
                yield node, block
            return
        for child in node:
            yield from visit(child, in_table or node.tag == _w("tbl"))
    return list(visit(element))

def _footnote_paragraphs(element):
    return [entry for note in element.findall(_w("footnote"))
            if not note.get(_w("type")) and note.get(_w("id")) not in ("-1", "0")
            for entry in _paragraphs(note, f"脚注 {note.get(_w('id'), '')}", "footnotes")]

def parse_document(name, raw):
    suffix = Path(name).suffix.lower()
    if suffix == ".txt":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise WorkflowError("请将 TXT 另存为 UTF-8 编码。") from exc
        blocks = [{"text": line.strip(), "kind": "text", "location": "正文", "part": "document"}
                  for line in text.splitlines() if line.strip()]
    elif suffix == ".docx":
        try:
            with ZipFile(BytesIO(raw)) as archive:
                if sum(member.file_size for member in archive.infolist()) > 120 * 1024 * 1024:
                    raise WorkflowError("Word 内部文件超过解压上限。")
                content = _xml(archive, "word/document.xml")
                body = content.find(_w("body"))
                if body is None:
                    raise WorkflowError("Word 文件缺少正文结构。")
                blocks = [block for _, block in _paragraphs(body, "正文", "document")]
                chapter = "正文"
                for block in blocks:
                    if block["kind"] == "heading":
                        chapter = block["text"]
                    block["location"] = chapter
                if "word/footnotes.xml" in archive.namelist():
                    notes = _xml(archive, "word/footnotes.xml")
                    back_links = {}
                    for index, block in enumerate(blocks):
                        for number in block["notes"]:
                            back_links.setdefault(number, []).append(index)
                    for _, block in _footnote_paragraphs(notes):
                        block["backLinks"] = back_links.get(block["location"].split()[-1],[])
                        blocks.append(block)
        except (BadZipFile, KeyError, ET.ParseError, RuntimeError) as exc:
            raise WorkflowError("无法读取 Word 文件。请上传未加密的 .docx 文件。") from exc
    elif suffix == ".pdf":
        try:
            return parse_pdf(raw)
        except ProofError as exc:
            if str(exc).startswith("未找到 pdftotext"):
                return [{"text":str(exc),"kind":"image","location":"校样","part":"pdf","scanNeeded":True}]
            raise WorkflowError(str(exc)) from exc
    else:
        raise WorkflowError("支持 .docx、.txt 和 .pdf 文件。")
    if not blocks:
        raise WorkflowError("文件未提取到可读正文。")
    if len(blocks) > 15000:
        raise WorkflowError("稿件段落超过解析上限，请按卷拆分。")
    return blocks

def issue_data(category, title, detail, anchor=None, severity="normal", evidence=None,
               group="", blocking=False, stage=0):
    return {"category": category, "title": title, "detail": detail, "anchor": anchor,
            "severity": severity, "evidence": evidence or [], "group": group, "blocking": blocking,
            "stage": stage}

def screen(blocks, affected=None, terms=()):
    issues = []
    seen = {}
    figures = {}
    figure_refs = []
    seen_dois = set()
    for index, block in enumerate(blocks):
        text = block["text"]
        active = affected is None or index in affected
        if block["kind"] in ("formula", "image") or re.match(r"^(?:示例代码|代码清单|\s*#include|\s*def )", text):
            continue
        normalized = re.sub(r"[\s\W]+", "", text).casefold()
        if block["kind"] == "text" and len(normalized) >= 22:
            previous = seen.setdefault(normalized, index)
            if previous != index and (active or (affected is not None and previous in affected)):
                issues.append(issue_data("内容重复", "正文存在相同段落", "核对两处语境，确认是否为有意回顾或引用。",
                                         index, "normal", [{"label": f"正文第 {previous + 1} 段", "text": blocks[previous]["text"], "anchor": previous}]))
        caption = re.match(r"^\s*图\s*([\d.－–-]+)\s*[：:、.\s]+\s*[^，。；]+$", text)
        if caption:
            figures.setdefault(caption[1], index)
        if block["kind"] != "heading":
            figure_refs.extend((ref, index) for ref in re.findall(r"图\s*([0-9]+(?:[.－–-][0-9]+)*)", text))
        if not active or block["kind"] == "heading" or re.match(r"^[“‘「『]", text):
            continue
        if any(text.count(opener) != text.count(closer) for opener, closer in ("（）", "“”", "《》", "【】")):
            issues.append(issue_data("体例用语", "成对标点数量不一致", "核对括号、引号或书名号。跨段引语可人工标记为例外。", index, "low",
                                     group="punctuation"))
        for term in terms:
            variant = term.get("variant", "")
            if term.get("type") == "term" and term.get("status") == "approved" and variant and variant in text:
                issues.append(issue_data("体例用语", f"统一用语「{variant}」", f"社内术语表建议使用「{term['title']}」。确认语义一致后处置。",
                                         index, "low", [{"label": "术语表", "text": term["title"]}], group=f"term:{term['id']}"))
        for doi in re.findall(r"10\.\d{4,9}/[^\s，。；、）》）]+", text, flags=re.I):
            doi = doi.rstrip(".;,)]").lower()
            if doi not in seen_dois:
                issues.append(issue_data("引文核验", "引文身份待核实", f"核对 DOI {doi} 对应的书目信息及作者、题名和年份。",
                                         index, "normal", [{"label": "DOI", "text": doi}], stage=2))
                seen_dois.add(doi)
    marked = set()
    for ref, index in figure_refs:
        if ref not in figures and ref not in marked and (
                affected is None or index in affected or blocks[index].get("recheckFigure", False)):
            marked.add(ref)
            issues.append(issue_data("图文核验", f"缺少图 {ref} 对应图题", "提取的正文里没有匹配的图题。请核对原图、编号与正文指代。",
                                     index, "high", stage=0))
    return issues[:250]

def compare_paragraphs(before, after):
    def key(block):
        return (block["kind"], block["location"], block["text"])
    matcher = SequenceMatcher(None, list(map(key, before)), list(map(key, after)), autojunk=False)
    changes, affected = [], set()
    for tag, start, end, replacement, replacement_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append({"kind": {"delete": "删除", "insert": "新增", "replace": "修改"}[tag],
                        "before": [entry["text"] for entry in before[start:end]],
                        "after": [entry["text"] for entry in after[replacement:replacement_end]],
                        "anchor": replacement if replacement < len(after) else None})
        affected.update(range(replacement, replacement_end))
    return changes, affected

def make_issue(manuscript, version, payload):
    return {"id": new_id(), "manuscript": manuscript["id"], "version": version,
            **payload, "state": "pending", "owner": manuscript["editor"], "created": now(),
            "confirmedAction": "", "note": ""}

def log(manuscript, label, note=""):
    manuscript.setdefault("events", []).insert(0, {"label": label, "note": note,
                                                   "time": now(), "owner": manuscript.get("editor", "责任编辑")})

def open_issue(issue):
    return issue["state"] in {"pending", "confirmed", "waiting"}

def apply_transition(manuscript, issues, action, note):
    note = note.strip()
    if not note:
        raise WorkflowError("请填写处理说明。")
    status, stage = manuscript["status"], manuscript["stage"]
    if status in {"archived", "rejected"}:
        raise WorkflowError("已结束的稿件不能继续流转。")
    blocking = any(item["blocking"] and open_issue(item) for item in issues)
    if action == "advance" and status == "active":
        if blocking:
            raise WorkflowError("请先处理阻断事项，再继续流转。")
        if stage == 4:
            if any(open_issue(item) for item in issues):
                raise WorkflowError("还有未结事项，全部处置后才能签核。")
            manuscript["status"] = "archived"
            label = "签核归档"
        else:
            if any(item.get("stage", 0) <= stage and open_issue(item) for item in issues):
                raise WorkflowError("请完成当前和前序节点的未结事项。")
            manuscript["stage"] += 1
            label = "进入" + STAGES[stage + 1][0]
    elif action == "return" and status == "active":
        if stage < 1:
            raise WorkflowError("当前已在首个节点。")
        manuscript["stage"] -= 1
        label = "退回" + STAGES[stage - 1][0]
    elif action == "request" and status == "active":
        manuscript["status"] = "revision"
        label = "待作者补件"
    elif action == "pause" and status in {"active", "revision"}:
        manuscript["status"] = "paused"
        label = "暂停加工"
    elif action == "resume" and status in {"paused", "revision"}:
        if blocking:
            raise WorkflowError("请先核实并处置阻断事项。")
        manuscript["status"] = "active"
        label = "恢复流转"
    elif action == "reject" and stage in (1, 3) and status in {"active", "paused", "revision"}:
        manuscript["status"] = "rejected"
        label = "登记选题会退稿决定"
    else:
        raise WorkflowError("当前状态无法执行该操作。")
    log(manuscript, label, note)

def decide_issues(data, ids, state, note, *, global_batch=False, actor=""):
    if state not in {"confirmed", "dismissed", "resolved", "waiting", "pending"}:
        raise WorkflowError("未知的处置结果。")
    note = note.strip()
    if state in {"dismissed", "waiting", "resolved"} and not note:
        raise WorkflowError("请填写处置依据。")
    items = [item for item in data["issues"] if item["id"] in ids]
    if not items or len(items) != len(set(ids)):
        raise WorkflowError("找不到待处置的问题。")
    first = items[0]
    owners = {item["id"]: item for item in data["manuscripts"]}
    if any(owners[item["manuscript"]]["status"] in {"archived", "rejected"} for item in items):
        raise WorkflowError("已结束稿件的处理记录只能查看。")
    group_key = (first["manuscript"], first["version"], first["group"])
    if global_batch:
        safe = first["group"] in {"punctuation","unit-spacing"} or first["group"].startswith(("term:", "unit:", "typo:"))
        if not safe or state not in {"confirmed","dismissed","resolved"} or any(
            item["group"] != first["group"] or item["severity"] != "low" or item["blocking"] or item["state"] != "pending"
            for item in items):
            raise WorkflowError("跨稿件仅支持相同规则下非阻断的低优先级待判断问题。")
    elif len(items) > 1 and (not first["group"] or any(
            (item["manuscript"], item["version"], item["group"]) != group_key for item in items)):
        raise WorkflowError("只有同一稿件版本下可合并的同类问题支持批量处置。")
    for item in items:
        item["state"], item["note"], item["updated"] = state, note, now()
        if actor:
            item["decidedBy"] = actor
    label = {"confirmed": "确认问题", "dismissed": "标记例外", "resolved": "完成处置",
             "waiting": "退回补证", "pending": "重新打开"}[state]
    for ident in {item["manuscript"] for item in items}:
        count = sum(item["manuscript"] == ident for item in items)
        log(owners[ident], f"{label} · {count} 项", note or first["title"])
        if actor:
            owners[ident]["events"][0]["owner"] = actor

def lookup_evidence(library, query, target, project, region, audience=""):
    try:
        target = date.fromisoformat(target)
    except ValueError as exc:
        raise WorkflowError("请填写有效的核验基准日。") from exc
    tokens = {value.lower() for value in re.split(r"[\s，、；,;]+", query.strip()) if value}
    if not tokens:
        raise WorkflowError("请输入待核主张或条款关键词。")
    matches = []
    today = date.today().isoformat()
    for item in library:
        if item["status"] != "approved" or item.get("type", "evidence") != "evidence":
            continue
        projects = set(re.split(r"[\s,，]+", item.get("projects", "").strip()))
        if project not in projects and "*" not in projects:
            continue
        if item.get("region") and item["region"].strip().casefold() != region.strip().casefold():
            continue
        if item.get("audience") and item["audience"].strip().casefold() != audience.strip().casefold():
            continue
        if item.get("approvalUntil") and item["approvalUntil"] <= today:
            continue
        if item.get("validFrom") and item["validFrom"] > target.isoformat():
            continue
        if item.get("validUntil") and item["validUntil"] <= target.isoformat():
            continue
        haystack = " ".join(item.get(key, "") for key in ("title", "keywords", "text", "chapter", "identifier")).lower()
        score = sum(word in haystack for word in tokens)
        if score and item.get("page") and item.get("chapter") and item.get("text"):
            matches.append((score, item))
    return [item for _, item in sorted(matches, key=lambda entry: -entry[0])[:8]]

def annotate_docx(raw, issues):
    with ZipFile(BytesIO(raw)) as archive:
        content = _xml(archive, "word/document.xml")
        nodes = _paragraphs(content.find(_w("body")), "正文", "document")
        notes_path = "word/footnotes.xml"
        footnotes = _xml(archive, notes_path) if notes_path in archive.namelist() else None
        if footnotes is not None:
            nodes.extend(_footnote_paragraphs(footnotes))
        comments_path = "word/comments.xml"
        rels_path = "word/_rels/document.xml.rels"
        comments = _xml(archive, comments_path) if comments_path in archive.namelist() else ET.Element(_w("comments"))
        rels = _xml(archive, rels_path) if rels_path in archive.namelist() else ET.Element(f"{{{R}}}Relationships")
        content_types = _xml(archive, "[Content_Types].xml")
        used = [int(item.get(_w("id"))) for item in comments if item.get(_w("id"), "").isdigit()]
        count = max(used, default=-1) + 1
        for item in issues:
            anchor = item.get("anchor")
            if item["state"] == "dismissed" or not isinstance(anchor, int) or not 0 <= anchor < len(nodes):
                continue
            node = nodes[anchor][0]
            ref_id = {_w("id"): str(count)}
            count += 1
            note = ET.SubElement(comments, _w("comment"), {**ref_id, _w("author"): "文衡编辑台",
                                                           _w("date"): now()})
            note_paragraph = ET.SubElement(note, _w("p"))
            ET.SubElement(ET.SubElement(note_paragraph, _w("r")), _w("t")).text = " · ".join(
                filter(None, (item["title"], item["detail"], item.get("note", ""))))
            node.insert(1 if node.find(_w("pPr")) is not None else 0, ET.Element(_w("commentRangeStart"), ref_id))
            ET.SubElement(node, _w("commentRangeEnd"), ref_id)
            ET.SubElement(ET.SubElement(node, _w("r")), _w("commentReference"), ref_id)
        if not any(item.get("Type") == REL + "comments" for item in rels):
            ET.SubElement(rels, f"{{{R}}}Relationship", {"Id": "rId" + new_id(), "Type": REL + "comments", "Target": "comments.xml"})
        if not any(item.get("PartName") == "/word/comments.xml" for item in content_types):
            ET.SubElement(content_types, f"{{{C}}}Override", {"PartName": "/word/comments.xml",
                          "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"})
        outputs = {"word/document.xml": content, comments_path: comments,
                   rels_path: rels, "[Content_Types].xml": content_types}
        if footnotes is not None:
            outputs[notes_path] = footnotes
        result = BytesIO()
        with ZipFile(result, "w", ZIP_DEFLATED) as new_archive:
            for item in archive.infolist():
                if item.filename not in outputs:
                    new_archive.writestr(item, archive.read(item))
            for path, root in outputs.items():
                original = archive.read(path) if path in archive.namelist() else b""
                new_archive.writestr(path, _serialize_xml(root, original))
        return result.getvalue()

def _serialize_xml(element, original):
    serialized = ET.tostring(element, encoding="utf-8", xml_declaration=True)
    if not original:
        return serialized
    # Word's compatibility attributes can refer to prefixes without a matching
    # element or attribute in the XML tree. Keep those original declarations.
    root_start = serialized.index(b"<", serialized.index(b"?>") + 2)
    root_end = serialized.index(b">", root_start)
    root_text = serialized[root_start:root_end]
    declarations = {}
    for event, item in ET.iterparse(BytesIO(original), events=("start-ns", "start")):
        if event == "start":
            break
        prefix, uri = item
        marker = ("xmlns:" + prefix + "=").encode()
        if prefix and marker not in root_text:
            declarations[prefix] = uri
    additions = "".join(f" xmlns:{prefix}={quoteattr(uri)}" for prefix, uri in declarations.items()).encode()
    insert_at = root_end - 1 if serialized[root_end - 1:root_end] == b"/" else root_end
    return serialized[:insert_at] + additions + serialized[insert_at:]

def seed():
    specs = [
        ("MS-2026-018", "制造现场的质量数据分析", "陈世明", "工程技术", 2, "active"),
        ("MS-2026-021", "数字时代的编辑方法", "周妍", "人文社科", 0, "active"),
        ("MS-2026-014", "城市更新中的社区营造", "罗皓", "人文社科", 1, "active"),
        ("MS-2026-012", "中国古代建筑构造图解", "何远", "建筑设计", 4, "revision"),
        ("MS-2026-019", "碳中和背景下的能源转型", "张琳", "工程技术", 3, "active"),
        ("MS-2026-008", "设计研究的理论与方法", "苏蓉", "艺术设计", 4, "archived"),
        ("MS-2026-022", "公共数据治理实务", "沈文", "人文社科", 0, "active"),
        ("MS-2026-016", "面向智能制造的设备管理", "王启", "工程技术", 2, "paused"),
    ]
    paragraphs = [
        {"text": "第三章 现场缺陷诊断", "location": "第三章", "kind": "heading"},
        {"text": "连续监测结果显示，2022 年、2023 年和 2024 年的缺陷率分别为 4.2%、3.5% 和 3.8%。因此，引入诊断方法以来，缺陷率呈逐年下降趋势。", "location": "第三章", "kind": "text"},
        {"text": "在诊断口径中，本案例将每件产品上的缺陷个数计入不合格品数，并用总检验品数作为分母。", "location": "第三章", "kind": "text"},
        {"text": "与同类实务书相比，本书提供了一套适用于现场工程师的缺陷原因诊断方法。", "location": "第三章", "kind": "text"},
        {"text": "图 3-2  某车间三年缺陷率统计", "location": "第三章", "kind": "text"},
    ]
    manuscripts = []
    for ident, title, author, category, stage, status in specs:
        version = {"id": ident + "-v1", "label": "V1", "name": "样章.txt", "created": now(),
                   "blocks": paragraphs if ident == specs[0][0] else [{"text": title + " · 样章", "location": "导言", "kind": "heading"}],
                   "changes": [], "type": "原稿", "size": 0, "file": ""}
        manuscripts.append({"id": ident, "title": title, "author": author, "editor": "林编辑",
                            "category": category, "description": "演示样稿", "stage": stage,
                            "status": status, "created": now(), "versions": [version], "events": [],
                            "comparisons": [], "assessment": {}, "bibliography": [],
                            "region": "", "audience": "", "example": True})
    log(manuscripts[0], "专业核验已开始", "已整理图表与引文疑点。")
    log(manuscripts[3], "待作者补件", "请补齐第三方插图授权。")
    log(manuscripts[5], "签核归档", "责任编辑已确认。")
    demo_issues = [
        (0, issue_data("图文核验", "「逐年下降」与原始数据不一致", "核实 2024 年较 2023 年回升 0.3 个百分点的统计条件。", 1, "high",
                       [{"label": "图 3-2 · 数值转录", "text": "2022 年 4.2%  /  2023 年 3.5%  /  2024 年 3.8%", "anchor": 4}], stage=2)),
        (1, issue_data("收稿材料", "缺少书稿中的原始配图", "作者提交了正文文件，原始配图仍待补齐。", 0, "high", blocking=True)),
        (0, issue_data("专业审校", "缺陷个数与不合格品数口径混用", "请核对两个指标的分子、分母，或补充企业自定义口径。", 2, "normal",
                       [{"label": "社内术语手册 · 2.1 · 第 7 页", "text": "一件产品可以出现多个缺陷，不合格品数按件计。"}], stage=2)),
        (2, issue_data("选题对照", "与同类实务书的差异待确认", "收集对照样章，区分作者新增案例与既有方法。", 0, "normal", stage=1)),
        (3, issue_data("图片授权", "插图的第三方授权材料待补", "请登记权利人、许可媒介、期限及署名。", 0, "high", blocking=True, stage=4)),
        (4, issue_data("学术评估", "核心结论需要补充可追溯证据", "补充关键数据来源、样本和推论边界，再决定是否送外审。", 0, "normal", stage=3)),
        (7, issue_data("引文核验", "同章引文存在集中疑点", "演示记录。需复核正式原刊和独立来源，经编辑确认后才可恢复流转。", 0, "high", blocking=True, stage=2)),
        (1, issue_data("体例用语", "统一术语「数字出版物」", "与社内术语表核对出现位置。", 0, "low", group="term:demo")),
        (1, issue_data("体例用语", "统一术语「数字出版物」", "与社内术语表核对出现位置。", 0, "low", group="term:demo")),
    ]
    issues = [make_issue(manuscripts[index], manuscripts[index]["versions"][0]["id"], payload)
              for index, payload in demo_issues]
    issues[4]["state"] = "waiting"
    library = [
        {"id": "sample-evidence", "type": "evidence", "title": "质量数据术语手册（演示）",
         "identifier": "DEMO-QA", "edition": "2025 年版", "chapter": "第 2 章 2.1", "page": "7",
         "text": "不合格品数以产品为统计单位。缺陷数可包含同件产品上的多项缺陷，二者不得混用。",
         "keywords": "缺陷 不合格品 统计口径", "source": "虚构的社内知识库样例", "status": "approved",
         "projects": "MS-2026-018", "validFrom": "2025-01-01", "validUntil": "", "approvalUntil": "",
         "region": "", "audience": "", "reviewer": "林编辑", "checked": "2026-11-12", "supersedes": ""},
        {"id": "sample-book", "type": "book", "title": "制造质量管理实务（演示）", "identifier": "DEMO-BOOK",
         "edition": "第 2 版", "text": "面向工艺工程师。覆盖缺陷分类、案例演算与质量数据诊断。",
         "keywords": "制造 质量 缺陷 数据", "source": "虚构样章", "status": "approved", "coverage": "样章",
         "projects": "*", "author": "演示作者", "checked": "2026-11-12"},
        {"id": "sample-term", "type": "term", "title": "不合格品数", "variant": "不良品数",
         "status": "approved", "source": "演示术语表", "projects": "MS-2026-018", "text": "按检验产品的件数计。"},
    ]
    return {"manuscripts": manuscripts, "issues": issues, "library": library}
