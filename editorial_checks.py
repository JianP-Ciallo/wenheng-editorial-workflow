"""Conservative local checks with evidence linked to manuscript paragraphs."""
from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal
from itertools import groupby
import re
import unicodedata


TYPOS = {
    "既使": "即使", "按步就班": "按部就班", "迫不急待": "迫不及待",
    "再接再励": "再接再厉", "一愁莫展": "一筹莫展", "甘败下风": "甘拜下风",
    "变本加利": "变本加厉", "不径而走": "不胫而走",
    "墨守陈规": "墨守成规", "滥芋充数": "滥竽充数", "换然一新": "焕然一新",
}
TYPO_PATTERN = re.compile("|".join(
    r"既使(?!用|得|唤|劲)" if word == "既使" else re.escape(word) for word in TYPOS
))
LANGUAGE_RULES = (
    (r"(?:^|[。！？；])\s*(?:通过|经过)[^，。；！？]{2,35}，\s*使(?:得)?[^，。；！？]{2,}",
     "句子主语待核对", "句首介词结构与“使”连用。请核对整个句子是否缺少明确主语。"),
    (r"避免(?:再)?不(?:再)?(?:发生|出现|重演|产生)[^。；！？]{0,25}",
     "否定关系待核对", "核对是否为“避免发生”或“确保不再发生”，以免叙述方向相反。"),
    (r"改善(?:了|其)?(?:工作效率|生产效率|运行效率|传输速度|响应速度)",
     "动宾搭配待核对", "请核对是改善工作条件，还是提高效率或速度。"),
    (r"降低(?:了)?[^，。；！？]{0,14}(?:不良率|缺陷率|故障率)的改善程度",
     "指标搭配待核对", "请明确是降低指标本身，还是改变指标的改善幅度。"),
    (r"(?:方案|方法|模型|算法)[一二三甲乙丙丁A-Z0-9]+[和与及](?:方案|方法|模型|算法)"
     r"[一二三甲乙丙丁A-Z0-9]+[^。；！？]{0,18}[。；]\s*(?:它|该方法|该方案|该模型|该算法|这一方法)",
     "代词所指待核对", "前文有多个候选对象，请写明该句所指以保留原意。"),
)
LANGUAGE_RULES = tuple((re.compile(pattern), title, detail) for pattern, title, detail in LANGUAGE_RULES)
QUOTED = re.compile(r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》|https?://[^\s，。；！？]+")
CODE = re.compile(r"^\s*(?:示例代码|代码清单|#include|def\s|function\s|import\s|class\s|//|\x60{3})")

UNITS = {
    "kg": ("kg", "千克", "公斤"), "g": ("g", "克", "公克"), "mg": ("mg", "毫克"),
    "km": ("km", "千米", "公里"), "m": ("m", "米", "公尺"), "cm": ("cm", "厘米", "公分"),
    "mm": ("mm", "毫米", "公厘"), "kW": ("kW", "千瓦"), "kWh": ("kWh", "千瓦时", "度电"),
    "Pa": ("Pa", "帕", "帕斯卡"), "kPa": ("kPa", "千帕"), "MPa": ("MPa", "兆帕"),
    "Hz": ("Hz", "赫兹"), "℃": ("℃", "°C", "摄氏度"),
}
CANONICAL_UNITS = {form: key for key, forms in UNITS.items() for form in forms}
UNIT_PATTERN = "|".join(map(re.escape, sorted(CANONICAL_UNITS, key=len, reverse=True)))
NUMBER = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
QUANTITY = re.compile(
    rf"(?<![A-Za-z0-9_./])(?P<number>{NUMBER})(?P<space>[ \t\u00a0]*)(?P<unit>{UNIT_PATTERN})(?![A-Za-z0-9/²³])"
)
PERCENT_OR_UNIT = rf"(?:个百分点|万元|亿元|万人|万件|{UNIT_PATTERN}|%|人|件|例|台|元|吨|°)"
VALUE = rf"{NUMBER}\s*(?:{PERCENT_OR_UNIT})?"
VALUE_PARTS = re.compile(rf"(?P<number>{NUMBER})\s*(?P<unit>{PERCENT_OR_UNIT})?")
SERIES_SEPARATOR = r"(?:\s*[、,，]\s*(?:以及|和|及|与)?\s*|\s*(?:以及|和|及|与)\s*)"
YEAR = r"(?:19|20)\d{2}"
YEARS = (
    rf"{YEAR}\s*年?(?:{SERIES_SEPARATOR}{YEAR}\s*年?){{1,11}}"
    rf"|{YEAR}\s*年?\s*(?:至|到|—|-|~)\s*{YEAR}\s*年?"
)
YEAR_SERIES = re.compile(
    rf"(?<!\d)(?P<years>{YEARS})\s*(?:的|[,，])?(?P<metric>[^。；;！？!?，,、0-9]{{1,24}}?)"
    rf"(?:分别|依次)(?:为|是|达到)\s*(?P<values>{VALUE}(?:{SERIES_SEPARATOR}{VALUE}){{1,11}})"
    rf"(?=\s*(?:[。；;！？!?，,]|$))"
)
TREND = re.compile(r"(?:逐年|连年|每年)(?:持续)?\s*(上升|增加|提高|增长|递增|下降|减少|降低|下滑|递减)")
INCREASING = {"上升", "增加", "提高", "增长", "递增"}
QUALIFIER = re.compile(r"不能|并非|没有|并未|并不|未|不[一定适]|预[计期测]|若|假设|目标|应当|有望|无需|难以")
METRIC = r"(?:不合格率|不良率|缺陷率|合格率|成品率|故障率|返修率|死亡率|出生率|增长率|通过率|产量|销售额|出货量|人口|浓度|温度|质量|收入)"
YEAR_VALUE_LINK = r"\s*年\s*(?:为|是|达到)\s*"
YEAR_VALUE = rf"{YEAR}{YEAR_VALUE_LINK}{VALUE}"
YEAR_PAIR = re.compile(rf"(?P<year>{YEAR}){YEAR_VALUE_LINK}(?P<value>{VALUE})")
YEAR_PAIRS = re.compile(
    rf"(?P<metric>{METRIC})(?:分别)?(?:在|于)?\s*(?P<pairs>{YEAR_VALUE}(?:{SERIES_SEPARATOR}{YEAR_VALUE}){{1,11}})"
    rf"(?=\s*(?:[。；;！？!?，,]|$))"
)
IMPLICIT_METRIC = re.compile(r"^\s*(?:因此|因此可见|可见|故|仍|整体|总体)?\s*(?:呈现|呈|表现为)?\s*$")
TREND_DENIAL = re.compile(r"不成立|不正确|不准确|存疑|有待|尚待|待核|并不|与.{1,12}不符")
SUBJECT = r"[\u4e00-\u9fffA-Za-z0-9]{1,16}?(?:车间|工厂|工段|生产线|产线|地区|团队|批次|公司|项目|样本|省|市|区|组|装置|产品|园区)"
SCALAR_PATTERNS = tuple(re.compile(
    rf"(?:^|[，,。；;])\s*{prefix}\s*的?\s*(?P<metric>{METRIC})\s*"
    rf"(?:为|是|达到)\s*(?P<number>{NUMBER})\s*(?P<unit>{PERCENT_OR_UNIT})\s*(?=$|[，,。；;！？!?])"
) for prefix in (
    rf"(?P<subject>{SUBJECT})\s*(?P<year>{YEAR})\s*年",
    rf"(?P<year>{YEAR})\s*年\s*(?P<subject>{SUBJECT})",
))

CHINESE_NUM = "零〇一二三四五六七八九十百两"
CHAPTER = re.compile(rf"^\s*(?:第\s*([0-9{CHINESE_NUM}]{{1,6}})\s*章|chapter\s+([0-9]{{1,6}}))(?=$|\D)", re.I)
SECTION = re.compile(r"^\s*(\d+(?:\.\d+)+)(?:[ .、：:])(?=\s*\D)")
CAPTION = re.compile(
    r"^\s*(?P<name>图|表)\s*(?P<number>\d+(?:\s*[.－—–-]\s*\d+)*)"
    r"(?:[：:、]|\s+)(?P<title>[^\n。！？；]{1,140})$"
)
FORMULA = re.compile(r"^\s*(?:公式|式)\s*[（(]?\s*(\d+(?:[.－—–-]\d+)*)\s*[）)]?\s*$")
FORMULA_TAIL = re.compile(r"[（(](\d+(?:[.－—–-]\d+)*)[）)]\s*$")
BIB_TITLE = re.compile(
    r"^\s*(?:\d+[.、]\s*)?(参考文献|引文目录|文献目录|Bibliography|References)\s*[:：]?\s*$", re.I
)
END_BIBLIOGRAPHY = re.compile(r"^(?:附录|后记|致谢|索引|作者简介|Appendix\b|Acknowledg(?:e)?ments\b|Author biograph(?:y|ies)\b)", re.I)
REF_NUMBER = re.compile(r"^\s*(?:\[(\d{1,4})\]|【(\d{1,4})】|(\d{1,4})[.、](?=\s|\D))\s*(?=\S)")
PACKED_REF_NUMBER = re.compile(r"\s+\[(\d{1,4})\]\s+(?=\S)")
REFERENCE_PRINT_DATA = re.compile(r"https?\s*:\s*//|\b(?:doi\s*[:.]|arxiv\s*[:.])|(?<!\d)(?:18|19|20)\d{2}(?!\d)", re.I)
REF_RUN = re.compile(r"(?<![A-Za-z0-9_])[\[【](\s*\d{1,4}(?:\s*[,、;~—–-]\s*\d{1,4})*\s*)[\]】]")
JOURNAL = re.compile(r"\[J(?:/OL)?\][.。]?\s*([^,;；。]{2,100}?)(?=\s*[,;；。]|$)", re.I)
DEFINITION = re.compile(r"^\s*[“「]?([\u4e00-\u9fffA-Za-z]{2,16})[”」]?\s*(?:是指|定义为)\s*([^。；]+)")
TEMPLATES = (
    re.compile(r"^随着.{3,35}的发展[，,].{4,45}(?:越来越重要|日益重要|具有重要意义)"),
    re.compile(r"^在.{3,35}的背景下[，,].{4,45}(?:的重要组成部分|发挥着重要作用)"),
)


def _normal(text):
    return unicodedata.normalize("NFKC", text)


def _prose(text):
    return QUOTED.sub(lambda match: " " * len(match[0]), text)


def _active(affected, *indexes):
    return affected is None or not affected.isdisjoint(indexes)


def _snippet(text, start=0, end=None):
    end = min(len(text), start + 90) if end is None else end
    left, right = max(0, start - 32), min(len(text), end + 32)
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


def _evidence(blocks, index, label="原文", text=None):
    block = blocks[index]
    return {
        "anchor": index,
        "label": f"{label} · {block.get('location', '正文')} · 第 {index + 1} 段",
        "text": _snippet(block["text"]) if text is None else text,
    }


def _finding(category, title, detail, evidence, *, severity="low", group="", stage=0):
    return {
        "category": category, "title": title, "detail": detail, "anchor": evidence[0]["anchor"],
        "severity": severity, "evidence": evidence, "group": group, "blocking": False, "stage": stage,
    }


def _language_and_units(blocks, body, affected):
    forms = defaultdict(dict)
    for index in body:
        block = blocks[index]
        if block["kind"] not in {"text", "table"}:
            continue
        original = block["text"]
        text = _prose(original)
        active = _active(affected, index)
        if active and block["kind"] == "text":
            for word, match in {match[0]: match for match in TYPO_PATTERN.finditer(text)}.items():
                yield _finding(
                    "编辑校对", f"疑似错别字「{word}」",
                    f"在此语境核对是否应写作「{TYPOS[word]}」。",
                    [_evidence(blocks, index, text=_snippet(original, *match.span()))],
                    group=f"typo:{word}",
                )
            for pattern, title, detail in LANGUAGE_RULES:
                match = pattern.search(text)
                if match:
                    yield _finding("编辑校对", title, detail,
                                   [_evidence(blocks, index, text=_snippet(original, *match.span()))])
        spacing_seen = set()
        for match in QUANTITY.finditer(text):
            form = match["unit"]
            if form in {"克", "米", "帕"} and re.match(r"[拉兰线饭粉粒氏布]", text[match.end():]):
                continue
            key = CANONICAL_UNITS[form]
            occurrence = _evidence(blocks, index, text=_snippet(original, *match.span()))
            existing = forms[key].get(form)
            if existing is None or active and not _active(affected, existing["anchor"]):
                forms[key][form] = occurrence
            if active and form.isascii() and not match["space"] and form not in spacing_seen:
                spacing_seen.add(form)
                yield _finding(
                    "体例用语", f"数值与单位「{form}」之间缺少空格",
                    f"请按本稿体例核对是否应写为「{match['number']} {form}」。",
                    [occurrence], group=f"unit-spacing:{form}",
                )
    for key, occurrences in forms.items():
        if len(occurrences) < 2:
            continue
        evidence = sorted(occurrences.values(), key=lambda item: not _active(affected, item["anchor"]))
        if _active(affected, evidence[0]["anchor"]):
            yield _finding(
                "体例用语", f"单位写法不一致「{key}」",
                f"可解析正文使用了「{'、'.join(occurrences)}」。请按本稿体例统一同一单位的写法。",
                evidence,
            )


def _chapter_value(raw):
    if raw.isdecimal():
        return int(raw)
    digits = {char: value for value, char in enumerate("零一二三四五六七八九")}
    digits.update(〇=0, 两=2)
    if all(char in digits for char in raw):
        return int("".join(str(digits[char]) for char in raw))
    total = pending = 0
    for char in raw:
        if char in {"十", "百"}:
            total += (pending or 1) * (10 if char == "十" else 100)
            pending = 0
        else:
            pending = digits[char]
    return total + pending


def _numbered_label(text, kind="text"):
    text = _normal(text).strip()
    chapter = CHAPTER.match(text)
    if chapter and (kind == "heading" or not re.search(r"[。！？；，]", text)
                    and not re.match(r"[的中里是至到与]", text[chapter.end():])):
        return "章", (_chapter_value(chapter[1] or chapter[2]),)
    section = SECTION.match(text) if kind == "heading" else None
    if section:
        return "节", tuple(int(part) for part in section[1].split("."))
    caption = CAPTION.match(text)
    if caption:
        title = caption["title"].strip()
        if title and not re.match(r"^(?:所示|显示|展示|描述|给出|表明|中[,， ]|与|的)", title):
            return caption["name"], tuple(int(part) for part in re.split(r"\s*[.－—–-]\s*", caption["number"]))
    formula = FORMULA.match(text) or (FORMULA_TAIL.search(text) if kind == "formula" else None)
    if formula:
        return "公式", tuple(int(part) for part in re.split(r"[.－—–-]", formula[1]))
    return None


def _chapter_key(block, scope):
    return scope if scope is not None and scope >= 0 else block.get("location", "")


def _numbering(blocks, body, scopes, affected):
    series = defaultdict(list)
    for index in body:
        block = blocks[index]
        label = _numbered_label(block["text"], block["kind"])
        if label:
            name, number = label
            series[name, number[:-1]].append((number[-1], index))
    for (name, prefix), entries in series.items():
        by_chapter = defaultdict(list)
        for value, anchor in entries:
            by_chapter[_chapter_key(blocks[anchor], scopes[anchor])].append((value, anchor))
        localized = not prefix and name in {"图", "表", "公式"} and len(by_chapter) > 1 and all(
            chunk[0][0] == 1 for chunk in by_chapter.values()
        )
        for chunk in by_chapter.values() if localized else [entries]:
            yield from _numbering_findings(blocks, name, prefix, chunk, affected)


def _numbering_findings(blocks, name, prefix, entries, affected):
    def display(value):
        return "-".join(str(item) for item in (*prefix, value))

    by_number = {}
    previous = None
    for value, index in entries:
        other = by_number.get(value)
        if other is not None and _active(affected, index, other):
            yield _finding(
                "编号检查", f"{name}编号重复", f"发现两个编号为 {display(value)} 的{name}。请核对是否为有意复用。",
                [_evidence(blocks, index), _evidence(blocks, other, "同编号位置")],
            )
        elif previous and value < previous[0] and _active(affected, index, previous[1]):
            yield _finding(
                "编号检查", f"{name}编号顺序待核对",
                f"编号 {display(value)} 位于 {display(previous[0])} 之后。",
                [_evidence(blocks, index), _evidence(blocks, previous[1], "上一个编号")],
            )
        by_number.setdefault(value, index)
        previous = (value, index)
    ordered = sorted(by_number)
    for before, after in zip(ordered, ordered[1:]):
        first, last = by_number[before], by_number[after]
        if after - before > 1 and _active(affected, first, last):
            missing = display(before + 1) if after == before + 2 else f"{display(before + 1)} 至 {display(after - 1)}"
            yield _finding(
                "编号检查", f"{name}编号不连续",
                f"在 {display(before)} 和 {display(after)} 之间未找到 {missing}。请查看原件是否缺号。",
                [_evidence(blocks, last), _evidence(blocks, first, "前一编号")],
            )


def _reference_number(text):
    match = REF_NUMBER.match(text)
    if match:
        return int(next(value for value in match.groups() if value is not None)), match.end()
    return None


def _reference_entries(text, packed=False):
    first = _reference_number(text)
    if not first:
        return
    number, start = first
    if packed and text.lstrip().startswith("["):
        for match in PACKED_REF_NUMBER.finditer(text,start):
            next_number = int(match[1])
            entry = text[start:match.start()].strip()
            closed = entry.endswith((".","。"))
            if (closed and next_number >= number
                    or next_number > number and REFERENCE_PRINT_DATA.search(entry)):
                yield number, entry
                number, start = next_number, match.end()
    yield number, text[start:].strip()


def _inline_numbers(text):
    for match in REF_RUN.finditer(text):
        numbers = set()
        for token in re.split(r"\s*[,、;]\s*", match[1].strip()):
            parts = re.split(r"\s*[~—–-]\s*", token)
            if len(parts) == 1:
                numbers.add(int(parts[0]))
            elif len(parts) == 2:
                first, last = map(int, parts)
                if 0 < first <= last <= first + 100:
                    numbers.update(range(first, last + 1))
        if numbers - {0}:
            yield match, numbers - {0}


def _bibliography_order(blocks):
    """Read the reference pages by columns, retaining the stored source indexes."""
    in_back_matter = False
    for page, entries in groupby(enumerate(blocks), key=lambda row:
                                 row[1].get("page") if row[1].get("part") == "pdf" else None):
        rows = list(entries)
        in_back_matter = in_back_matter or any(BIB_TITLE.fullmatch(_normal(block["text"]).strip())
                                               for _,block in rows)
        if not page or not in_back_matter:
            yield from rows
            continue
        width = next((block["pageWidth"] for _,block in rows if block.get("pageWidth")),0)
        middle = width / 2
        columns = set()
        dividers = []
        for _,block in rows:
            if block["kind"] == "image" or not block.get("bbox"):
                continue
            left, top, right, _ = block["bbox"]
            if left < middle - 8 and right > middle + 8:
                dividers.append(top)
            elif right - left >= width * .24:
                columns.add(left >= middle - 8)
        if not width or len(columns) < 2:
            yield from rows
            continue
        dividers.sort()

        def position(row):
            index, block = row
            if block["kind"] == "image" or not block.get("bbox"):
                return len(dividers)+1, 0, 0, index
            left, top, right, _ = block["bbox"]
            spans_columns = left < middle - 8 and right > middle + 8
            band = bisect_left(dividers,top) * 2 + int(spans_columns)
            return band, int(left >= middle - 8), top, index

        yield from sorted(rows,key=position)


def _reference_sections(blocks):
    sections = set()
    chapter = -1
    owner = None
    scopes = {}
    references = defaultdict(dict)
    footnote_scopes = {}
    body = []
    for index, block in _bibliography_order(blocks):
        text = _normal(block["text"]).strip()
        if CODE.match(text):
            continue
        if block["kind"] == "footnote":
            body.append(index)
            note_id = block.get("location", "").removeprefix("脚注 ").strip()
            scopes[index] = footnote_scopes.get(note_id)
            continue
        if BIB_TITLE.fullmatch(text):
            owner = chapter
            sections.add(chapter)
            continue
        kind = block["kind"]
        label = _numbered_label(text, kind)
        if label and label[0] == "章":
            chapter = index
        if owner is not None and (kind == "heading" or label and label[0] == "章" or END_BIBLIOGRAPHY.match(text)):
            owner = None
        for reference in block.get("notes", []):
            footnote_scopes.setdefault(str(reference), chapter)
        if owner is not None:
            for number, entry in _reference_entries(text, packed=block.get("part") == "pdf"):
                references[owner].setdefault(number, []).append((index, entry))
        else:
            body.append(index)
            scopes[index] = chapter
    # A single bibliography at the end normally covers earlier chapters.
    terminal = chapter if sections == {chapter} else None
    return sections, terminal, references, body, scopes


def _citation_usages(blocks, structure):
    _, terminal, _, body, scopes = structure
    for index in body:
        if blocks[index]["kind"] not in {"text", "table", "footnote"}:
            continue
        text = _prose(_normal(blocks[index]["text"]))
        matches = list(_inline_numbers(text))
        if not matches:
            continue
        scope = terminal if terminal is not None else scopes[index]
        if scope is None:
            continue
        numbers = set().union(*(values for _, values in matches))
        yield index, scope, numbers, text, matches[0][0].span()


def citation_alignment(blocks):
    """Aligned citation locations, including missing and ambiguous references."""
    structure = _reference_sections(blocks)
    sections, _, references, _, _ = structure
    total = 0
    citations = []
    for index, scope, numbers, text, span in _citation_usages(blocks, structure):
        if not sections and not _reference_context(text):
            continue
        total += 1
        if len(citations) >= 150:
            continue
        matches = []
        for number in sorted(numbers)[:100]:
            targets = references[scope].get(number, [])
            matches.append({
                "number": number,
                "targetCount": len(targets),
                "targets": [{"anchor":target,"page":blocks[target].get("page"),
                             "text":_snippet(f"[{number}] {entry}")}
                            for target,entry in targets[:8]],
            })
        citations.append({
            "anchor": index, "text": _snippet(blocks[index]["text"], *span),
            "matchCount": len(numbers), "matches": matches,
        })
    return {"citations": citations, "total": total,
            "referenceCount":sum(len(entries) for entries in references.values())}


def _reference_context(text):
    return re.search(r"参考|引文|引用|文献|参见|详见|见\s*\[", text)


def _missing_reference(section_exists, numbers, known):
    missing = sorted(numbers.difference(known))
    if not missing:
        return None
    formatted = "、".join(f"[{number}]" for number in missing[:20])
    suffix = " 等" if len(missing) > 20 else ""
    title = "正文引文缺少对应条目" if section_exists else "未定位可对照的参考文献表"
    detail = f"正文引用 {formatted}{suffix} 未在可解析的参考文献表中找到。请核对文末条目或原件编号。"
    return title, detail


def citation_gaps(blocks):
    """Return the current finding or None for each recognized citation anchor."""
    structure = _reference_sections(blocks)
    sections, _, references, _, _ = structure
    # A previously misordered bibliography block may have been flagged as prose.
    gaps = {index: None for entries in references.values()
            for alternatives in entries.values() for index,_ in alternatives}
    gaps.update({index:_missing_reference(scope in sections,numbers,references[scope])
                 for index,scope,numbers,text,_ in _citation_usages(blocks,structure)
                 if sections or _reference_context(text)})
    return gaps


def _citation_findings(blocks, structure, affected, author, watchlist):
    sections, _, references, _, _ = structure
    usages = defaultdict(set)
    for index, scope, numbers, text, _ in _citation_usages(blocks, structure):
        usages[scope].update(numbers)
        if not _active(affected, index):
            continue
        if not sections and not _reference_context(text):
            continue
        gap = _missing_reference(scope in sections,numbers,references[scope])
        if gap:
            title,detail = gap
            yield _finding("引文核验",title,detail,[_evidence(blocks,index)],severity="normal",stage=2)
    for scope, entries in references.items():
        unreferenced = []
        for number, alternatives in entries.items():
            first = alternatives[0][0]
            for index, _ in alternatives[1:]:
                if _active(affected, first, index):
                    yield _finding(
                        "引文核验", "文末参考文献编号重复", f"参考文献 [{number}] 对应多条记录。",
                        [_evidence(blocks, index), _evidence(blocks, first, "同编号条目")],
                        severity="normal", stage=2,
                    )
            if number not in usages[scope] and _active(affected, first):
                unreferenced.append((number, first))
        if unreferenced:
            labels = "、".join(f"[{number}]" for number, _ in unreferenced[:15])
            suffix = f" 等 {len(unreferenced)} 项" if len(unreferenced) > 15 else ""
            yield _finding(
                "引文核验", "文末条目未见对应正文引用",
                f"{labels}{suffix} 未在可解析正文中找到。请核对是否属于扩展阅读或脚注。",
                [_evidence(blocks, index, "文末条目") for _, index in unreferenced[:8]], stage=2,
            )
        yield from _citation_structure(blocks, entries, affected, author, watchlist)


def _match_key(text):
    return re.sub(r"[\W_]+", "", _normal(text)).casefold()


def _citation_structure(blocks, entries, affected, author, watchlist):
    names = {_match_key(name) for name in re.split(r"[,，、;/；和与]", author) if name.strip()}
    approved_journals = {}
    for entry in watchlist:
        if isinstance(entry, str):
            name, version = entry, ""
        else:
            name = entry.get("title") or entry.get("name", "")
            version = entry.get("version") or entry.get("edition") or entry.get("year")
        if name:
            approved_journals[_match_key(name)] = str(version or "")
    journals = defaultdict(list)
    watch = defaultdict(list)
    same_author = []
    unique = set()
    for alternatives in entries.values():
        if len(alternatives) != 1:
            continue
        index, body = alternatives[0]
        signature = _match_key(body)
        if not signature or signature in unique:
            continue
        unique.add(signature)
        author_region = re.split(r"[.。]\s*", body, maxsplit=1)[0]
        author_names = {_match_key(token) for token in re.split(r",|，|、|;|；|等\b", author_region)}
        if names.intersection(author_names):
            same_author.append(index)
        match = JOURNAL.search(body)
        if match:
            name = match[1].strip()
            key = _match_key(name)
            if key:
                journals[key].append((name, index))
                if key in approved_journals:
                    watch[key].append((name, index))
    if len(unique) >= 5 and len(same_author) >= 3 and len(same_author) * 10 >= len(unique) * 3:
        yield from _structure_issue(
            blocks, affected, "引文中同名作者出现较集中",
            f"{len(unique)} 条可识别文献中有 {len(same_author)} 条含与稿件作者同名的署名。请核对作者身份和引用用途。",
            same_author,
        )
    total_journals = sum(len(values) for values in journals.values())
    for matches in journals.values():
        if total_journals >= 5 and len(matches) >= 3 and len(matches) * 5 >= total_journals * 3:
            yield from _structure_issue(
                blocks, affected, "引文的期刊来源集中",
                f"{total_journals} 条可识别期刊文献中，{len(matches)} 条出自《{matches[0][0]}》。请核对来源覆盖。",
                [index for _, index in matches],
            )
    for key, matches in watch.items():
        version = f"（{approved_journals[key]}）" if approved_journals[key] else ""
        yield from _structure_issue(
            blocks, affected, "引文命中已配置的关注期刊名录",
            f"《{matches[0][0]}》与编辑部关注名录{version}的期刊名称一致。请核对版次、来源及引用用途。",
            [index for _, index in matches],
        )


def _structure_issue(blocks, affected, title, detail, indexes):
    current = [index for index in indexes if _active(affected, index)]
    if current:
        anchors = [current[0], *[index for index in indexes if index != current[0]]]
        yield _finding("引文核验", title, detail,
                       [_evidence(blocks, index, "来源条目") for index in anchors[:6]], stage=2)


def _metric(value):
    compact = re.sub(r"\s+", "", value).strip("：:")
    return re.sub(r"^(?:的|其|各年)+", "", compact)


def _year_values(text):
    for match in YEAR_SERIES.finditer(text):
        years = [int(year) for year in re.findall(YEAR, match["years"])]
        parsed = [part for part in VALUE_PARTS.finditer(match["values"])]
        if len(years) == 2 and re.search(r"至|到|—|-|~", match["years"]):
            years = list(range(years[0], years[-1] + 1)) if 0 < years[-1] - years[0] <= 11 else []
        series = _align_values(years, parsed)
        if series is not None:
            yield _metric(match["metric"]), *series, match.span()
    for match in YEAR_PAIRS.finditer(text):
        matches = list(YEAR_PAIR.finditer(match["pairs"]))
        years = [int(pair["year"]) for pair in matches]
        parsed = [VALUE_PARTS.match(pair["value"]) for pair in matches]
        if any(part is None for part in parsed):
            continue
        series = _align_values(years, parsed)
        if series is not None:
            yield match["metric"], *series, match.span()


def _align_values(years, parsed):
    if len(years) != len(parsed) or years != sorted(set(years)):
        return None
    units = {CANONICAL_UNITS.get(part["unit"], part["unit"]) for part in parsed if part["unit"]}
    if len(units) > 1:
        return None
    values = [(year, Decimal(part["number"])) for year, part in zip(years, parsed)]
    return values, next(iter(units), "")


def _scope_clause(text, position):
    return re.split(r"[。！？；,.，、]", text[:position])[-1]


def _same_metric(metric, clause):
    if re.fullmatch(METRIC, metric):
        return metric in re.findall(METRIC, clause)
    return bool(metric) and metric in clause


def _trend_findings(blocks, body, scopes, affected):
    for offset, index in enumerate(body):
        block = blocks[index]
        if block["kind"] not in {"text", "table"}:
            continue
        text = _prose(_normal(block["text"]))
        quantities = list(_year_values(text))
        if not quantities:
            continue
        nearby = [index]
        if len(body) > offset + 1:
            next_index = body[offset + 1]
            following = blocks[next_index]
            if following["kind"] == "text" and _chapter_key(block, scopes[index]) == _chapter_key(
                    following, scopes[next_index]):
                nearby.append(next_index)
        for metric, values, unit, span in quantities:
            for candidate in nearby:
                if not _active(affected, index, candidate):
                    continue
                claim = _prose(_normal(blocks[candidate]["text"]))
                for match in TREND.finditer(claim):
                    clause = _scope_clause(claim, match.start())
                    rest = re.split(r"[。！？；,.，、?!]", claim[match.end():], maxsplit=1)[0]
                    implicit = candidate == index and span[1] <= match.start() <= span[1] + 32 and (
                        not re.search(r"[。；;！？!?]", claim[span[1]:match.start()]) and IMPLICIT_METRIC.fullmatch(clause)
                    )
                    explicit = _same_metric(metric, clause)
                    referenced = len(quantities) == 1 and re.search(r"该指标|此指标|上述数据", clause)
                    if QUALIFIER.search(clause) or TREND_DENIAL.search(rest) or not (implicit or explicit or referenced):
                        continue
                    increasing = match[1] in INCREASING
                    pairs = zip(values, values[1:])
                    mismatch = next(((before, after) for before, after in pairs
                                     if (after[1] <= before[1] if increasing else after[1] >= before[1])), None)
                    if mismatch is None:
                        continue
                    (before_year, before), (after_year, after) = mismatch
                    delta = after - before
                    change = "没有变化" if not delta else f"{'增加' if delta > 0 else '减少'} {abs(delta):f}"
                    if delta and unit == "%":
                        change += " 个百分点"
                    elif delta and unit:
                        change += f" {unit}"
                    detail = (f"{metric}从 {before_year} 年的 {before:f}{unit} 变为 {after_year} 年的 {after:f}{unit}，"
                              f"{change}，与「{match[0]}」的表述不符。请在统计口径一致的前提下复核。")
                    yield _finding(
                        "数据矛盾", f"「{match[0]}」与序列数值不一致", detail,
                        [_evidence(blocks, candidate, "趋势表述", _snippet(blocks[candidate]["text"], *match.span())),
                         _evidence(blocks, index, "分年数值", _snippet(block["text"], *span))],
                        severity="high", stage=2,
                    )
                    break


def _scalar_findings(blocks, body, affected):
    previous = {}
    reported = set()
    for index in body:
        block = blocks[index]
        if block["kind"] != "text":
            continue
        text = _prose(_normal(block["text"]))
        for pattern in SCALAR_PATTERNS:
            for match in pattern.finditer(text):
                unit = CANONICAL_UNITS.get(match["unit"], match["unit"])
                key = (_metric(match["subject"]), match["year"], match["metric"], unit)
                value = Decimal(match["number"])
                old_value, earlier, excerpt = previous.setdefault(
                    key, (value, index, _snippet(text, *match.span()))
                )
                if old_value == value or earlier == index or key in reported or not _active(affected, index, earlier):
                    continue
                subject, year, metric, _ = key
                reported.add(key)
                yield _finding(
                    "数据矛盾", "同一对象、年份与指标的数值不一致",
                    f"{subject} {year} 年的{metric}分别记为 {old_value:f}{unit} 和 {value:f}{unit}。"
                    "请核对数据来源和统计口径。",
                    [_evidence(blocks, index, "后一处数值", _snippet(text, *match.span())),
                     _evidence(blocks, earlier, "前一处数值", excerpt)],
                    severity="normal", stage=2,
                )


def _trigrams(text):
    text = _match_key(text)[:480]
    return {text[index:index + 3] for index in range(len(text) - 2)}


def _style_patterns(blocks, body, scopes, affected):
    definitions = defaultdict(list)
    templates = defaultdict(dict)
    for index in body:
        block = blocks[index]
        if block["kind"] != "text":
            continue
        text = block["text"].strip()
        chapter = _chapter_key(block, scopes[index])
        definition = DEFINITION.match(text)
        if definition and len(definition[2]) >= 22:
            definitions[definition[1]].append((chapter, index, _trigrams(definition[2]), _match_key(definition[2])))
        for pattern in TEMPLATES:
            if pattern.match(text):
                if chapter not in templates[pattern.pattern] or _active(affected, index):
                    templates[pattern.pattern][chapter] = index
                break
    for term, entries in definitions.items():
        if len(entries) < 3:
            continue
        representatives = {}
        for entry in entries:
            representatives.setdefault(entry[0], entry)
        candidates = list(representatives.values())[:32]
        for chapter, index, grams, text in entries:
            if not _active(affected, index):
                continue
            similar = [other_index for other, other_index, other_grams, other_text in candidates
                       if other != chapter and text != other_text and
                       len(grams & other_grams) >= 0.75 * len(grams | other_grams)]
            if len(similar) >= 2:
                yield _finding(
                    "内容重复", f"多处近似复述「{term}」的定义",
                    "不同章节出现高度相似的定义。请对照是否增加了新条件、数据或应用示例。",
                    [_evidence(blocks, index), *[_evidence(blocks, other, "近似定义") for other in similar[:3]]],
                )
                break
    for chapters in templates.values():
        indexes = list(chapters.values())
        active = [index for index in indexes if _active(affected, index)]
        if len(chapters) >= 3 and active:
            counterpart = [index for index in indexes if index != active[0]]
            yield _finding(
                "内容重复", "跨章段落沿用同一表述模板",
                "不同章节的开头与结尾结构高度相似。请检查各处是否各自给出了必要的内容和证据。",
                [_evidence(blocks, index) for index in [active[0], *counterpart[:3]]],
            )


def affected_by_changes(blocks, changes, affected):
    """Recheck surviving citations and numbered neighbors when their targets were removed."""
    impacted = set(affected or ())
    removed_refs = set()
    removed_usages = set()
    removed_series = set()
    removed_bibliography = False
    for change in changes:
        for old in change["before"]:
            text = _normal(old).strip()
            entries = list(_reference_entries(text,packed=True))
            if entries:
                removed_refs.update(number for number,_ in entries)
            else:
                for _, numbers in _inline_numbers(_prose(text)):
                    removed_usages.update(numbers)
            label = _numbered_label(text)
            if label:
                removed_series.add((label[0], label[1][:-1]))
            if BIB_TITLE.fullmatch(text):
                removed_bibliography = True
    if not (removed_refs or removed_usages or removed_series or removed_bibliography):
        return impacted
    for index, block in enumerate(blocks):
        text = _normal(block["text"])
        if (removed_bibliography or removed_refs) and any(
                removed_bibliography or removed_refs & values for _, values in _inline_numbers(text)):
            impacted.add(index)
        entry = _reference_number(text)
        if entry and entry[0] in removed_usages:
            impacted.add(index)
        label = _numbered_label(text, block["kind"])
        if label and (label[0], label[1][:-1]) in removed_series:
            impacted.add(index)
    return impacted


def check_blocks(blocks, affected=None, author="", watchlist=()):
    """Return findings using the existing issue fields and zero-based anchors."""
    if not blocks or affected is not None and not affected:
        return []
    structure = _reference_sections(blocks)
    _, _, _, indexes, scopes = structure
    body = [index for index in indexes if blocks[index]["kind"] not in {"image", "footnote"}]
    issues = [
        *_language_and_units(blocks, body, affected),
        *_numbering(blocks, body, scopes, affected),
        *_citation_findings(blocks, structure, affected, author, watchlist),
        *_trend_findings(blocks, body, scopes, affected),
        *_scalar_findings(blocks, body, affected),
        *_style_patterns(blocks, body, scopes, affected),
    ]
    return sorted(issues, key=lambda issue: issue["anchor"])
