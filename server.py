"""Run the private editorial workspace on a loopback address."""
import argparse
import base64
import binascii
from copy import deepcopy
from datetime import date, timedelta
from getpass import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
from io import StringIO
import json
import math
import mimetypes
from os import environ
from pathlib import Path
import re
import sqlite3
from threading import Event, Thread
from time import monotonic
from urllib.parse import parse_qs, quote, urlsplit
import webbrowser

from workflow import (STAGES, WorkflowError, annotate_docx, apply_transition, compare_paragraphs,
                      decide_issues, issue_data, log, make_issue, new_id,
                      now, open_issue, parse_document, screen)
from research import (can_cite, check_public_source, compare_book, crossref_metadata, evidence_excerpt, excerpts,
                      redact_issue, retrieve)
from workspace import (Forbidden, ROLES, Workspace, add_account, has_project, permitted,
                       expired_version, pilot_summary, public_account, purge_retained, visible_library)
from editorial_checks import affected_by_changes, check_blocks, citation_alignment
from proofs import ProofError, inspect_proof, parse_spreadsheet, render_pdf_page

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / ".editor-data"
LIMIT = 20 * 1024 * 1024

Store = Workspace

def field(data, name, required=False, length=1000):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise WorkflowError("字段格式无效。")
    value = value.strip()
    if (required and not value) or len(value) > length:
        raise WorkflowError("请填写必填字段，并检查输入长度。")
    return value

def lookup(items, ident):
    record = next((record for record in items if record["id"] == ident), None)
    if record is None:
        raise WorkflowError("记录不存在。")
    return record

def primary_version(item):
    return next((entry for entry in reversed(item["versions"])
                 if entry["type"] != "补件" and entry["blocks"] and not expired_version(entry)),None)

def validate_anchor(anchor, version):
    if anchor is not None and (type(anchor) is not int or not version or not 0 <= anchor < len(version["blocks"])):
        raise WorkflowError("原文段落定位无效。")

def replace_evidence(data, entry):
    prior = entry.get("supersedes","")
    if not prior or entry["type"] != "evidence":
        return
    matches = [old for old in data["library"] if old["id"] == prior or old.get("identifier") == prior]
    matches = [old for old in matches if old["id"] != entry["id"] and old["type"] == "evidence"]
    if len(matches) != 1:
        raise WorkflowError("替代对象需通过唯一资料编号指向一条旧版条款。")
    old = matches[0]
    if (not old.get("validFrom") or entry["validFrom"] <= old["validFrom"]
            or project_ids(old) != project_ids(entry)
            or any(old.get(key,"") != entry.get(key,"") for key in ("region","audience"))):
        raise WorkflowError("新版的生效日须晚于旧版，适用范围和项目授权须一致。")
    old["validUntil"] = min(filter(None,(old.get("validUntil"),entry["validFrom"])))
    old["replacedBy"] = entry["id"]

def snapshot(data, account=None):
    items = []
    visible = [item for item in data["manuscripts"] if account is None or has_project(account, item)]
    for item in visible:
        has_expired = any(expired_version(version) for version in item["versions"])
        versions = [{**{key: value for key, value in version.items() if key not in {"blocks","changes","file","purgeFile"}},
                     "paragraphs": 0 if expired_version(version) else len(version["blocks"]),
                     "diffCount": 0 if has_expired else len(version["changes"]), "expired":expired_version(version),
                     "download":bool(version["file"]) and not expired_version(version)} for version in item["versions"]]
        items.append({**item, "versions": versions})
    today = date.today().isoformat()
    records = [{key:value for key,value in entry.items() if key != "corpus"} for entry in data["library"]]
    library = [{**entry, **{key:"" for key in ("text","tableRows","footnotes","conditions","latex","symbols")}, "restricted": True}
               if entry.get("approvalUntil") and entry["approvalUntil"] <= today else entry
               for entry in records if account is None or visible_library(account, entry, visible)]
    allowed = {item["id"] for item in items}
    sources = {entry["id"]:entry for entry in data["library"]}
    blocked = {version["id"] for item in visible for version in item["versions"] if expired_version(version)}
    issues = [({**issue,"title":"已到期版本的审校记录","detail":"","note":"","evidence":[],"rights":{},"anchor":None}
               if issue["version"] in blocked else redact_issue(issue,sources))
              for issue in data["issues"] if issue["manuscript"] in allowed]
    trials = [trial for trial in data.get("trials", []) if trial["manuscript"] in allowed]
    return {"manuscripts": items, "issues": issues, "library": library, "stages": STAGES,
            "trials":trials, "pilot":pilot_summary(trials),
            "tasks":[task for task in data.get("tasks",[]) if task["manuscript"] in allowed]}

def create_manuscript(data, payload):
    year = date.today().year
    sequence = max((int(item["id"].rsplit("-", 1)[1]) for item in data["manuscripts"]
                    if item["id"].startswith(f"MS-{year}-")), default=0) + 1
    record = {"id": f"MS-{year}-{sequence:03}", "title": field(payload, "title", True, 160),
              "author": field(payload, "author", True, 80), "editor": field(payload, "editor", True, 80),
              "category": field(payload, "category", True, 60), "description": field(payload, "description"),
              "stage": 0, "status": "active", "created": now(), "versions": [], "events": [],
              "comparisons": [], "assessment": {}, "bibliography": [],
              "region": field(payload, "region", length=60), "audience": field(payload, "audience", length=100)}
    data["manuscripts"].insert(0, record)
    log(record, "登记新稿")
    return record

