"""Local layout and source-data verification for manuscripts and proofs."""
from collections import defaultdict
import csv
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from posixpath import normpath
import re
from shutil import which
import struct
import subprocess
from tempfile import TemporaryDirectory
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


XHTML = "{http://www.w3.org/1999/xhtml}"
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MATH = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
POSITION = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
RELATION = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_PAGES = 1200
MAX_ROWS = 15000
MAX_COLUMNS = 512

FIGURE_NUMBER = r"\d+(?:\s*[.\-–—]\s*\d+)*"
FIGURE_PREFIX = rf"(?:图\s*|(?<!\w)fig(?:ure)?\.?\s+)({FIGURE_NUMBER})(?!\d|\s*[.\-–—]\s*\d)"
FIGURE_CAPTION = re.compile(rf"^\s*{FIGURE_PREFIX}(?:\s*[.:、]\s*|\s+)(?=[^\s。；;])", re.I)
FIGURE_MENTION = re.compile(FIGURE_PREFIX, re.I)
APPENDIX_HEADING = re.compile(r"^\s*(?:附录\s*|Appendix\s+)([A-Z]|\d+)(?:[.:、\s]|$)", re.I)
APPENDIX_FIGURE = re.compile(rf"(?:附录\s*|Appendix\s+)([A-Z]|\d+)\s*(?:中(?:的)?)?\s*{FIGURE_PREFIX}", re.I)
MAP_WORDS = re.compile(r"行政区划|行政边界|国界(?:线)?|领土|疆域|经纬网|地图|地理分布图|边境线|administrative map|political map", re.I)
THIRD_PARTY = re.compile(r"转载(?:自|于)?|引自|改编自|改绘自|摘自|图片来源|摄影[:：]|图源[:：]|source[s]?[:：]|reproduced from|adapted from", re.I)
LOCAL_SOURCE = re.compile(r"作者(?:自制|供图|绘制|绘图)|本书编者绘制|自摄|原创|author(?:s)?(?:'s| own)", re.I)
CAPTION_SOURCE = re.compile(r"^\s*(?:来源|资料来源|图片来源|图源|供图|source)\s*[:：]", re.I)


class ProofError(ValueError):
    """A local proof or data file cannot be read."""


def _block(text, kind, location, part, **metadata):
    return {"text": text, "kind": kind, "location": location, "part": part, **metadata}


def _issue(category, title, detail, anchor, evidence=None, severity="normal", stage=4):
    return {"category": category, "title": title, "detail": detail, "anchor": anchor,
            "severity": severity, "evidence": evidence or [], "group": "", "blocking": False,
            "stage": stage}


