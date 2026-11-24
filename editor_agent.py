"""Evidence-driven review of one uploaded manuscript version."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from hashlib import sha256
from http.client import HTTPSConnection, HTTPException
import json
from pathlib import Path
import re
from time import sleep
from urllib.parse import quote, urlencode

from copyright_review import COPYRIGHT_CATEGORIES, copyright_status
from editorial_checks import _citation_usages, _reference_context, _reference_sections
from proofs import _image_associations, figure_contexts
from research import DOI, can_cite, compact, compare_book, crossref_metadata, redact_issue, valid_evidence
from workflow import current_review_issues, open_issue, parse_document
from workspace import expired_version


STAGES = {
    "structure": "结构解析", "editorial": "文本与体例", "citations": "引文对照与回查",
    "proofs": "图表与复算", "copyright": "版权和地图线索", "academic": "选题增量与同类书",
    "documents": "成果物整理",
}
BIBLIOGRAPHY_FINDINGS = {
    "引文身份待核实", "正文引文缺少对应条目", "未定位可对照的参考文献表",
    "文末参考文献编号重复", "文末条目未见对应正文引用",
}
PROOF_CATEGORIES = {"图文核验", "校样复核", "数据矛盾", "编号检查"}
ACADEMIC_CATEGORIES = {"学术评估", "选题对照", "内容重复"}
RETRACTIONS = {"retraction", "withdrawal", "partial-retraction", "removal"}
WARNINGS = {"expression-of-concern", "concern", "correction"}
ACADEMIC_CUES = re.compile(
    r"^(?:摘要|研究方法|研究结果|研究目的|abstract\b|method(?:s|ology)?\b|results\b)"
    r"|本(?:研究|实验)|(?:数据|调查|实验)(?:表明|显示)", re.I,
)
AUTHOR_DATE = re.compile(
    r"[（(][^（）()\n]{0,55}(?:等|et\s+al\.|[A-Z][a-z-]{2,}|[\u3400-\u9fff]{2,5})"
    r"[,，\s]+(?:19|20)\d{2}[a-z]?\s*[）);,，；]"
    r"|(?:[A-Z][a-z-]{2,}|[\u3400-\u9fff]{2,5})\s*[（(]\s*(?:19|20)\d{2}[a-z]?\s*[）)]"
)
STANDARD_ID = re.compile(r"(?<![A-Z0-9])(?P<series>GB\s*/\s*[TZ]|GBZ?|ISO\s*/\s*IEC|ISO|IEC)"
                         r"\s*(?P<number>\d{2,7}(?:\.\d{1,4})?)"
                         r"(?:\s*[-–—:：]\s*(?P<edition>(?:19|20)\d{2}))?(?![A-Z0-9])", re.I)
MAX_CORPORA = 4
MAX_MANUSCRIPT_COMPARE = 400
MAX_CORPUS_COMPARE = 180


def _updates_for_doi(doi):
    """Retrieve notices that update the cited work, including Retraction Watch."""
    query = urlencode({"filter": "updates:" + doi, "rows": 20, "select": "DOI,update-to"})
    conn = HTTPSConnection("api.crossref.org", timeout=5)
    try:
        conn.request("GET", "/works?" + query,
                     headers={"Accept": "application/json", "User-Agent": "WenhengEditorialWorkspace/1.0"})
        response = conn.getresponse()
        if response.status != 200:
            return None
        message = json.loads(response.read(1024 * 1024))["message"]
        items = message["items"]
        if message["total-results"] > len(items):
            return None
        notices = {}
        for notice in items:
            matched = False
            for update in notice.get("update-to", []):
                if update.get("DOI", "").casefold() == doi:
                    matched = True
                    record = {"doi": notice.get("DOI", ""), "type": update.get("type", ""),
                              "source": update.get("source", "")}
                    key = record["doi"], record["type"]
                    if key not in notices or record["source"] == "publisher":
                        notices[key] = record
            if not matched:
                return None
        return list(notices.values())
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return None
    finally:
        conn.close()


def lookup_doi(doi):
    """Resolve a reference's identity and its indexed post-publication notices."""
    try:
        found = crossref_metadata(doi)
        if found["status"] == "throttled":
            sleep(.4)
            found = crossref_metadata(doi)
        if found["status"] != "found":
            return found
        notices = _updates_for_doi(doi)
        if notices is None:
            return {**found, "status": "unavailable"}
        changes = {notice["type"].lower().replace("_", "-").replace(" ", "-") for notice in notices}
        status = "retracted" if changes & RETRACTIONS else "notice" if changes & WARNINGS else "found"
        # A cited retraction notice must be reviewed in context as well.
        if status == "found" and found.get("retracted"):
            status = "notice"
        return {**found, "status": status, "notices": notices}
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return {"doi": doi, "status": "unavailable"}