def assign_upload(data, item, payload, store):
    name = field(payload, "name", True, 180).replace("\\", "/").rsplit("/", 1)[-1]
    kind = field(payload, "kind", True, 20)
    if kind not in {"原稿", "返修稿", "校样", "补件"}:
        raise WorkflowError("请选择正确的稿件版本类型。")
    try:
        encoded = payload.get("content", "")
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise WorkflowError("附件内容无效。") from exc
    if not raw or len(raw) > LIMIT:
        raise WorkflowError("请上传 20 MB 以内的非空文件。")
    spreadsheet = Path(name).suffix.lower() in {".xlsx",".csv"}
    if spreadsheet and kind != "补件":
        raise WorkflowError("电子表格请作为原始数据补件上传。")
    try:
        blocks = parse_spreadsheet(name,raw) if spreadsheet else parse_document(name,raw)
    except ProofError as exc:
        raise WorkflowError(str(exc)) from exc
    previous = primary_version(item)
    changes, affected = compare_paragraphs(previous["blocks"], blocks) if previous and blocks and kind != "补件" else ([], None)
    if affected is not None and any(
            re.match(r"^\s*图\s*\d", text) for change in changes for text in change["before"] + change["after"]):
        for block in blocks:
            if re.search(r"图\s*\d", block["text"]):
                block["recheckFigure"] = True
    ident = new_id()
    filename = ident + Path(name).suffix.lower()
    versions = item["versions"]
    version = {"id": ident, "label": f"V{len(versions) + 1}", "name": name, "type": kind, "created": now(),
               "file": filename, "size": len(raw), "blocks": blocks, "changes": changes}
    recheck = affected_by_changes(blocks,changes,affected) if affected is not None else None
    findings = run_checks(data,item,version,raw,recheck,previous)
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / filename).write_bytes(raw)
    versions.append(version)
    data["issues"][0:0] = [make_issue(item, ident, finding) for finding in findings]
    log(item, f"上传 {version['label']} · {kind}", f"{name} · 新增 {len(findings)} 条待判断事项")
    return {"id": ident, "issues": len(findings)}, filename

def run_checks(data,item,version,raw=None,affected=None,previous=None):
    blocks, name = version["blocks"], version["name"]
    spreadsheet = Path(name).suffix.lower() in {".xlsx",".csv"}
    entries = [entry for entry in data["library"] if can_cite(entry,item["id"])]
    terms = [entry for entry in entries if entry["type"] == "term"]
    watchlist = [entry for entry in entries if entry["type"] == "watch"]
    settings = item.get("proofSettings",{})
    findings = []
    if blocks and version["type"] != "补件":
        findings = screen(blocks,affected,terms) + check_blocks(blocks,affected,item["author"],watchlist)
    if spreadsheet or version["type"] != "补件":
        findings.extend(inspect_proof(blocks,name=name,raw=raw,affected=affected,
                                      min_photo_ppi=settings.get("photoPpi",300), min_line_ppi=settings.get("linePpi",600),
                                      max_figure_pages=settings.get("maxFigurePages",1),
                                      prior_blocks=previous["blocks"] if previous else None))
    return sorted(findings,key=lambda finding:{"high":0,"normal":1,"low":2}[finding["severity"]])[:350]

def recheck_worker(store, stop):
    while not stop.wait(.75):
        try:
            with store.lock:
                store.sync()
                if not any(task["status"] in {"queued","running"} for task in store.data["tasks"]):
                    continue
                data = deepcopy(store.data)
                task = next(task for task in data["tasks"] if task["status"] in {"queued","running"})
                task["status"] = "running"
                store.save(data)
                data = deepcopy(data)
                task = lookup(data["tasks"],task["id"])
                item = lookup(data["manuscripts"],task["manuscript"])
                version = lookup(item["versions"],task["version"])
                try:
                    if item["status"] in {"archived","rejected"}:
                        raise WorkflowError("稿件已结束")
                    raw = (store.directory/version["file"]).read_bytes() if version["file"] else None
                    old = {(issue["title"],issue["detail"],issue["anchor"]) for issue in data["issues"]
                           if issue["version"] == version["id"]}
                    findings = [finding for finding in run_checks(data,item,version,raw)
                                if (finding["title"],finding["detail"],finding["anchor"]) not in old]
                    data["issues"][0:0] = [make_issue(item,version["id"],finding) for finding in findings]
                    task.update(status="complete", count=len(findings), updated=now())
                    log(item,"复跑检查",f"{version['label']} · 新增 {len(findings)} 项")
                except (OSError,ValueError):
                    task.update(status="failed", updated=now())
                store.save(data)
        except (sqlite3.Error,WorkflowError):
            continue