def _local_command(command, cwd, timeout=20, stdin=None, required=False):
    executable = which(command[0])
    if not executable:
        if required:
            raise ProofError(f"未找到 {command[0]}。安装 Poppler 并将其 bin 目录加入 PATH 后，可自动检查 PDF 校样。")
        return b""
    try:
        result = subprocess.run([executable, *command[1:]], input=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                cwd=cwd, timeout=timeout, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        if required:
            raise ProofError(f"{command[0]} 无法在本机完成解析，请检查安装或拆分文档后重试。") from exc
        return b""
    if not result.returncode and len(result.stdout) <= 64 * 1024 * 1024:
        return result.stdout
    if required:
        raise ProofError("PDF 解析未完成。请检查文件是否有效、未加密，或拆分校样后重试。")
    return b""


def _coordinate_box(element):
    return [round(float(element.attrib[key]), 2) for key in ("xMin", "yMin", "xMax", "yMax")]


def _word_text(words):
    full = ""
    for word in words:
        if full and word and not (re.search(r"[\u3000-\u303f\u3400-\u9fff，。；：]", full[-1])
                                  or re.match(r"[\u3000-\u303f\u3400-\u9fff，。；：,.!?%）]", word[0])):
            full += " "
        full += word
    return full


def _pdf_rasters(raw, directory):
    output = _local_command(["pdfimages", "-l", str(MAX_PAGES), "-list", "-"], directory, stdin=raw)
    results = defaultdict(list)
    for line in output.decode("ascii", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 14 or not fields[0].isdigit() or fields[2] != "image":
            continue
        try:
            page, ordinal = map(int, fields[:2])
            width, height = map(int, fields[3:5])
            x_ppi, y_ppi = map(float, fields[12:14])
            bit_depth = int(fields[7])
        except ValueError:
            continue
        if not (x_ppi > 0 and y_ppi > 0):
            continue
        results[page].append({"page": page, "source": f"第 {page} 页图件 {ordinal + 1}",
                              "width": width, "height": height, "lineArt": bit_depth == 1,
                              "xPpi": x_ppi, "yPpi": y_ppi,
                              "printWidth": round(width * 72 / x_ppi, 2),
                              "printHeight": round(height * 72 / y_ppi, 2)})
    return results, bool(output)


def _pdf_raster_coordinates(raw, directory, rasters):
    """Add bounding boxes only where output from both Poppler tools agrees."""
    if not rasters or sum(map(len, rasters.values())) > 250 or not which("pdftohtml"):
        return
    output = _local_command(["pdftohtml", "-q", "-l", str(MAX_PAGES), "-xml", "-noroundcoord",
                             "-zoom", "1", "-stdout", "-fmt", "jpg", "-", "layout"], directory,
                            timeout=12, stdin=raw)
    try:
        layout = ET.fromstring(output)
    except ET.ParseError:
        return
    ocr_budget = 4
    for page in layout.findall("page"):
        images = page.findall("image")
        placed = rasters.get(int(page.get("number", "0")), [])
        if len(placed) != len(images):
            continue
        for graphic, image in zip(placed, images):
            try:
                left, top, width, height = (float(image.attrib[key]) for key in ("left", "top", "width", "height"))
            except (KeyError, ValueError):
                continue
            if (abs(width - graphic["printWidth"]) <= max(2, width * 0.04)
                    and abs(height - graphic["printHeight"]) <= max(2, height * 0.04)):
                graphic["bbox"] = [round(value, 2) for value in (left, top, left + width, top + height)]
                try:
                    preview = directory / Path(image.get("src", "")).name
                    if preview.is_file() and preview.stat().st_size <= 5 * 1024 * 1024:
                        binary = preview.read_bytes()
                        graphic["digest"] = sha256(binary).hexdigest()
                        if ocr_budget and which("tesseract") and graphic["width"] >= 250 and graphic["height"] >= 150:
                            graphic["numericLabels"] = _ocr_labels(binary, directory)
                            ocr_budget -= 1
                except OSError:
                    pass


def _ocr_language(directory):
    if not which("tesseract") or not which("pdftoppm"):
        return ""
    languages = set(_local_command(["tesseract", "--list-langs"], directory, timeout=3).decode().splitlines())
    return "+".join(language for language in ("chi_sim", "eng") if language in languages)


def _ocr_labels(binary, directory):
    output = _local_command(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"],
                            directory, timeout=5, stdin=binary)
    results = []
    for token in csv.DictReader(StringIO(output.decode("utf-8", errors="replace")), delimiter="\t"):
        value = (token.get("text") or "").strip()
        if not re.fullmatch(r"[-−+]?\d+(?:[.,]\d+)?[%％]?", value):
            continue
        try:
            if float(token["conf"]) < 85:
                continue
            x, y, width, height = (int(token[key]) for key in ("left", "top", "width", "height"))
        except (KeyError, ValueError, TypeError):
            continue
        results.append({"value": value, "pixelBox": [x, y, x + width, y + height], "source": "本机 OCR 待复核"})
    return results[:40]


def _ocr_scan(raw, directory, page, language):
    png = _local_command(["pdftoppm", "-r", "144", "-f", str(page), "-l", str(page),
                          "-singlefile", "-png", "-"], directory, timeout=8, stdin=raw)
    if not png:
        return []
    tsv = _local_command(["tesseract", "stdin", "stdout", "-l", language, "tsv"],
                         directory, timeout=8, stdin=png)
    lines = defaultdict(list)
    for word in csv.DictReader(StringIO(tsv.decode("utf-8", errors="replace")), delimiter="\t"):
        try:
            text = (word.get("text") or "").strip()
            if not text or float(word["conf"]) < 75:
                continue
            x, y, width, height = (int(word[key]) for key in ("left", "top", "width", "height"))
            line = tuple(word[key] for key in ("block_num", "par_num", "line_num"))
        except (TypeError, KeyError, ValueError):
            continue
        lines[line].append((text, x, y, x + width, y + height))
    return [(_word_text(word[0] for word in words),
             [min(word[1] for word in words) / 2, min(word[2] for word in words) / 2,
              max(word[3] for word in words) / 2, max(word[4] for word in words) / 2])
            for words in lines.values()]


def parse_pdf(raw):
    if not raw.lstrip().startswith(b"%PDF-"):
        raise ProofError("无法识别 PDF 文件。")
    directory = Path(__file__).resolve().parent
    output = _local_command(["pdftotext", "-bbox-layout", "-enc", "UTF-8", "-l", str(MAX_PAGES),
                             "-cropbox", "-", "-"], directory, stdin=raw, required=True)
    try:
        document = ET.fromstring(output)
    except ET.ParseError as exc:
        raise ProofError("PDF 校样的版面坐标无法读取。") from exc
    rasters, raster_check = _pdf_rasters(raw, directory)
    if rasters and which("pdftohtml"):
        try:
            with TemporaryDirectory(prefix=".proof-", dir=directory) as temporary:
                _pdf_raster_coordinates(raw, Path(temporary), rasters)
        except OSError:
            pass
    pages = document.findall(f".//{XHTML}page")
    blocks = []
    scanned = 0
    language = None
    for number, page in enumerate(pages, 1):
        page_meta = {"page": number, "pageWidth": float(page.get("width", "0")),
                     "pageHeight": float(page.get("height", "0"))}
        location = f"第 {number} 页"
        page_blocks = []
        for group in page.findall(f".//{XHTML}block"):
            lines = [_word_text(word.text or "" for word in line.findall(f"{XHTML}word"))
                     for line in group.findall(f"{XHTML}line")]
            text = _word_text(line for line in lines if line).strip()
            if text:
                page_blocks.append(_block(text, "text", location, "pdf",
                                          bbox=_coordinate_box(group), **page_meta))
        images = rasters.get(number, [])
        if not page_blocks and images:
            scanned += 1
            if language is None:
                language = _ocr_language(directory)
            text_boxes = _ocr_scan(raw, directory, number, language) if language and scanned <= 4 else []
            for text, box in text_boxes:
                page_blocks.append(_block(text, "text", location, "pdf", bbox=box, ocr=True, **page_meta))
            page_blocks.append(_block("[扫描页待复核]", "image", location, "pdf", scanNeeded=True, **page_meta))
        for image in images:
            page_blocks.append(_block(f"[{image['source']}]", "image", location, "pdf",
                                      images=[image], bbox=image.get("bbox"), **page_meta))
        blocks.extend(page_blocks)
    if not blocks:
        blocks.append(_block("[PDF 未提取到可核验的文字或图件]", "image", "校样", "pdf", scanNeeded=True))
    if len(pages) == MAX_PAGES:
        blocks.append(_block("[超过一次检查的页数，请将后续页面分卷上传]", "image", "校样", "pdf", scanNeeded=True))
    if not raster_check:
        blocks.append(_block("[位图清点未完成，请核实 Poppler 的 pdfimages 工具]",
                             "image", "校样", "pdf", scanNeeded=True))
    return blocks


def render_pdf_page(raw, page):
    """Render one crop-box page for layout review and coordinate overlays."""
    if not isinstance(page, int) or not 1 <= page <= MAX_PAGES:
        raise ProofError("PDF 页码超出可检查范围。")
    png = _local_command(["pdftoppm", "-png", "-cropbox", "-scale-to", "1400", "-f", str(page),
                          "-l", str(page), "-singlefile", "-"], Path(__file__).resolve().parent,
                         timeout=10, stdin=raw, required=True)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ProofError("无法渲染该页，请下载校样原件查看。")
    return png


def _zip_xml(archive, path, max_size=24 * 1024 * 1024):
    info = archive.getinfo(path)
    if info.file_size > max_size:
        raise ProofError("附件内部文件超过解析上限，请拆分后重试。")
    return ET.fromstring(archive.read(info))


def _relationship_paths(root, parent, allowed_root):
    paths = {}
    for node in root:
        key, target = node.get("Id"), node.get("Target", "")
        if not key or not target or node.get("TargetMode") == "External":
            continue
        path = normpath(target.lstrip("/") if target.startswith("/") else f"{parent}/{target}")
        if path.startswith(allowed_root + "/"):
            paths[key] = path
    return paths


def _image_dimensions(raw):
    if len(raw) >= 25 and raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return (*struct.unpack(">II", raw[16:24]), raw[24] == 1)
    if len(raw) >= 10 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return (*struct.unpack("<HH", raw[6:10]), False)
    if len(raw) >= 30 and raw.startswith(b"BM"):
        width, height = struct.unpack("<ii", raw[18:26])
        return width, abs(height), int.from_bytes(raw[28:30], "little") == 1
    if raw.startswith(b"\xff\xd8"):
        markers = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
        cursor = 2
        while cursor + 8 < len(raw) and raw[cursor] == 0xff:
            marker = raw[cursor + 1]
            if marker == 0xff:
                cursor += 1
                continue
            length = int.from_bytes(raw[cursor + 2:cursor + 4], "big")
            if marker in markers and length >= 7:
                height, width = struct.unpack(">HH", raw[cursor + 5:cursor + 9])
                return width, height, False
            if marker in (0xda, 0xd9) or length < 2:
                break
            cursor += length + 2
    return None


def _word_paragraphs(element):
    if element.tag == WORD + "p":
        text = "".join(node.text or "" for node in element.iter() if node.tag in (WORD + "t", MATH + "t"))
        if text.strip() or any(node.tag in (WORD + "drawing", WORD + "pict", WORD + "footnoteReference", MATH + "oMath")
                               for node in element.iter()):
            yield element
        return
    for child in element:
        yield from _word_paragraphs(child)


def _visible_fraction(crop, *sides):
    if crop is None:
        return 1.0
    try:
        cuts = [int(crop.get(side, "0")) / 100000 for side in sides]
    except ValueError:
        return 1.0
    return max(0.0, min(1.0, 1.0 - sum(cuts)))


def _word_table_data(body, blocks, indexes):
    for table_number, table in enumerate(body.iter(WORD + "tbl"), 1):
        if any(node is not table for node in table.iter(WORD + "tbl")):
            continue
        rows = []
        anchors = {}
        for ordinal, row in enumerate(table.findall(WORD + "tr"), 1):
            if row.find(f".//{WORD}gridSpan") is not None or row.find(f".//{WORD}vMerge") is not None:
                rows = []
                break
            values = []
            for cell in row.findall(WORD + "tc"):
                parts = [paragraph for paragraph in _word_paragraphs(cell) if paragraph in indexes]
                values.append(" ".join(blocks[indexes[paragraph]]["text"] for paragraph in parts))
                if parts and ordinal not in anchors:
                    anchors[ordinal] = indexes[parts[0]]
            rows.append((ordinal, values))
        totals = _table_totals(rows)
        for ordinal, values in rows:
            if ordinal not in anchors:
                continue
            block = blocks[anchors[ordinal]]
            block["tableNumber"] = table_number
            block["tableRow"] = ordinal
            block["cells"] = values
            if ordinal in totals:
                block["calculations"] = totals[ordinal]


def _word_positions(raw, blocks):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            document = _zip_xml(archive, "word/document.xml")
            rels_path = "word/_rels/document.xml.rels"
            image_paths = _relationship_paths(_zip_xml(archive, rels_path), "word", "word/media") if rels_path in archive.namelist() else {}
            body = document.find(WORD + "body")
            if body is None:
                return
            stored = {}
            indexes = {paragraph: index for index, paragraph in zip(range(len(blocks)), _word_paragraphs(body))}
            _word_table_data(body, blocks, indexes)
            ocr_budget = 4
            for paragraph, index in indexes.items():
                block = blocks[index]
                block.pop("images", None)
                for frame in paragraph.iter():
                    if frame.tag not in (POSITION + "inline", POSITION + "anchor"):
                        continue
                    extent = frame.find(POSITION + "extent")
                    if extent is None:
                        continue
                    try:
                        width_inches = int(extent.attrib["cx"]) / 914400
                        height_inches = int(extent.attrib["cy"]) / 914400
                    except (KeyError, ValueError):
                        continue
                    if not (width_inches > 0 and height_inches > 0):
                        continue
                    meta = frame.find(POSITION + "docPr")
                    alt = (meta.get("descr") or meta.get("title") or "") if meta is not None else ""
                    for bitmap in frame.iter(DRAWING + "blip"):
                        path = image_paths.get(bitmap.get(RELATION + "embed"))
                        if not path:
                            continue
                        if path not in stored:
                            try:
                                info = archive.getinfo(path)
                            except KeyError:
                                continue
                            if info.file_size > 24 * 1024 * 1024:
                                continue
                            binary = archive.read(info)
                            size = _image_dimensions(binary)
                            labels = []
                            if (size and ocr_budget and which("tesseract") and size[0] >= 250 and size[1] >= 150
                                    and re.search(r"图表|统计|数据|chart|graph", alt, re.I)):
                                labels = _ocr_labels(binary, Path(__file__).resolve().parent)
                                ocr_budget -= 1
                            stored[path] = (size, sha256(binary).hexdigest(), labels)
                        size, digest, labels = stored[path]
                        if not size or not all(value > 0 for value in size[:2]):
                            continue
                        width, height, line_art = size
                        line_art |= bool(re.search(r"线稿|线图|line.?art", alt, re.I))
                        crop = frame.find(f".//{DRAWING}srcRect")
                        cropped_width = width * _visible_fraction(crop, "l", "r")
                        cropped_height = height * _visible_fraction(crop, "t", "b")
                        if cropped_width <= 0 or cropped_height <= 0:
                            continue
                        block.setdefault("images", []).append(
                            {"source": Path(path).name, "alt": alt[:240], "digest": digest,
                             "width": width, "height": height, "visibleWidth": round(cropped_width),
                             "visibleHeight": round(cropped_height), "lineArt": line_art,
                             "xPpi": round(cropped_width / width_inches, 2), "yPpi": round(cropped_height / height_inches, 2),
                             "printWidth": round(width_inches * 72, 2), "printHeight": round(height_inches * 72, 2)})
                        if labels:
                            block["images"][-1]["numericLabels"] = labels
    except (BadZipFile, KeyError, ET.ParseError, OSError, RuntimeError):
        return


def _number(value):
    cleaned = value.strip()
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", cleaned):
        cleaned = cleaned.replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d{1,3})?", cleaned):
        return None
    try:
        number = Decimal(cleaned)
        return number if number.is_finite() and abs(number.adjusted()) < 310 else None
    except InvalidOperation:
        return None


def _display_number(value):
    return "0" if not value else str(value.normalize()) if abs(value.adjusted()) > 18 else format(value.normalize(), "f")


def _calculation(cell, formula, expected, observed):
    actual = _number(observed)
    tolerance = max(Decimal("0.0000000001"), abs(expected) * Decimal("0.0000000001"))
    return {"cell": cell, "formula": formula, "expected": _display_number(expected), "observed": observed,
            "status": "uncached" if actual is None else "checked" if abs(actual - expected) <= tolerance else "conflict"}


def _table_totals(rows):
    header = rows[0][1] if rows else []
    data_rows = []
    section_rows = []
    findings = defaultdict(list)
    for index, values in rows[1:]:
        if not values or not any(value.strip() for value in values):
            data_rows.clear()
            section_rows.clear()
            continue
        label_index = next((cell for cell, value in enumerate(values) if value.strip()), None)
        label = values[label_index].strip().casefold()
        if label not in {"合计", "总计", "小计", "total", "subtotal", "grand total"}:
            data_rows.append(values)
            section_rows.append(values)
            continue
        selected = data_rows if label in {"总计", "grand total"} else section_rows
        if len(selected) >= 2:
            for column, observed in enumerate(values):
                if column <= label_index or _number(observed) is None:
                    continue
                title = header[column] if column < len(header) else ""
                if re.search(r"编号|序号|年份|平均|占比|比例|百分比|(?:^|\s)(?:year|rate|id)(?:$|\s)|率\s*$|%", title, re.I):
                    continue
                addends = [_number(row[column]) if column < len(row) else None for row in selected]
                if all(value is not None for value in addends):
                    cell = title or f"第 {column + 1} 列"
                    formula = " + ".join(map(_display_number, addends[:8])) + (" + …" if len(addends) > 8 else "")
                    findings[index].append(_calculation(cell, formula, sum(addends), observed))
        section_rows.clear()
        if label in {"合计", "总计", "total", "grand total"}:
            data_rows.clear()
    return findings


def _csv_blocks(raw):
    try:
        value = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProofError("CSV 无法读取。请将原表另存为 UTF-8 CSV。") from exc
    try:
        sep = csv.Sniffer().sniff(value[:8000], delimiters=",\t;")
    except csv.Error:
        sep = csv.excel
    rows = []
    try:
        for cells in csv.reader(StringIO(value), dialect=sep, strict=True):
            if len(rows) >= MAX_ROWS:
                raise ProofError("表格超出一万五千行，请拆分后上传。")
            if len(cells) > MAX_COLUMNS:
                raise ProofError("表格列数超过解析上限，请拆分后上传。")
            rows.append((len(rows) + 1, [cell.strip() for cell in cells]))
    except csv.Error as exc:
        raise ProofError("CSV 表格格式有误，请在电子表格软件中重新导出。") from exc
    totals = _table_totals(rows)
    return [_block(" | ".join(values), "table", f"CSV · 第 {index} 行", "spreadsheet",
                   cells=values, calculations=totals.get(index, []))
            for index, values in rows if any(values)]


def _column_number(column):
    value = 0
    for char in column:
        value = value * 26 + ord(char) - ord("A") + 1
    return value


def _excel_address(address):
    match = re.fullmatch(r"\$?([A-Z]{1,3})\$?([1-9]\d{0,6})", address or "", re.I)
    return (int(match[2]), _column_number(match[1].upper())) if match else None


def _formula_calculation(cell, formula, observed, values, formula_cells):
    call = re.fullmatch(r"(SUM|AVERAGE|MIN|MAX|COUNT)\s*\(([^()]*)\)", formula, re.I)
    if not call:
        return None
    inputs = []
    for argument in call[2].split(","):
        limits = argument.strip().split(":")
        if len(limits) not in (1, 2) or not all(_excel_address(term) for term in limits):
            return None
        first, last = _excel_address(limits[0]), _excel_address(limits[-1])
        first_row, last_row = sorted((first[0], last[0]))
        first_col, last_col = sorted((first[1], last[1]))
        if (last_row - first_row + 1) * (last_col - first_col + 1) > 100000:
            return None
        for row in range(first_row, last_row + 1):
            for col in range(first_col, last_col + 1):
                # Recalculate from source cells. Chained formulas need a spreadsheet engine.
                if (row, col) in formula_cells:
                    return None
                value = values.get((row, col))
                if value is not None:
                    inputs.append(value)
    operations = {"SUM": sum, "AVERAGE": lambda nums: sum(nums) / len(nums),
                  "MIN": min, "MAX": max, "COUNT": lambda nums: Decimal(len(nums))}
    operator = call[1].upper()
    if inputs or operator == "COUNT":
        return _calculation(cell, "=" + formula, Decimal(operations[operator](inputs)), observed)
    return None


def _workbook_blocks(raw):
    with ZipFile(BytesIO(raw)) as archive:
        if sum(part.file_size for part in archive.infolist()) > 85 * 1024 * 1024:
            raise ProofError("电子表格解压后超出解析上限，请拆分再上传。")
        relations = _zip_xml(archive, "xl/_rels/workbook.xml.rels")
        sheets = _relationship_paths(relations, "xl", "xl/worksheets")
        workbook = _zip_xml(archive, "xl/workbook.xml")
        try:
            strings = ["".join(node.text or "" for node in entry.iter(SHEET + "t"))
                       for entry in _zip_xml(archive, "xl/sharedStrings.xml").findall(SHEET + "si")]
        except KeyError:
            strings = []
        blocks = []
        for sheet in workbook.findall(f".//{SHEET}sheet"):
            path = sheets.get(sheet.get(RELATION + "id"))
            if not path:
                continue
            rows = _zip_xml(archive, path).findall(f".//{SHEET}row")
            if len(blocks) + len(rows) > MAX_ROWS:
                raise ProofError("表格超出一万五千行，请拆分后上传。")
            entries = []
            values = {}
            formula_cells = set()
            numeric_rows = []
            for ordinal, row in enumerate(rows, 1):
                row_number = int(row.get("r", ordinal))
                cells = []
                for offset, entry in enumerate(row.findall(SHEET + "c"), 1):
                    dtype = entry.get("t", "n")
                    raw_value = entry.findtext(SHEET + "v", "")
                    formula_element = entry.find(SHEET + "f")
                    formula = (formula_element.text or "").strip() if formula_element is not None else None
                    cell = entry.get("r", "")
                    address = _excel_address(cell) or (row_number, offset)
                    if address[1] > MAX_COLUMNS:
                        raise ProofError("表格列数超过解析上限，请拆分后上传。")
                    shown = raw_value
                    if dtype == "s" and raw_value.isdigit():
                        index = int(raw_value)
                        shown = strings[index] if 0 <= index < len(strings) else ""
                    elif dtype == "inlineStr":
                        shown = "".join(text.text or "" for text in entry.iter(SHEET + "t"))
                    elif dtype == "n":
                        values[address] = _number(raw_value)
                    if formula_element is not None:
                        formula_cells.add(address)
                    while len(cells) < address[1] - 1:
                        cells.append(("", "", None))
                    cells.append((cell, shown, formula))
                entries.append((row_number, cells))
                numeric_rows.append((row_number, [cell[1] for cell in cells]))
            totals = _table_totals(numeric_rows)
            for row_number, row in entries:
                if not any(shown or formula is not None for _, shown, formula in row):
                    continue
                calculations = []
                for cell, shown, formula in row:
                    if formula is None:
                        continue
                    result = _formula_calculation(cell,formula,shown,values,formula_cells)
                    calculations.append(result or {"cell":cell,"formula":"=" + formula if formula else "共享或数组公式",
                                                   "expected":"","observed":shown,"status":"unsupported"})
                calculations.extend(totals.get(row_number, []) if not calculations else [])
                cells = [shown or ("=" + formula if formula else "[公式]" if formula is not None else "")
                         for _, shown, formula in row]
                location = f"{sheet.get('name', '工作表')} · 第 {row_number} 行"
                blocks.append(_block(" | ".join(cells), "table", location, "spreadsheet",
                                     cells=cells, calculations=calculations, formulaCoverage=True))
        return blocks


def parse_spreadsheet(name, raw):
    suffix = Path(name).suffix.lower()
    try:
        if suffix == ".xlsx":
            blocks = _workbook_blocks(raw)
        elif suffix == ".csv":
            blocks = _csv_blocks(raw)
        else:
            raise ProofError("支持 .xlsx 和 UTF-8 CSV 原始数据表。")
    except ProofError:
        raise
    except (BadZipFile, ET.ParseError, KeyError, ValueError, RuntimeError, OverflowError) as exc:
        raise ProofError("电子表格无法解析。请确认文件为有效且未加密的 XLSX。") from exc
    if not blocks:
        raise ProofError("电子表格没有可核验数据。")
    return blocks


def figure_number_key(number):
    return re.sub(r"\s*[.\-–—]\s*", "-", unicodedata.normalize("NFKC", number))


def figure_caption(text):
    text = unicodedata.normalize("NFKC", text)
    match = FIGURE_CAPTION.match(text)
    if match and not re.match(r"所示|可见|表示|给出|中|的|shows?\b|illustrates?\b", text[match.end():], re.I):
        return figure_number_key(match[1])
    return None


def figure_contexts(blocks):
    captions = {}
    mentions = defaultdict(list)
    for index, block in enumerate(blocks):
        if block["kind"] in {"image", "formula"}:
            continue
        text = block["text"]
        key = figure_caption(text)
        if key is not None:
            captions.setdefault(key, index)
            continue
        if block.get("ocr") or block["kind"] == "heading":
            continue
        for found in FIGURE_MENTION.finditer(unicodedata.normalize("NFKC", text)):
            key = figure_number_key(found[1])
            if not mentions[key] or mentions[key][-1] != index:
                mentions[key].append(index)
    return captions, mentions


def figure_links(blocks):
    captions, mentions = figure_contexts(blocks)
    result = defaultdict(list)
    for number, anchor in captions.items():
        for position in mentions[number]:
            result[position].append({"number":number,"anchor":anchor,"page":blocks[anchor].get("page")})
    return result


def appendix_captions(blocks, captions):
    by_index = {index:number for number,index in captions.items()}
    current = None
    result = {}
    for index, block in enumerate(blocks):
        match = APPENDIX_HEADING.match(unicodedata.normalize("NFKC",block["text"]))
        if match:
            current = match[1].casefold()
        if current and index in by_index:
            result[by_index[index]] = current
    return result


def _label(block, ordinal):
    location = block.get("location") or "原稿"
    if block.get("bbox"):
        return f"{location} · 左上 {block['bbox'][0]:g}, {block['bbox'][1]:g} pt"
    return f"{location} · 第 {ordinal + 1} 段"


def _evidence(blocks, index, text=None):
    block = blocks[index]
    return {"label": _label(block, index), "anchor": index,
            "text": (text if text is not None else block["text"])[:1200]}


def _image_associations(blocks, captions):
    by_page = defaultdict(list)
    for index, block in enumerate(blocks):
        if block.get("page") and block.get("images"):
            for image in block["images"]:
                if image.get("bbox"):
                    by_page[block["page"]].append((index, image["bbox"]))
    associated = {}
    for key, caption in captions.items():
        block = blocks[caption]
        if block.get("page") and block.get("bbox"):
            x0, y0, x1, y1 = block["bbox"]
            nearby = [(abs(y0 - box[3]), index) for index, box in by_page[block["page"]]
                      if box[0] < x1 + 12 and x0 < box[2] + 12 and -8 <= y0 - box[3] <= 60]
            if nearby:
                associated[key] = min(nearby)[1]
        elif not block.get("page"):
            adjacent = [index for index in (caption - 1, caption + 1)
                        if 0 <= index < len(blocks) and blocks[index].get("images")]
            if adjacent:
                associated[key] = adjacent[0]
    return associated


def _detached_figures(blocks, captions, associations):
    page_captions = defaultdict(list)
    page_figures = defaultdict(list)
    for key, index in captions.items():
        if blocks[index].get("page") and blocks[index].get("bbox"):
            page_captions[blocks[index]["page"]].append((key, index))
    for index, block in enumerate(blocks):
        if block.get("page") and block.get("images") and block.get("bbox"):
            page_figures[block["page"]].append(index)
    for page, entries in page_captions.items():
        figures = page_figures[page]
        if len(entries) == len(figures) == 1:
            key, caption = entries[0]
            if key not in associations:
                cb = blocks[caption]["bbox"]
                ib = blocks[figures[0]]["bbox"]
                if cb[0] < ib[2] and ib[0] < cb[2] and abs(cb[1] - ib[3]) > 60:
                    yield key, caption, figures[0], abs(cb[1] - ib[3])


def _raster_identity(block):
    return tuple((image.get("digest", ""), image["width"], image["height"], image["xPpi"], image["yPpi"])
                 for image in block.get("images", []))


def _chart_labels(blocks, figure_index):
    block = blocks[figure_index]
    if not block.get("bbox") or not block.get("page"):
        return []
    x0, y0, x1, y1 = block["bbox"]
    labels = []
    for index, group in enumerate(blocks):
        if group["kind"] != "text" or group.get("page") != block["page"] or not group.get("bbox"):
            continue
        gx0, gy0, gx1, gy1 = group["bbox"]
        if x0 <= gx0 <= gx1 <= x1 and y0 <= gy0 <= gy1 <= y1:
            text = group["text"].strip()
            if re.fullmatch(r"[-−+]?\d+(?:[.,]\d+)?\s*[%％]?", text):
                labels.append({"value": text, "anchor": index, "bbox": group["bbox"],
                               "source": "OCR" if group.get("ocr") else "PDF 文字层"})
    return labels


def _version_figures(blocks, captions, associations, prior, affected):
    prior_captions, _ = figure_contexts(prior)
    prior_images = _image_associations(prior, prior_captions)
    for key, caption in captions.items():
        if key not in prior_captions:
            continue
        old_caption = prior_captions[key]
        changed = blocks[caption]["text"] != prior[old_caption]["text"]
        old_figure, new_figure = prior_images.get(key), associations.get(key)
        if (old_figure is not None and new_figure is not None
                and blocks[new_figure].get("part") == prior[old_figure].get("part")):
            digest_changed = _raster_identity(blocks[new_figure]) != _raster_identity(prior[old_figure])
            changed = changed or digest_changed
        if changed or (affected is not None and caption in affected and old_figure != new_figure):
            yield key, caption, old_caption


def inspect_proof(blocks, *, name="", raw=None, affected=None, min_photo_ppi=300,
                  min_line_ppi=600, max_figure_pages=1, prior_blocks=None):
    is_pdf = Path(name).suffix.lower() == ".pdf" or any(block.get("part") == "pdf" for block in blocks)
    is_data = Path(name).suffix.lower() in {".csv", ".xlsx"}
    if raw and Path(name).suffix.lower() == ".docx":
        _word_positions(raw, blocks)
    issues = [_issue("图文核验", "原始数据表复算有差异",
                     f"{calc['cell']} 按「{calc['formula']}」复算为 {calc['expected']}，表中记录为 {calc['observed']}。",
                     index, [_evidence(blocks, index, " | ".join(block.get("cells") or [block["text"]]))], "high")
              for index, block in enumerate(blocks) if affected is None or index in affected
              for calc in block.get("calculations", []) if calc["status"] == "conflict"][:250]
    if is_data:
        return issues
    captions, mentions = figure_contexts(blocks)
    appendices = appendix_captions(blocks,captions) if is_pdf else {}
    associations = _image_associations(blocks, captions)
    if prior_blocks is None:
        prior_blocks = []
    changes = list(_version_figures(blocks, captions, associations, prior_blocks, affected)) if prior_blocks else []
    active = set(range(len(blocks))) if affected is None or is_pdf else set(affected)
    for key, caption, _ in changes:
        active.update(index for index in (caption, associations.get(key), *mentions[key]) if index is not None)
    if prior_blocks:
        old_images = {_raster_identity(block) for block in prior_blocks if block.get("images")}
        active.update(index for index, block in enumerate(blocks) if block.get("images")
                      and _raster_identity(block) not in old_images)
    for index in sorted(active):
        block = blocks[index]
        for image in block.get("images", []):
            threshold = min_line_ppi if image.get("lineArt") else min_photo_ppi
            x_ppi, y_ppi = image["xPpi"], image["yPpi"]
            if min(x_ppi, y_ppi) < threshold:
                detail = (f"{image['width']} × {image['height']} 像素，置入尺寸为 "
                          f"{image['printWidth']:g} × {image['printHeight']:g} pt，排版后有效分辨率为 "
                          f"{x_ppi:.1f} × {y_ppi:.1f} PPI，低于"
                          f"{'线稿' if image.get('lineArt') else '图像'}的 {threshold:g} PPI 检查标准。")
                issues.append(_issue("校样复核", "图件有效分辨率偏低", detail, index,
                                     [_evidence(blocks, index, image["source"] + " · " + detail)]))
    scanned = [index for index, block in enumerate(blocks) if block.get("scanNeeded")]
    if scanned:
        locations = "、".join(str(blocks[index]["location"]) for index in scanned[:12])
        issues.append(_issue("校样复核", "PDF 有扫描页或无法读取的内容",
                             f"请按原件人工复核文字与图件。涉及 {len(scanned)} 处（{locations}）。",
                             scanned[0], [_evidence(blocks, index) for index in scanned[:12]]))
    for key, caption in captions.items():
        figure = associations.get(key)
        mention = mentions[key][0] if mentions[key] else None
        if not any(index in active for index in (caption, figure, mention)):
            continue
        block = blocks[caption]
        source = next((index for index in range(caption + 1, min(caption + 3, len(blocks)))
                       if CAPTION_SOURCE.match(blocks[index]["text"]) and blocks[index].get("page") == block.get("page")), None)
        body = " ".join([block["text"], *(image.get("alt", "") for image in blocks[figure].get("images", []))]) if figure is not None else block["text"]
        citation = blocks[source]["text"] if source is not None else ""
        if MAP_WORDS.search(body):
            issues.append(_issue("地图审核", f"图 {key} 涉及地图内容",
                                 "请核实地图来源、审图号和适用范围，按本社流程提交人工审核。", caption,
                                 [_evidence(blocks, index) for index in (caption, source) if index is not None],
                                 "high", stage=2))
        if (THIRD_PARTY.search(body) or citation) and not LOCAL_SOURCE.search(body + " " + citation):
            issues.append(_issue("图片授权", f"图 {key} 可能使用第三方图件",
                                 "请登记著作权人、许可范围及期限，保存原图和授权材料。", caption,
                                 [_evidence(blocks, index) for index in (caption, source) if index is not None]))
        if figure is not None and is_pdf:
            labels = _chart_labels(blocks, figure)
            if labels:
                block["numericLabels"] = labels
        if mention is None or not block.get("page") or not blocks[mention].get("page"):
            continue
        figure_index = figure if figure is not None else caption
        figure_page = blocks[figure_index]["page"]
        mention_page = blocks[mention]["page"]
        distance = abs(figure_page - mention_page)
        if key in appendices and any(
                match[1].casefold() == appendices[key] and figure_number_key(match[2]) == key
                for match in APPENDIX_FIGURE.finditer(unicodedata.normalize("NFKC",blocks[mention]["text"]))):
            continue
        if distance > max_figure_pages:
            target = "图件" if figure is not None else "图题"
            issues.append(_issue("校样复核", f"图 {key} 首次指代与{target}页距较大",
                                 f"正文在第 {mention_page} 页，{target}在第 {figure_page} 页，相距 {distance} 页。"
                                 f"请复核当前 {max_figure_pages:g} 页的版式阈值。", mention,
                                 [_evidence(blocks, index) for index in dict.fromkeys((mention, caption, figure_index))]))
    if is_pdf:
        for key, caption, image, gap in _detached_figures(blocks, captions, associations):
            issues.append(_issue("校样复核", f"图 {key} 图题与本页图件相隔较远",
                                 f"单图单图题页面中，两者纵向间距约为 {gap:.1f} pt。请核对图题归属和留白。",
                                 caption, [_evidence(blocks, caption), _evidence(blocks, image)]))
    for key, caption, old in changes:
        evidence = [{"label": "上一版本图题", "text": prior_blocks[old]["text"]},
                    _evidence(blocks, caption)]
        evidence.extend(_evidence(blocks, index) for index in mentions[key][:20])
        issues.append(_issue("校样复核", f"图 {key} 图题或图件版本有变化",
                             "请对照上一版本核查图号、图注和正文全部关联指代。", caption, evidence))
    return issues[:250]