def _location(blocks, index):
    return blocks[index] if isinstance(index, int) and 0 <= index < len(blocks) else {}


def _evidence(blocks, index, label="原文", text=None):
    block = _location(blocks, index)
    return {"label": label, "anchor": index, "text": (block.get("text", "") if text is None else text)[:950]}


def case_for_finding(stage, finding, blocks, *, allow_exception=False):
    index = finding.get("anchor")
    block = _location(blocks, index)
    title, detail = finding["title"], finding["detail"]
    identity = json.dumps([stage, title, detail, index, finding.get("id")], ensure_ascii=False)
    options = [{"id": "A", "label": "退回作者补正"}]
    if allow_exception:
        options.append({"id": "B", "label": "核实后保留，登记依据"})
    options.append({"id": "C", "label": "登记终止立项"})
    return {
        "id": sha256(identity.encode()).hexdigest()[:20],
        "stage": stage, "severity": finding.get("severity", "normal"), "title": title,
        "detail": detail, "anchor": index, "location": block.get("location", ""),
        "page": block.get("page"), "excerpt": block.get("text", "")[:950],
        "issueId": finding.get("id"),
        "evidence": finding.get("evidence", [])[:24], "options": options,
        "authorRequest": f"请补充说明或修正：{title}。{detail}",
    }


def _case(stage, blocks, title, detail, anchor=None, *, evidence=(), severity="normal", exception=False):
    return case_for_finding(stage, {"title": title, "detail": detail, "anchor": anchor,
                                   "severity": severity, "evidence": list(evidence)},
                            blocks, allow_exception=exception)


def _dois(text):
    values = []
    for match in DOI.finditer(text):
        doi = match[0].casefold().rstrip(".,;:")
        while doi.endswith(")") and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        if len(doi) <= 240:
            values.append(doi)
    return list(dict.fromkeys(values))


def _doi_link(doi):
    return "https://doi.org/" + quote(doi, safe="/") if DOI.fullmatch(doi) else ""


def _matches_bibliography(entry, metadata):
    title = compact(metadata.get("title") or "")
    text = " ".join(DOI.sub("", entry).split())
    if not title or title not in compact(text):
        return False
    years = set(re.findall(r"\b(?:18|19|20)\d{2}\b", text))
    if metadata.get("year") and str(metadata["year"]) not in years:
        return False
    names = metadata.get("authors") or []
    if names:
        full = compact(names[0])
        written = set(re.findall(r"[^\W\d_]+", text.casefold()))
        components = [word.casefold() for word in re.findall(r"[^\W\d_]+", names[0]) if len(word) > 1]
        if full not in compact(text) and (not components or components[-1] not in written):
            return False
    return True


def _bibliography_evidence(item, blocks, scope, number, entry):
    labels = ({"全文", "文末", "参考文献", "正文"} if scope < 0 else
              {compact(blocks[scope]["text"]), compact(blocks[scope].get("location", ""))})
    for record in item.get("bibliography", []):
        source_a, source_b = record.get("sourceA", "").strip(), record.get("sourceB", "").strip()
        key = compact(record.get("key", ""))
        if (record.get("status") == "verified" and record.get("number") == number
                and compact(record.get("chapter", "")) in labels - {""}
                and source_a and source_b and source_a.casefold() != source_b.casefold()
                and len(key) >= 6 and key in compact(entry)):
            return {"label": "编辑登记的两条回查来源", "text": f"{source_a} · {source_b}"}
    return None