def library_patrol(store, stop):
    delay = 5
    while not stop.wait(delay):
        delay = 3600
        today = date.today()
        cutoff = (today-timedelta(days=30)).isoformat()
        try:
            with store.lock:
                store.sync()
                due = [(entry["id"],entry["sourceUrl"]) for entry in store.data["library"]
                       if entry["status"] == "approved" and entry.get("sourceUrl")
                       and (not entry.get("approvalUntil") or entry["approvalUntil"]>today.isoformat())
                       and entry.get("linkChecked","") <= cutoff][:10]
            for ident,url in due:
                if stop.is_set():
                    return
                started = monotonic()
                try:
                    result = check_public_source(url)
                except (WorkflowError,ValueError):
                    continue
                with store.lock:
                    store.sync()
                    data = deepcopy(store.data)
                    entry = next((entry for entry in data["library"] if entry["id"]==ident
                                  and entry.get("sourceUrl") == url),None)
                    if entry:
                        entry.update(linkStatus=result["status"],linkChecked=today.isoformat())
                        store.save(data)
                        store.audit(now(),"system","library-check",ident,round((monotonic()-started)*1000),
                                    result["http"] or 503)
        except (sqlite3.Error,WorkflowError):
            continue

def project_ids(item):
    return set(re.split(r"[\s,，]+", item.get("projects", "").strip()))

def create_library_entry(data, payload):
    kind = field(payload, "type", True, 30)
    if kind not in {"evidence", "book", "term", "watch"}:
        raise WorkflowError("请选择资料类型。")
    entry = {"id": new_id(), "type": kind}
    for key in ("title", "source", "projects", "identifier", "edition", "chapter", "page", "text", "keywords",
                "validFrom", "validUntil", "approvalUntil", "region", "audience", "reviewer", "checked",
                "supersedes", "coverage", "author", "variant", "evidenceType", "tableRows", "footnotes",
                "conditions", "latex", "symbols", "sourceUrl"):
        entry[key] = field(payload, key, key in ("title", "source", "projects"),
                           12000 if key in {"text","tableRows","footnotes","conditions","latex","symbols"} else 400)
    for key in ("validFrom", "validUntil", "approvalUntil", "checked"):
        if entry[key]:
            date.fromisoformat(entry[key])
    if entry["validUntil"] and entry["validFrom"] and entry["validUntil"] <= entry["validFrom"]:
        raise WorkflowError("失效首日必须晚于生效日。")
    if kind == "evidence" and any(not entry[key] for key in ("identifier", "edition", "chapter", "page", "text", "validFrom")):
        raise WorkflowError("条款需要标识、版本、原文定位以及生效日。")
    if kind == "evidence":
        entry["evidenceType"] = entry["evidenceType"] or "clause"
        if entry["evidenceType"] not in {"clause", "table", "formula"}:
            raise WorkflowError("请选择有效的证据对象类型。")
        if entry["evidenceType"] == "table" and not entry["tableRows"]:
            raise WorkflowError("请录入保留完整表头与单位的表行。")
        if entry["evidenceType"] == "formula" and not (entry["latex"] and entry["symbols"]):
            raise WorkflowError("请录入经过校对的 LaTeX 和符号释义。")
        entry["requiresFootnotes"] = payload.get("requiresFootnotes") in (True, "true", "yes")
    if kind == "term" and not entry["variant"]:
        raise WorkflowError("请填写需规范的变体。")
    if kind == "book" and payload.get("corpus"):
        if entry["coverage"] != "授权全文":
            raise WorkflowError("只有授权全文允许入库比对。")
        name = field(payload,"corpusName",True,180)
        if Path(name).suffix.casefold() not in {".docx",".txt"}:
            raise WorkflowError("全文比对支持 DOCX 和 UTF-8 TXT。")
        try:
            raw = base64.b64decode(payload["corpus"],validate=True)
        except (ValueError,TypeError) as exc:
            raise WorkflowError("无法读取授权全文文件。") from exc
        if len(raw) > 2*1024*1024:
            raise WorkflowError("请拆分为 2 MB 以内的授权全文。")
        entry["corpus"] = parse_document(name,raw)
    entry["status"] = "pending"
    data["library"].insert(0, entry)
    return entry

def import_library(data, payload):
    if payload.get("xlsx"):
        try:
            raw = base64.b64decode(payload["xlsx"],validate=True)
            rows = parse_spreadsheet("library.xlsx",raw)
        except (ProofError,TypeError,ValueError) as exc:
            raise WorkflowError("无法读取 Excel 资料表。请核对文件格式。") from exc
        worksheet = rows[0]["location"].rsplit(" · 第 ",1)[0]
        buffer = StringIO()
        writer = csv.writer(buffer)
        for row in rows:
            if row["location"].rsplit(" · 第 ",1)[0] == worksheet:
                writer.writerow(row["cells"])
        text = buffer.getvalue()
    else:
        text = field(payload, "csv", True, 2 * 1024 * 1024).lstrip("\ufeff")
    aliases = {"类型":"type", "名称":"title", "来源":"source", "项目":"projects", "标识":"identifier",
               "版次":"edition", "章条":"chapter", "页码":"page", "正文":"text", "关键词":"keywords",
               "生效日":"validFrom", "失效首日":"validUntil", "授权到期日":"approvalUntil",
               "变体":"variant", "覆盖范围":"coverage", "作者":"author"}
    type_names = {"术语表":"term", "专业条款":"evidence", "同类书目":"book", "预警期刊":"watch"}
    count = 0
    try:
        reader = csv.DictReader(StringIO(text), strict=True)
        for row in reader:
            if count >= 500 or None in row:
                raise WorkflowError("每批最多 500 条，请核对 CSV 的表头和列数。")
            values = {aliases.get(key.strip(), key.strip()):value or "" for key,value in row.items()}
            if not any(values.values()):
                continue
            fallback = field(payload, "type") or "term"
            values["type"] = type_names.get(values.get("type"), values.get("type") or fallback)
            values["projects"] = values.get("projects") or field(payload, "projects", True, 400)
            try:
                create_library_entry(data, values)
            except (WorkflowError, ValueError) as exc:
                raise WorkflowError(f"CSV 第 {reader.line_num} 行：{exc}") from exc
            count += 1
    except csv.Error as exc:
        raise WorkflowError("无法解析 CSV。请用 UTF-8 保存，并确认引号和分隔符。") from exc
    if not count:
        raise WorkflowError("CSV 中没有可导入的资料。")
    return count