def _citation_review(item, blocks, structure, doi_lookup, progress):
    sections, _, references, body, _ = structure
    cases = []
    rows = []
    used = defaultdict(set)
    scoped_uses = [(index, scope, numbers) for index, scope, numbers, text, _ in _citation_usages(blocks, structure)
                   if sections or _reference_context(text)]
    for index, scope, numbers in scoped_uses:
        used[scope].update(numbers)
        missing = sorted(number for number in numbers if number not in references.get(scope, {}))
        if missing:
            labels = "、".join(f"[{value}]" for value in missing[:20])
            cases.append(_case("citations", blocks, "正文引用缺少对应条目",
                               f"正文中的 {labels} 共 {len(missing)} 个编号缺少可对照条目。", index))
    if sections and any(not references.get(scope) for scope in sections):
        cases.append(_case("citations", blocks, "参考文献表编号待核",
                           "找到参考文献表标题，但至少一处没有可对照的编号条目。请确认编号和原稿结构。"))
    first = next((index for index in body if AUTHOR_DATE.search(blocks[index]["text"])), None)
    if first is not None:
        cases.append(_case("citations", blocks, "作者—年份引文待回查",
                           "识别到作者—年份引文，须结合文末列表或脚注人工核对。", first))
    listed_dois = {doi for numbered in references.values() for entries in numbered.values()
                   for _,entry in entries for doi in _dois(entry)}
    for index in body:
        unlinked = [doi for doi in _dois(blocks[index]["text"]) if doi not in listed_dois]
        if unlinked:
            cases.append(_case("citations",blocks,"正文 DOI 待辨识",
                               "这些 DOI 未与可解析的文末条目对应。请确认用途并补充书目信息，或登记保留依据。",
                               index,evidence=[{"label":"正文 DOI","text":doi,"url":_doi_link(doi)}
                                               for doi in unlinked[:12]],exception=True))
    results = {}
    outage = 0
    for scope, numbered in references.items():
        for number, entries in numbered.items():
            if len(entries) > 1:
                evidence = [_evidence(blocks, index, f"文末 [{number}]", entry) for index, entry in entries]
                cases.append(_case("citations", blocks, "文末参考文献编号重复",
                                   f"[{number}] 对应 {len(entries)} 条文末记录。", entries[0][0], evidence=evidence))
            if number not in used[scope]:
                cases.append(_case("citations", blocks, "文末条目未见正文引用",
                                   f"未在所属章节或适用正文中查到 [{number}]。", entries[0][0],
                                   evidence=[_evidence(blocks, entries[0][0], f"文末 [{number}]", entries[0][1])],
                                   exception=True))
            for index, entry in entries:
                entry_evidence = _evidence(blocks, index, f"文末 [{number}]", entry)
                dois = _dois(entry)
                if len(dois) != 1:
                    detail = "该条目尚无可解析 DOI，须补证或查阅其原始书目来源。" if not dois else "同一条目包含多个 DOI，须确认引用对象。"
                    manual = _bibliography_evidence(item, blocks, scope, number, entry) if not dois else None
                    cases.append(_case("citations", blocks, "文末条目 DOI 待核",
                                       f"[{number}] {detail}", index,
                                       evidence=[entry_evidence, manual] if manual else [entry_evidence],
                                       exception=bool(manual)))
                    rows.append({"number": number, "anchor": index, "doi": "", "status": "unverified"})
                    continue
                doi = dois[0]
                if doi not in results and outage < 2:
                    try:
                        response = doi_lookup(doi)
                    except (OSError, HTTPException, ValueError):
                        response = {"status": "unavailable"}
                    results[doi] = response
                    outage = outage + 1 if response["status"] in {"unavailable", "throttled"} else 0
                    if len(results) % 10 == 0:
                        progress("citations", f"已请求 {len(results)} 个不同 DOI 的身份及更新索引。")
                response = results.get(doi, {"status": "deferred"})
                status = response["status"]
                if status == "found":
                    status = "verified" if _matches_bibliography(entry, response) else "mismatch"
                rows.append({"number": number, "anchor": index, "doi": doi, "status": status,
                             "title": response.get("title", ""), "url": _doi_link(doi)})
                if status == "verified":
                    continue
                title, detail = {
                    "retracted": ("Crossref 收录撤稿通知", "公开索引中有指向该文献的撤稿或撤回通知，请人工核对原论据。"),
                    "notice": ("Crossref 收录更新通知", "该 DOI 存在更正、质疑或撤稿相关通知，请核对引用语境。"),
                    "mismatch": ("引文与 Crossref 元数据待比对", "DOI 可解析，题名、作者或年份未能与文末条目对齐。"),
                    "noCoverage": ("Crossref 未覆盖此 DOI", "请使用出版方或其他书目来源回查。"),
                    "deferred": ("暂未完成 DOI 查询", "Crossref 连续不可用，余下引文待重新核查。"),
                }.get(status, ("Crossref 核验不可用", "DOI 身份或更新通知查询失败。请重试或补独立来源。"))
                evidence = [entry_evidence, {"label": "Crossref 书目信息",
                            "text": response.get("title") or doi, "url": _doi_link(doi)}]
                manual = _bibliography_evidence(item, blocks, scope, number, entry) if status == "noCoverage" else None
                if manual:
                    evidence.append(manual)
                evidence.extend({"label": "Crossref 更新通知",
                                 "text": " · ".join(filter(None, [notice.get("type"), notice.get("source"), notice.get("doi")])),
                                 "url": _doi_link(notice.get("doi", ""))}
                                for notice in response.get("notices", []) if isinstance(notice, dict))
                cases.append(_case("citations", blocks, title, f"[{number}] {doi}。{detail}", index,
                                   evidence=evidence, exception=bool(manual),
                                   severity="high" if status == "retracted" else "normal"))
    applicable = bool(sections or scoped_uses or cases)
    detail = (f"对照 {len(rows)} 条已解析文末记录，Crossref 核对匹配 {sum(row['status'] == 'verified' for row in rows)} 条。"
              "撤稿核查范围为 Crossref 当时提供的更新索引。"
              if applicable else "未识别到适用的方括号编号引文或参考文献表。")
    return cases, rows, applicable, detail