def save_trial(data, item, payload, actor):
    record = {"id":new_id(), "manuscript":item["id"], "editor":actor,
              "mode":field(payload,"mode",True,30), "notes":field(payload,"notes",length=500)}
    if record["mode"] not in {"盲测", "交叉处理", "普通试点"}:
        raise WorkflowError("请选择验收方式。")
    record["date"] = date.fromisoformat(field(payload,"date",True,10)).isoformat()
    for key in ("words","truePositive","falsePositive","falseNegative","traditionalMinutes",
                "assistedMinutes","hourly","runtimeCost","fixedCost","volume"):
        try:
            value = float(payload.get(key,""))
            count = key in {"words","truePositive","falsePositive","falseNegative","volume"}
            if not math.isfinite(value) or value < 0 or value > 1e9 or count and not value.is_integer():
                raise ValueError
            if key in {"words","volume"} and value <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise WorkflowError("请检查字数、样本数、工时与费用。") from exc
        record[key] = int(value) if count else value
    data["trials"].insert(0,record)

def save_bibliography(data, item, payload):
    record = {key: field(payload, key, key in {"key", "chapter", "status"}, 500)
              for key in ("key", "chapter", "status", "sourceA", "sourceB", "note", "covered")}
    if record["status"] not in {"verified", "conflict", "unverified", "unavailable", "noCoverage"}:
        raise WorkflowError("请选择引文核验状态。")
    if record["status"] == "verified" and not record["sourceA"]:
        raise WorkflowError("核实身份至少需要一条可回查来源。")
    try:
        record["number"] = int(payload.get("number", ""))
        if record["number"] < 1:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise WorkflowError("请填写章内顺序编号。") from exc
    records = item["bibliography"]
    slot = next((index for index, old in enumerate(records)
                 if (old["chapter"], old["number"]) == (record["chapter"], record["number"])), None)
    if slot is None:
        records.append(record)
    else:
        records[slot] = record
    log(item, "登记引文回查", record["key"])
    chapter = sorted((ref for ref in records if ref["chapter"] == record["chapter"]), key=lambda ref: ref["number"])
    for offset in range(len(chapter) - 2):
        window = chapter[offset:offset + 3]
        countable = all(
            (ref["status"] == "conflict" or ref["status"] == "unverified" and ref["covered"] == "yes")
            and ref["sourceA"] and ref["sourceB"] and ref["sourceA"].casefold() != ref["sourceB"].casefold()
            for ref in window)
        if not countable or window[-1]["number"] - window[0]["number"] != 2 or len({ref["key"].casefold() for ref in window}) != 3:
            continue
        fingerprint = f"citations:{record['chapter']}:{window[0]['number']}:{'|'.join(ref['key'] for ref in window)}"
        if any(issue.get("fingerprint") == fingerprint and issue["manuscript"] == item["id"] for issue in data["issues"]):
            continue
        evidence = [{"label": ref["key"], "text": f"{ref['sourceA']} / {ref['sourceB']} · {ref['note']}"} for ref in window]
        finding = issue_data("引文核验", "学术诚信高危／待核", "同章连续三条不同引文达到暂停加工条件。请编辑部复核来源和字段差异。",
                             severity="high", evidence=evidence, blocking=True, stage=2)
        version = item["versions"][-1]["id"] if item["versions"] else ""
        data["issues"].insert(0, {**make_issue(item, version, finding), "fingerprint": fingerprint})
        if item["status"] in {"active", "revision"}:
            apply_transition(item, [], "pause", "同章引文触发人工复核阈值。")

class Handler(BaseHTTPRequestHandler):
    store = None

    def setup(self):
        super().setup()
        self.started = monotonic()
        self.actor = "anonymous"
        self.audit_context = None
        self.cookie_header = None

    def log_message(self, format, *args):
        return

    def reply(self, status, body, content_type="application/json; charset=utf-8", filename=None):
        if self.audit_context:
            op, ident = self.audit_context
            try:
                self.store.audit(now(), self.actor, op, ident, round((monotonic()-self.started)*1000), status)
            except sqlite3.Error:
                pass
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        if self.cookie_header:
            self.send_header("Set-Cookie", self.cookie_header)
        self.end_headers()
        self.wfile.write(body)

    def json(self, payload, status=200):
        self.reply(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def local_request(self):
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1)(?::\d+)?", host):
            self.json({"error": "请使用本机地址访问。"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}", f"https://{host}"}:
            self.json({"error": "请求来源无效。"}, 403)
            return False
        return True

    def require(self, account, ability):
        if not permitted(account, ability):
            raise Forbidden("当前角色没有执行该操作的权限。")

    def project(self, data, ident, account):
        item = next((entry for entry in data["manuscripts"] if entry["id"] == ident), None)
        if item is None or not has_project(account, item):
            raise Forbidden("稿件不存在或当前账号没有该项目权限。")
        return item

    def authorize(self):
        self.store.sync()
        account = self.store.identity(self.headers.get("Cookie"))
        if account:
            self.actor = account["id"]
        return account

    def failure(self, exc):
        if isinstance(exc, Forbidden):
            self.json({"error":str(exc),"code":"PERMISSION_DENIED"},403)
        elif isinstance(exc, (OSError, sqlite3.Error)):
            self.json({"error":"无法读写本机工作区，请检查文件和磁盘状态。","code":"STORAGE_ERROR"},500)
        elif not isinstance(exc, WorkflowError):
            self.json({"error":"输入格式不正确，请检查日期和字段值。","code":"INVALID_INPUT"},400)
        else:
            self.json({"error":str(exc),"code":"WORKFLOW_VALIDATION"},400)

    def do_GET(self):
        if not self.local_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in {"/", "/index.html", "/styles.css", "/app.js", "/icons.js", "/ui.js", "/tools.js"}:
            file = WEB / ("index.html" if path == "/" else path[1:])
            mime = mimetypes.guess_type(file)[0] or "application/octet-stream"
            self.reply(200, file.read_bytes(), mime + "; charset=utf-8")
            return
        try:
            with self.store.lock:
                account = self.authorize()
                if not account:
                    self.json({"error":"请登录文衡编辑台。","code":"LOGIN_REQUIRED"},401)
                    return
                data = self.store.data
                if path == "/api/state":
                    abilities = [name for name in ("create","flow","upload","decision","review","library",
                                                   "approve","trial","maintenance","audit","accounts")
                                 if permitted(account,name)]
                    self.json({**snapshot(data, account), "viewer":public_account(account),
                               "abilities":abilities, "roles":ROLES})
                    return
                if path == "/api/accounts":
                    self.require(account,"accounts")
                    self.json({"accounts":[public_account(person) for person in data["accounts"]]})
                    return
                if path == "/api/audit":
                    self.require(account,"audit")
                    self.json({"rows":self.store.audit_rows()})
                    return
                match = re.fullmatch(r"/api/manuscripts/([\w-]+)/versions/([\w-]+)/(text|download|comments|page)", path)
                if match:
                    self.audit_context = (match[3], match[2])
                    item = self.project(data, match[1], account)
                    version = lookup(item["versions"], match[2])
                    if expired_version(version):
                        self.json({"error":"该版本已到保留期限。","code":"VERSION_EXPIRED"},410)
                        return
                    if match[3] == "text":
                        changes = [] if any(expired_version(entry) for entry in item["versions"]) else version["changes"]
                        self.json({"blocks": version["blocks"], "changes":changes})
                        return
                    if match[3] == "page":
                        number = int(parse_qs(parsed.query).get("number",["0"])[0])
                        if not version["file"] or not version["name"].casefold().endswith(".pdf") or not any(
                                block.get("page") == number for block in version["blocks"]):
                            raise WorkflowError("未找到可预览的校样页。")
                        try:
                            raw = (self.store.directory/version["file"]).read_bytes()
                            png = render_pdf_page(raw,number)
                        except ProofError as exc:
                            raise WorkflowError(str(exc)) from exc
                        self.reply(200,png,"image/png")
                        return
                    if not version["file"]:
                        sample = "\n\n".join(block["text"] for block in version["blocks"])
                        self.reply(200, sample.encode("utf-8"), "text/plain; charset=utf-8", version["name"])
                        return
                    raw = (self.store.directory / version["file"]).read_bytes()
                    filename = version["name"]
                    if match[3] == "comments":
                        if not filename.lower().endswith(".docx"):
                            raise WorkflowError("只有 Word 文件支持写入批注。")
                        sources = {entry["id"]:entry for entry in data["library"]}
                        issues = [redact_issue(issue,sources) for issue in data["issues"] if issue["version"] == version["id"]]
                        raw = annotate_docx(raw, issues)
                        filename = Path(filename).stem + "（编辑批注）.docx"
                    self.reply(200, raw, "application/octet-stream", filename)
                    return
                if path == "/api/evidence":
                    args = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                    item = self.project(data, args.get("project"), account)
                    self.audit_context = ("evidence", item["id"])
                    version = (lookup(item["versions"], args["version"]) if args.get("version")
                               else primary_version(item))
                    if version and expired_version(version):
                        raise WorkflowError("当前稿件版本已到保留期限。")
                    anchor = int(args["anchor"]) if args.get("anchor") else None
                    validate_anchor(anchor, version)
                    block = version["blocks"][anchor] if version and anchor is not None else None
                    query = args.get("q","").strip() or (block["text"] if block else "")
                    target = args.get("date","")
                    found = retrieve(data["library"], query, target, item)
                    results = [{**entry, "citationTicket":self.store.receipts.issue(
                        entry,item["id"],version["id"] if version else "",anchor,target),
                        "excerpt":evidence_excerpt(entry)} for entry in found]
                    self.json({"results":results, "status":"引证待人工判断" if found else "待补证",
                               "anchor":anchor, "version":version["id"] if version else "", "block":block})
                    return
                if path == "/api/compare":
                    args = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                    item = self.project(data,args.get("project"),account)
                    entry = next((entry for entry in data["library"] if entry["id"] == args.get("book")
                                  and entry["type"] == "book" and can_cite(entry,item["id"])),None)
                    if not entry or entry.get("coverage") != "授权全文":
                        raise Forbidden("仅可比对本项目获准检索的已审定全文。")
                    version = primary_version(item)
                    self.audit_context = ("compare", item["id"])
                    self.json({"title":entry["title"], "version":version["id"] if version else "",
                               "matches":compare_book(version["blocks"],entry.get("corpus") or entry["text"]) if version else []})
                    return
                if path == "/api/excerpts":
                    args = {key: value[0] for key, value in parse_qs(parsed.query).items()}
                    item = self.project(data,args.get("project"),account)
                    self.audit_context = ("excerpts",item["id"])
                    self.json(excerpts(item,args.get("q","")[:350]))
                    return
                if path == "/api/citations":
                    args = {key:value[0] for key,value in parse_qs(parsed.query).items()}
                    item = self.project(data,args.get("project"),account)
                    version = primary_version(item)
                    self.audit_context = ("citations",item["id"])
                    self.json({**citation_alignment(version["blocks"] if version else []),
                               "version":version["id"] if version else ""})
                    return
            self.json({"error": "页面不存在。"}, 404)
        except (WorkflowError, ValueError, OSError, sqlite3.Error) as exc:
            self.failure(exc)

    def do_POST(self):
        if not self.local_request():
            return
        created_file = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > LIMIT * 4 // 3 + 65536 or self.headers.get_content_type() != "application/json":
                raise WorkflowError("请求格式无效或附件超过 20 MB。")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise WorkflowError("请求格式无效。")
            path = urlsplit(self.path).path
            with self.store.lock:
                account = self.authorize()
                if path == "/api/login":
                    self.audit_context = ("login","")
                    token = self.store.login(field(payload,"username",True,32),field(payload,"password",True,240))
                    self.cookie_header = f"wenheng={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800"
                    self.json({"ok":True})
                    return
                if not account:
                    self.json({"error":"请登录文衡编辑台。","code":"LOGIN_REQUIRED"},401)
                    return
                if path == "/api/logout":
                    self.store.logout(self.headers.get("Cookie"))
                    self.cookie_header = "wenheng=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    self.json({"ok":True})
                    return
                if path == "/api/crossref":
                    self.require(account,"review")
            if path == "/api/crossref":
                self.audit_context = ("crossref","")
                self.json(crossref_metadata(field(payload,"doi",True,300)))
                return
            with self.store.lock:
                account = self.authorize()
                if not account:
                    self.json({"error":"请登录文衡编辑台。","code":"LOGIN_REQUIRED"},401)
                    return
                data = deepcopy(self.store.data)
                match = re.fullmatch(r"/api/manuscripts/([\w-]+)/([a-z-]+)", path)
                library_item = re.fullmatch(r"/api/library/([a-f0-9-]+|sample-[a-z]+)(/check)?",path)
                known = {"/api/manuscripts":"create","/api/accounts":"members","/api/issues/decision":"decision",
                         "/api/library":"library-create","/api/library/import":"library-import"}
                if match:
                    self.audit_context = (match[2],match[1])
                elif library_item:
                    self.audit_context = ("library-check" if library_item[2] else "library-review",library_item[1])
                else:
                    self.audit_context = (known.get(path,"workspace-action"),"")
                result, created_file = self.mutate(data, path, payload, account)
                try:
                    self.store.save(data)
                except (OSError,sqlite3.Error,WorkflowError):
                    if created_file:
                        (self.store.directory / created_file).unlink(missing_ok=True)
                    raise
            self.json(result)
        except (WorkflowError, ValueError, UnicodeError, OSError, sqlite3.Error) as exc:
            self.failure(exc)

    def mutate(self, data, path, payload, account):
        if path == "/api/manuscripts":
            self.require(account,"create")
            if not account.get("local"):
                payload["editor"] = account["name"]
            item = create_manuscript(data,payload)
            item["createdBy"] = account["id"]
            return {"id":item["id"]}, None
        if path == "/api/issues/decision":
            self.require(account,"decision")
            ids = payload.get("ids")
            if not isinstance(ids, list) or not ids or len(ids)>100 or any(not isinstance(value, str) for value in ids):
                raise WorkflowError("问题选择无效。")
            items = [lookup(data["issues"],ident) for ident in ids]
            for item in items:
                self.project(data,item["manuscript"],account)
            decide_issues(data, ids, field(payload,"state"), field(payload,"note"),
                          global_batch=payload.get("global") is True, actor=account["name"])
            return {"ok": True}, None
        if path == "/api/accounts":
            self.require(account,"accounts")
            if not data["accounts"]:
                raise WorkflowError("请先在本机终端初始化管理员。")
            add_account(data, field(payload,"username",True,32),field(payload,"name",True,80),
                        field(payload,"password",True,240),field(payload,"role",True,30),
                        field(payload,"projects",length=400))
            return {"ok":True}, None
        account_match = re.fullmatch(r"/api/accounts/([\w.-]+)", path)
        if account_match:
            self.require(account,"accounts")
            member = lookup(data["accounts"],account_match[1])
            new_role = field(payload,"role",True,30)
            if new_role not in ROLES or (member["id"] == account["id"] and new_role != "admin"):
                raise WorkflowError("请选择有效角色；当前管理员可以由另一名管理员调整。")
            member.update(name=field(payload,"name",True,80),role=new_role,projects=field(payload,"projects",length=400))
            return {"ok":True}, None
        if path == "/api/library/import":
            self.require(account,"library")
            return {"count":import_library(data,payload)}, None
        if path == "/api/library":
            self.require(account,"library")
            return {"id": create_library_entry(data, payload)["id"]}, None
        check_match = re.fullmatch(r"/api/library/([\w-]+)/check",path)
        if check_match:
            self.require(account,"maintenance")
            entry = lookup(data["library"],check_match[1])
            if entry.get("approvalUntil") and entry["approvalUntil"] <= date.today().isoformat():
                raise WorkflowError("资料授权已到期。")
            result = check_public_source(entry.get("sourceUrl",""))
            entry.update(linkStatus=result["status"],linkChecked=date.today().isoformat())
            return result,None
        library_match = re.fullmatch(r"/api/library/([\w-]+)", path)
        if library_match:
            entry = lookup(data["library"], library_match[1])
            status = field(payload, "status", True)
            if status not in {"approved", "pending", "inactive"}:
                raise WorkflowError("无效的资料状态。")
            if status == "approved":
                self.require(account,"approve")
                visible = [item for item in data["manuscripts"] if has_project(account,item)]
                if not visible_library(account,entry,visible):
                    raise Forbidden("此资料不在当前账号的审定范围。")
                replace_evidence(data,entry)
            else:
                self.require(account,"library")
            reviewer = field(payload,"reviewer",True,80) if account.get("local") else account["name"]
            entry.update(status=status, reviewer=reviewer, checked=date.today().isoformat())
            return {"ok": True}, None
        match = re.fullmatch(r"/api/manuscripts/([\w-]+)/(upload|action|issue|comparison|assessment|bibliography|trial|proof-settings|recheck|rights|retention)", path)
        if not match:
            raise WorkflowError("接口不存在。")
        item = self.project(data, match[1], account)
        action = match[2]
        self.require(account,{"upload":"upload","action":"flow","trial":"trial","proof-settings":"upload",
                              "recheck":"upload","retention":"maintenance"}.get(action,"review"))
        if action == "retention":
            if item["status"] not in {"archived","rejected"}:
                raise WorkflowError("只有已结束稿件可登记版本保留期限。")
            version = lookup(item["versions"],field(payload,"version",True,120))
            if version.get("retired"):
                raise WorkflowError("该版本原件已清除。")
            until = field(payload,"until",length=10)
            version["retentionUntil"] = date.fromisoformat(until).isoformat() if until else ""
            log(item,"登记版本保留期限",version["id"])
            return {"ok":True},None
        if action == "trial":
            save_trial(data,item,payload,account["name"])
            return {"ok":True}, None
        if item["status"] in {"archived", "rejected"}:
            raise WorkflowError("已结束稿件只能查看。")
        issues = [issue for issue in data["issues"] if issue["manuscript"] == item["id"]]
        if action == "upload":
            return assign_upload(data, item, payload, self.store)
        if action == "recheck":
            version = lookup(item["versions"],field(payload,"version",True))
            if any(task["version"] == version["id"] and task["status"] in {"queued","running"} for task in data["tasks"]):
                raise WorkflowError("此版本已有待执行检查。")
            task = {"id":new_id(),"manuscript":item["id"],"version":version["id"],"status":"queued","created":now()}
            data["tasks"].append(task)
            return {"id":task["id"]}, None
        if action == "rights":
            issue = lookup(issues, field(payload,"id",True))
            if issue["category"] not in {"图片授权","地图审核"}:
                raise WorkflowError("只有图片授权和地图审核事项支持登记。")
            mapping = {"rightsOwner":"权利人/出处","media":"许可媒介","scope":"许可范围","expiry":"授权截止日",
                       "credit":"署名","mapNumber":"审图号","note":"说明"}
            record = {key:field(payload,key,length=400) for key in mapping}
            if record["expiry"]:
                record["expiry"] = date.fromisoformat(record["expiry"]).isoformat()
            if not record["rightsOwner"]:
                raise WorkflowError("请填写权利人或图件出处。")
            issue["rights"] = record
            log(item,"登记图件核验",issue["title"])
        if action == "action":
            name = field(payload, "action", True)
            if name == "advance":
                if not any(ver["type"] in {"原稿", "返修稿"} for ver in item["versions"]):
                    raise WorkflowError("请先上传原稿或返修稿。")
                if item["stage"] == 1 and not item["comparisons"]:
                    raise WorkflowError("请在同类书对照中保存至少一条对照意见。")
                if item["stage"] == 3 and item["assessment"].get("recommendation") != "建议送审":
                    raise WorkflowError("请在学术评估中完成送审意见。当前稿件需有可支持送审的评估。")
                if item["stage"] == 4 and any(task["manuscript"]==item["id"] and task["status"] in {"queued","running"}
                                                for task in data["tasks"]):
                    raise WorkflowError("请等待校样检查完成后再签核。")
            apply_transition(item, issues, name, field(payload, "note", True))
            item["events"][0]["owner"] = account["name"]
        if action == "proof-settings":
            try:
                settings = {key:int(payload.get(key,"")) for key in ("photoPpi","linePpi","maxFigurePages")}
                if not 72 <= settings["photoPpi"] <= 2400 or not 72 <= settings["linePpi"] <= 2400 or not 0 <= settings["maxFigurePages"] <= 20:
                    raise ValueError
            except (ValueError,TypeError) as exc:
                raise WorkflowError("请填写 72–2400 PPI 和 0–20 页之间的参数。") from exc
            item["proofSettings"] = settings
            log(item,"更新印制参数")
        if action == "issue":
            version_id = field(payload, "version")
            if version_id:
                version = lookup(item["versions"], version_id)
            else:
                version = item["versions"][-1] if item["versions"] else None
            anchor = payload.get("anchor")
            validate_anchor(anchor,version)
            severity = field(payload, "severity", True)
            if severity not in {"high", "normal", "low"}:
                raise WorkflowError("请选择问题的优先级。")
            evidence_text = field(payload, "evidence", length=12000)
            evidence_label = field(payload, "evidenceLabel", length=300)
            evidence = [{"label": evidence_label or "人工记录", "text": evidence_text}] if evidence_text else []
            ticket = field(payload,"citationTicket",length=2000)
            if ticket:
                entry = self.store.receipts.verify(ticket,data,item,version,anchor)
                evidence = [{"label":" · ".join([entry["title"],entry["edition"],entry["chapter"],f"第 {entry['page']} 页"]),
                             "text":evidence_excerpt(entry), "evidenceId":entry["id"]}]
            finding = issue_data(field(payload, "category", True), field(payload, "title", True, 160),
                                 field(payload, "detail", True, 1500), anchor, severity, evidence,
                                 blocking=payload.get("blocking") is True, stage=item["stage"])
            issue = make_issue(item, version["id"] if version else "", finding)
            data["issues"].insert(0, issue)
            log(item, "新增问题", issue["title"])
            return {"id": issue["id"]}, None
        if action == "comparison":
            comparison = {key: field(payload, key, True, 1500)
                          for key in ("title", "coverage", "overlap", "difference", "source")}
            item["comparisons"].insert(0, {**comparison, "id": new_id(), "time": now()})
            log(item, "新增同类书对照", comparison["title"])
        if action == "assessment":
            item["assessment"] = {key: field(payload, key, True, 2000)
                                  for key in ("question", "increment", "evidence", "recommendation")}
            log(item, "保存学术评估", item["assessment"]["recommendation"])
        if action == "bibliography":
            save_bibliography(data, item, payload)
        return {"ok": True}, None

def main():
    parser = argparse.ArgumentParser(description="运行文衡出版社审稿工作台")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--open", action="store_true", help="启动时打开本机浏览器")
    parser.add_argument("--init-admin", metavar="ACCOUNT", help="为已有工作区初始化独立登录账号")
    parser.add_argument("--purge-expired", action="store_true", help="列出已到期、待清除的归档版本")
    parser.add_argument("--apply", action="store_true", help="与 --purge-expired 同用时执行清除")
    args = parser.parse_args()
    Handler.store = Store(args.data)
    if args.purge_expired:
        identifiers = purge_retained(Handler.store,args.apply)
        for project,version in identifiers:
            print(project,version)
        print(f"{'已清除' if args.apply else '待清除'} {len(identifiers)} 个到期版本")
        return
    if args.init_admin:
        if Handler.store.data["accounts"]:
            parser.error("已启用登录，请由现有管理员添加账号。")
        password = getpass("管理员口令（至少 10 位）：")
        if password != getpass("再次输入口令："):
            parser.error("两次输入不一致。")
        data = deepcopy(Handler.store.data)
        add_account(data,args.init_admin,"工作区管理员",password,"admin","*")
        Handler.store.save(data)
        print("管理员已初始化。启动编辑台后登录。")
        return
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"文衡编辑台 {url}", flush=True)
        if args.open:
            webbrowser.open(url)
        stop = Event()
        worker = Thread(target=recheck_worker,args=(Handler.store,stop),daemon=True)
        worker.start()
        if environ.get("EDITOR_SOURCE_HOSTS","").strip():
            Thread(target=library_patrol,args=(Handler.store,stop),daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()

if __name__ == "__main__":
    main()