def _stage(finding):
    category = finding["category"]
    if category == "引文核验":
        return "citations"
    if category in COPYRIGHT_CATEGORIES:
        return "copyright"
    if category in PROOF_CATEGORIES:
        return "proofs"
    if category in ACADEMIC_CATEGORIES:
        return "academic"
    return "editorial"


def _signature(finding):
    return finding["category"], finding.get("anchor"), finding["title"]


def _may_except(stage, finding):
    return not finding.get("blocking") and (stage in {"editorial", "academic"}
                                            or finding["category"] == "数据矛盾")


def _fixed_issue_ids(data, item, version, findings, fixes):
    anchors = {fix["anchor"] for fix in fixes}
    remaining = {_signature(finding) for finding in findings}
    return [issue["id"] for issue in data.get("issues", [])
            if issue["manuscript"] == item["id"] and issue.get("version") == version["id"]
            and issue.get("origin") != "manual" and issue["state"] == "pending"
            and not issue.get("decidedBy") and not issue.get("blocking")
            and issue.get("anchor") in anchors and issue.get("group", "").startswith("unit-spacing:")
            and _signature(issue) not in remaining]


def _finding_cases(data, item, version, blocks, findings, fixes):
    versions = {row["id"]: row for row in item.get("versions", [])}
    current = [issue for issue in data.get("issues", []) if issue["manuscript"] == item["id"]]
    current = list(current_review_issues(current, versions))
    matching = {}
    for issue in current:
        if issue.get("version") == version["id"] and issue.get("origin") != "manual":
            matching.setdefault(_signature(issue), issue)
    sources = {entry["id"]: entry for entry in data.get("library", [])}
    fixed = {fix["anchor"] for fix in fixes}
    cases = []
    seen = set()
    covered = set()
    for finding in findings:
        key = _signature(finding)
        if key in seen or finding["title"] in BIBLIOGRAPHY_FINDINGS:
            continue
        seen.add(key)
        issue = matching.get(key)
        stage = _stage(finding)
        if stage == "copyright" and issue and copyright_status(issue, item)["status"] == "ready":
            continue
        exception = _may_except(stage, finding) and (not issue or _may_except(stage,issue))
        if issue:
            covered.add(issue["id"])
        if issue and exception and not open_issue(issue):
            continue
        if issue:
            finding = {**finding, "id": issue["id"]}
        cases.append(case_for_finding(stage, finding, blocks, allow_exception=exception))
    for issue in current:
        rights = issue["category"] in COPYRIGHT_CATEGORIES
        if issue["id"] in covered or (issue.get("version") == version["id"] and _signature(issue) in seen
                                     and issue.get("origin") != "manual" and not issue.get("blocking") and not rights):
            continue
        active = issue.get("version") in {version["id"], "", None}
        unresolved = open_issue(issue) or rights and copyright_status(issue, item)["status"] != "ready"
        legacy = not issue.get("origin") and not (issue["title"] in BIBLIOGRAPHY_FINDINGS or (
            issue.get("anchor") in fixed and issue.get("group", "").startswith(("typo:", "unit-spacing:"))))
        # Keep unclassified legacy records and current manual decisions visible when they are still open.
        if not unresolved or not (active and (issue.get("origin") == "manual" or rights or legacy)
                                  or issue.get("blocking") or (rights and issue.get("rights"))):
            continue
        visible = redact_issue(issue, sources)
        if not active:
            old = versions.get(issue.get("version"))
            if old and expired_version(old):
                visible = {**visible, "title": "过期版本有未结审校事项",
                           "detail": "请由责任编辑核对归档的处置状态。", "evidence": []}
            visible = {**visible, "anchor": None, "evidence": [{key: value for key, value in row.items()
                                                               if key != "anchor"} for row in visible["evidence"]]}
        stage = _stage(issue)
        cases.append(case_for_finding(stage, visible, blocks, allow_exception=_may_except(stage, issue)))
    return cases


def _proof_review(item, blocks):
    cases = []
    captions, mentions = figure_contexts(blocks)
    associated = _image_associations(blocks, captions)
    associated.update({number: index for number, index in captions.items() if blocks[index].get("images")})
    for number in sorted(set(captions) | set(mentions)):
        index = captions.get(number)
        problem = ("正文指代无对应图题" if index is None else "图题尚无可测量的对应图件"
                   if number not in associated else "找到图件，尚无正文指代" if number not in mentions else "")
        if problem:
            title = f"缺少图 {number} 对应图题" if index is None else f"图 {number} {problem}"
            cases.append(_case("proofs", blocks, title,
                               "请对照原件、正文图号和图题；图件需提供可核验的置入分辨率。",
                               index if index is not None else mentions[number][0]))
    settings = item.get("proofSettings", {})
    images = [(index, image) for index, block in enumerate(blocks) for image in block.get("images", [])]
    known_images = set(associated.values())
    for index, image in images:
        line = image.get("lineArt")
        minimum = max(600 if line else 300, settings.get("linePpi" if line else "photoPpi", 0))
        ppi = min(image.get("xPpi", 0), image.get("yPpi", 0))
        if index not in known_images:
            cases.append(_case("proofs", blocks, "图件与图题对应待确认",
                               "此处有可测量图件，尚未定位对应图题，请核对图件用途和正文指代。",
                               index, exception=True))
        if ppi < minimum:
            cases.append(_case("proofs", blocks, "图件印制分辨率需处理",
                               f"按{('线稿' if line else '照片')}核验最低 {minimum:g} PPI，实测较低轴为 {ppi:g} PPI。",
                               index, evidence=[_evidence(blocks, index, "图件", image.get("source", ""))]))
    numeric_tables = defaultdict(list)
    for index, block in enumerate(blocks):
        kind = block["kind"]
        if kind == "image" and not block.get("images") and not block.get("scanNeeded"):
            cases.append(_case("proofs", blocks, "图件未获得可测量的置入数据",
                               "请补图件原件和版面置入尺寸，或核对矢量图的类型及转换输出。", index))
        if block.get("formulaCheck") or kind == "formula" and not block.get("latex"):
            cases.append(_case("proofs", blocks, "公式结构需人工复核",
                               "当前提取器没有完整识别该公式结构，请查看原件核对。", index))
        if kind == "table" and re.search(r"\d", block["text"]):
            numeric_tables[block.get("tableNumber", index)].append(index)
    if not numeric_tables:
        first_table = next((index for index, block in enumerate(blocks)
                            if block.get("part") == "pdf" and re.match(
                                r"^(?:附?表\s*\d|table\s+\d)", block["text"], re.I)), None)
        if first_table is not None:
            cases.append(_case("proofs", blocks, "PDF 表格缺少结构化计算依据",
                               "请补可解析数据表及与本校样的对应位置，核对表项和计算结果。", first_table))
    calculations = 0
    for anchors in numeric_tables.values():
        results = [result for index in anchors for result in blocks[index].get("calculations", [])]
        calculations += sum(result.get("status") == "checked" for result in results)
        if not results or any(result.get("status") != "checked" for result in results):
            cases.append(_case("proofs", blocks, "数值表待核原数",
                               "该表的计算结果未全部获得可复算依据，请补原始数据、公式或说明。", anchors[0]))
    for version in item.get("versions", []):
        if version["type"] != "补件" or version.get("supersededBy") or expired_version(version):
            continue
        rows = [(index,result) for index,block in enumerate(version["blocks"]) for result in block.get("calculations",[])]
        calculations += sum(row.get("status") == "checked" for _,row in rows)
        discrepancy = [(index,row) for index,row in rows if row.get("status") != "checked"]
        if discrepancy:
            evidence = [{"label": f"{version['name']} · {row['cell']}",
                         "version":version["id"], "anchor":index,
                         "text": (f"{row['formula']} 目前无法复算，请核对原始数据。"
                                  if row["status"] == "unsupported" else
                                  f"{row['formula']} 复算为 {row['expected']}，记录为 {row['observed'] or '无缓存值'}。")}
                        for index,row in discrepancy[:16]]
            cases.append(_case("proofs", blocks, "补件计算待核", f"该补件有 {len(discrepancy)} 处结果不一致或未获复算。",
                               evidence=evidence, severity="high"))
        unknown = [block for block in version["blocks"] if not block.get("calculations")
                   and any(cell.startswith("=") for cell in block.get("cells", []))]
        if unknown:
            cases.append(_case("proofs", blocks, "补件包含未解析的公式",
                               f"{version['name']} 有 {len(unknown)} 行包含当前计算器未解析的公式。",
                               evidence=[{"label": block["location"], "text": block["text"][:250]}
                                         for block in unknown[:16]]))
    return cases, bool(captions or mentions or images or numeric_tables or calculations), (
        f"核查 {len(captions)} 个图题及 {len(images)} 个可测量图件，"
        f"复算吻合 {calculations} 个已支持的合计或公式。")


def _standard_id(match):
    return compact(match["series"]), match["number"], match["edition"]


def _standards_review(data, item, blocks, body):
    referenced = {}
    for index in body:
        for match in STANDARD_ID.finditer(blocks[index]["text"]):
            referenced.setdefault(_standard_id(match), (index, match[0]))
    candidates = defaultdict(set)
    today = date.today().isoformat()
    for entry in data.get("library", []):
        if not valid_evidence(entry, item, today) or not (match := STANDARD_ID.fullmatch(entry.get("identifier", ""))):
            continue
        series, number, edition = _standard_id(match)
        recorded = re.search(r"(?:18|19|20)\d{2}", entry.get("edition", ""))
        candidates[series, number].add(edition or (recorded[0] if recorded else None))
    cases = []
    for (series, number, edition), (index, label) in referenced.items():
        scoped_editions = candidates.get((series, number), set())
        if not scoped_editions or (edition and edition not in scoped_editions):
            cases.append(_case("academic", blocks, f"标准条文依据待核 · {label}",
                               "本项目资料库缺少已审定、尚在适用时段且对应版次的授权条文。请核对适用地区、对象和版次。",
                               index))
    return cases, len(referenced)


def _academic_review(data, item, blocks, body):
    cases = []
    evidence_anchor = next((index for index in body if ACADEMIC_CUES.search(blocks[index]["text"])), None)
    source_blocks = [blocks[index] for index in body]
    entries = [entry for entry in data.get("library", []) if entry.get("type") == "book"
               and entry.get("coverage") == "授权全文" and entry.get("corpus") and can_cite(entry, item["id"])]
    limited = len(entries) > MAX_CORPORA or len(source_blocks) > MAX_MANUSCRIPT_COMPARE
    for entry in entries[:MAX_CORPORA]:
        corpus = [row for row in entry["corpus"] if len(compact(row["text"])) >= 22]
        limited = limited or len(corpus) > MAX_CORPUS_COMPARE
        match = next((row for row in compare_book(source_blocks[:MAX_MANUSCRIPT_COMPARE], corpus[:MAX_CORPUS_COMPARE])
                      if len(row.get("overlap", "")) >= 65 or row["score"] >= .87), None)
        if match:
            cases.append(_case("academic", blocks, "与已授权同类书存在较高重合线索",
                               "请核对重合内容是否为引文、共同定义或实质拼接。",
                               body[match["anchor"]], exception=True,
                               evidence=[{"label": f"{entry['title']} · {match['sourceLocation']}",
                                          "text": match["matched"][:950], "evidenceId": entry["id"]}]))
    assessment = item.get("assessment", {})
    documented = bool(assessment.get("increment") and assessment.get("evidence"))
    if evidence_anchor is not None and not documented:
        cases.append(_case("academic", blocks, "研究增量与依据待复核",
                           "请在学术评估登记相较既有工作的具体增量和依据。相似度线索可辅助核对。",
                           evidence_anchor))
    if evidence_anchor is not None and not entries:
        cases.append(_case("academic", blocks, "同类书比对覆盖不足",
                           "没有可用于本项目全文比对的已审定授权书目。请添加授权资料或登记替代比对依据。",
                           evidence_anchor, exception=True))
    if entries and limited:
        cases.append(_case("academic", blocks, "同类书全文自动比对达到覆盖上限",
                           f"单次覆盖前 {MAX_CORPORA} 种授权书目、书稿前 {MAX_MANUSCRIPT_COMPARE} 段"
                           f"及各授权书目前 {MAX_CORPUS_COMPARE} 个有效段落。",
                           exception=True))
    standards, standard_count = _standards_review(data, item, blocks, body)
    cases.extend(standards)
    return cases, bool(entries or evidence_anchor is not None or standard_count), (
        f"比对 {min(len(entries), MAX_CORPORA)} 种项目授权书目。"
        f"条文资料覆盖 {standard_count - len(standards)}/{standard_count} 个明确提及的标准标识。"
        f"{'已有编辑登记的增量与依据。' if documented else '学术增量按正文研究线索和现有证据判断覆盖情况。'}")


def _checked_word(raw, version):
    from agent_documents import revise_docx, suggest_fixes

    fixes = suggest_fixes(raw, version["blocks"])
    if not fixes:
        return raw, fixes
    return revise_docx(raw, fixes, tracked=False), fixes


def run_review(data, item, version, raw, check, progress, doi_lookup=lookup_doi):
    """Plan and execute review, reporting only tool progress and observed evidence."""
    blocks = deepcopy(version["blocks"])
    version = {**version, "blocks": blocks}
    progress("structure", "读取已解析的稿件结构与证据定位。")
    readable = [block for block in blocks if block["kind"] in {"text", "heading", "table", "footnote"}
                and block["text"].strip()]
    by_stage = defaultdict(list)
    if not readable:
        by_stage["structure"].append(_case("structure", blocks, "没有可审读的稿件正文",
                                          "请上传可提取文字的稿件，或者为扫描件提供经校对的文字版。"))

    progress("editorial", "执行文本和体例检查，验证可确定的 Word 修订。")
    fixes = []
    word = raw is not None and Path(version["name"]).suffix.casefold() == ".docx"
    if word:
        revised, fixes = _checked_word(raw, version)
        if fixes:
            updated = parse_document(version["name"], revised)
            if len(updated) != len(blocks) or any(old["kind"] != new["kind"] for old, new in zip(blocks, updated)):
                fixes = []
                by_stage["documents"].append(_case("documents", blocks, "自动修订须人工核对",
                                                   "修订后文档结构发生变化，建议保留原稿并人工处理。"))
            else:
                blocks = updated
                version["blocks"] = blocks
                raw = revised
    findings = check(data, item, version, raw)
    for case in _finding_cases(data, item, version, blocks, findings, fixes):
        by_stage[case["stage"]].append(case)
    if len(findings) >= 250:
        by_stage["editorial"].append(_case("editorial", blocks, "高密度审校结果待复核",
                                          "触及部分规则的单轮输出上限。请分批核对完整结果并复跑。"))

    progress("citations", "同时执行引文、图表与授权资料核验，按 DOI 回查公开索引。")
    structure = _reference_sections(blocks)
    with ThreadPoolExecutor(max_workers=3,thread_name_prefix="editorial-review") as pool:
        cite_task = pool.submit(_citation_review,item,blocks,structure,doi_lookup,progress)
        proof_task = pool.submit(_proof_review,item,blocks)
        academic_task = pool.submit(_academic_review,data,item,blocks,structure[3])
        citation_cases, citations, citations_applicable, citation_detail = cite_task.result()
        by_stage["citations"].extend(citation_cases)

        progress("proofs", "汇总图题、图件分辨率和可计算表项。")
        proof_cases, proofs_applicable, proof_detail = proof_task.result()
        existing_proofs = {(case["title"],case["anchor"]) for case in by_stage["proofs"]}
        by_stage["proofs"].extend(case for case in proof_cases if (case["title"],case["anchor"]) not in existing_proofs)

        progress("copyright", "汇总第三方材料、地图线索及现有授权记录。")
        rights_applicable = bool(by_stage["copyright"] or any(
            issue["manuscript"] == item["id"] and issue["category"] in COPYRIGHT_CATEGORIES
            for issue in data.get("issues", [])))

        progress("academic", "汇总项目授权资料、同类书与研究增量核验结果。")
        academic_cases, academic_applicable, academic_detail = academic_task.result()
        by_stage["academic"].extend(academic_cases)

    progress("documents", "生成签发白皮书、证据案卷和可用的修订成果。")
    descriptions = {
        "structure": (True, f"{len(readable)} 个可读正文、标题、表格或脚注块。"),
        "editorial": (bool(readable), f"执行内置体例规则，可确定的 Word 修订涉及 {len(fixes)} 个段落。"),
        "citations": (citations_applicable, citation_detail),
        "proofs": (proofs_applicable, proof_detail),
        "copyright": (rights_applicable, "核对可识别的第三方材料与地图线索，以及本版本登记的权利依据。"),
        "academic": (academic_applicable, academic_detail),
        "documents": (word, "Word 原稿保留，可按版本生成清稿与修订标记稿。"
                      if word else "此格式可查阅白皮书与案卷，Word 修订需要 DOCX 原稿。"),
    }
    checks, cases = [], []
    for stage, label in STAGES.items():
        applicable, detail = descriptions[stage]
        unresolved = list({case["id"]: case for case in by_stage[stage]}.values())
        cases.extend(unresolved)
        checks.append({"id": stage, "label": label, "detail": detail,
                       "status": "review" if unresolved else "pass" if applicable else "not_applicable"})
    applicable = sum(entry["status"] != "not_applicable" for entry in checks)
    return {"status": "escalation_required" if cases else "ready_for_signoff",
            "paragraphs": len(readable),
            "characters": sum(len(re.sub(r"\s+", "", block["text"])) for block in readable),
            "fixes": fixes, "autoFixCount": sum(fix.get("count", 1) for fix in fixes),
            "fixedIssueIds": _fixed_issue_ids(data, item, version, findings, fixes),
            "checks": checks, "cases": cases,
            "citations": citations, "coverage": sum(entry["status"] == "pass" for entry in checks) / applicable,
            "version": version["id"]}
