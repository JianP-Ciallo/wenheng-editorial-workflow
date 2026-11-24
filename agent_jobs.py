"""Persist and execute version-bound editorial review jobs."""
from copy import deepcopy
from datetime import date
from email.message import EmailMessage
from hashlib import sha256
import json
import sqlite3
from time import time

from copyright_review import COPYRIGHT_CATEGORIES, copyright_status
from proofs import parse_spreadsheet
from research import can_cite
from workflow import WorkflowError, annotate_docx, decide_issues, log, new_id, now
from workspace import expired_version


STEPS = (
    ("structure", "解析稿件"), ("editorial", "文本与体例"), ("citations", "正文引文与 DOI"),
    ("proofs", "图件与数据"), ("copyright", "版权与地图"),
    ("academic", "同类书与学术评估"), ("documents", "整理白皮书与案卷"),
)
RUNNING = {"queued", "running"}
FINAL = {"approved", "returned", "rejected"}
LEASE_SECONDS = 180


def review_version(item):
    return next((version for version in reversed(item["versions"]) if version["type"] != "补件"), None)


def latest_run(data, item):
    return next((run for run in reversed(data.get("agent_runs", [])) if run["manuscript"] == item["id"]), None)


def _inputs(data, item):
    keys = ("title", "author", "category", "description", "region", "audience",
            "proofSettings", "bibliography", "assessment", "comparisons")
    today = date.today().isoformat()
    library = [({k:value for k,value in entry.items() if k not in {"linkChecked","linkStatus"}},
                not entry.get("validFrom") or entry["validFrom"] <= today,
                not entry.get("validUntil") or today < entry["validUntil"])
               for entry in data["library"] if can_cite(entry, item["id"])]
    issues = [issue for issue in data["issues"] if issue["manuscript"] == item["id"]]
    rights = [copyright_status(issue,item,today) for issue in issues if issue["category"] in COPYRIGHT_CATEGORIES]
    payload = ([item.get(key) for key in keys],
               [(ver["id"], expired_version(ver,today), ver.get("file"), ver.get("supersededBy")) for ver in item["versions"]],
               issues, rights, library)
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def expired(item, run):
    refs = set(run.get("sourceVersions", [run["version"]]))
    versions = {version["id"]: version for version in item["versions"]}
    return any(ident not in versions or expired_version(versions[ident]) for ident in refs)


def stale(data, item, run):
    latest = latest_run(data, item)
    return (expired(item, run) or latest is None or latest["id"] != run["id"]
            or run["inputHash"] != _inputs(data, item))


def _record(run, message):
    run["trace"].append({"time": now(), "message": message})


def _fail(run, message):
    run.update(status="failed", error=message)
    run.pop("lease", None)
    run.pop("heartbeat", None)
    _record(run, message)


def enqueue(data, item, actor):
    version = review_version(item)
    if not version or expired_version(version):
        raise WorkflowError("请先上传可审校的原稿、返修稿或校样。")
    if item.get("trashedAt") or item["status"] in {"archived", "rejected"}:
        raise WorkflowError("当前稿件无法启动智能审校。")
    fingerprint = _inputs(data, item)
    previous = latest_run(data, item)
    if previous and previous["status"] in RUNNING:
        if previous["inputHash"] == fingerprint:
            return previous
        _fail(previous, "稿件或核验资料已更新，已安排新的审校任务。")
    run = {
        "id": new_id(), "manuscript": item["id"], "version": version["id"], "actor": actor,
        "sourceVersions": [ver["id"] for ver in item["versions"] if not expired_version(ver)],
        "inputHash": fingerprint, "created": now(), "status": "queued", "progress": 0,
        "steps": [{"id": key, "label": label, "status": "pending"} for key, label in STEPS], "trace": [],
    }
    _record(run, "稿件已进入智能审校队列。")
    data.setdefault("agent_runs", []).append(run)
    return run


def public_run(run, *, outdated=False, summary=False):
    keys = ("id", "manuscript", "version", "status", "progress", "created", "error")
    if not summary:
        keys += ("steps", "trace")
    result = {key: run[key] for key in keys if key in run}
    result["stale"] = outdated
    if not summary:
        result["trace"] = [] if outdated else run["trace"][-80:]
    if run.get("brief"):
        result["verdict"] = run["brief"]["status"]
    return result


def has_publish_order(item, run):
    return (run.get("brief",{}).get("status") == "approved" and item["status"] == "archived"
            and not expired(item,run))


def claim(store):
    with store.lock:
        store.sync()
        cutoff = time() - LEASE_SECONDS
        if not any(run["status"] in RUNNING and run.get("heartbeat",0) <= cutoff
                   for run in store.data["agent_runs"]):
            return None
        data = deepcopy(store.data)
        items = {item["id"]: item for item in data["manuscripts"]}
        for run in data["agent_runs"]:
            if run["status"] not in RUNNING or run.get("heartbeat", 0) > cutoff:
                continue
            item = items.get(run["manuscript"])
            if not item or item.get("trashedAt"):
                continue
            if item["status"] in {"archived", "rejected"} or stale(data, item, run):
                _fail(run, "稿件状态、版本或核验资料已变更。请使用当前资料重新审校。")
                store.save(data)
                return None
            version = review_version(item)
            lease = new_id()
            run.update(status="running", progress=0, lease=lease, heartbeat=time())
            for step in run["steps"]:
                step["status"] = "pending"
            _record(run, "启动审校。" if len(run["trace"]) == 1 else "继续执行中断的审校任务。")
            store.save(data)
            # Keep the analysis snapshot separate from subsequent progress writes.
            return data, item, version, run["id"], lease
    return None


class ReviewSuperseded(Exception):
    pass


def _write_run(store, ident, lease, update):
    with store.lock:
        store.sync()
        data = deepcopy(store.data)
        run = next((row for row in data["agent_runs"] if row["id"] == ident), None)
        if not run or run.get("lease") != lease:
            raise ReviewSuperseded
        item = next((row for row in data["manuscripts"] if row["id"] == run["manuscript"]), None)
        if not item or item.get("trashedAt") or item["status"] in {"archived", "rejected"} or stale(data, item, run):
            _fail(run, "审校期间稿件或核验资料已变更。请重新审校。")
            store.save(data)
            raise ReviewSuperseded
        update(run)
        store.save(data)


def _with_current_supplements(item, directory):
    versions = []
    for version in item["versions"]:
        if (version["type"] == "补件" and not version.get("supersededBy") and not expired_version(version)
                and version.get("file","").endswith(".xlsx")
                and not all(block.get("formulaCoverage") for block in version["blocks"])):
            raw = (directory / version["file"]).read_bytes()
            version = {**version, "blocks":parse_spreadsheet(version["name"],raw)}
        versions.append(version)
    return {**item, "versions":versions}


def review_one(store, check, review=None):
    claimed = claim(store)
    if not claimed:
        return False
    data, item, version, ident, lease = claimed
    def progress(key, message):
        def update(run):
            for index, step in enumerate(run["steps"]):
                if step["id"] == key:
                    step["status"] = "running"
                    run["progress"] = max(run["progress"], round(100 * index / len(STEPS)))
                elif step["status"] == "running":
                    step["status"] = "complete"
            run["heartbeat"] = time()
            _record(run, message)
        _write_run(store, ident, lease, update)

    def complete(run):
        brief.update(version=version["id"], manuscript=item["id"], title=item["title"],
                     author=item["author"], timestamp=now(), agentVersion="1.0",
                     hasDocx=bool(version.get("file") and version["name"].lower().endswith(".docx")))
        run.update(status="complete", progress=100, brief=brief)
        run.pop("lease", None)
        run.pop("heartbeat", None)
        for step in run["steps"]:
            step["status"] = "complete"
        _record(run, "审校完成，已生成白皮书与人工裁决案卷。")

    try:
        if review is None:
            from editor_agent import run_review
            review = run_review
        raw = (store.directory / version["file"]).read_bytes() if version.get("file") else None
        brief = review(data, _with_current_supplements(item,store.directory), version, raw, check, progress)
        _write_run(store, ident, lease, complete)
    except ReviewSuperseded:
        pass
    except sqlite3.Error:
        raise
    except Exception as exc:
        print(f"Agent review failed ({ident}): {type(exc).__name__}", flush=True)
        try:
            _write_run(store, ident, lease, lambda run: _fail(run, "审校执行失败，请检查稿件后重试。"))
        except ReviewSuperseded:
            pass
    return True


def worker(store, stop, check):
    while not stop.wait(.75):
        try:
            review_one(store, check)
        except (sqlite3.Error, WorkflowError):
            continue


def _decision_brief(data, item, run):
    if stale(data, item, run):
        raise WorkflowError("此审校结果已过期，请先重新审校。")
    if run["status"] != "complete" or run["brief"]["status"] in FINAL or item["status"] in {"archived", "rejected"}:
        raise WorkflowError("当前审校任务无法继续裁决或批准。")
    return run["brief"]


def rule(data, item, run, ident, option, actor, note):
    brief = _decision_brief(data, item, run)
    case = next((row for row in brief["cases"] if row["id"] == ident), None)
    if not note.strip() or not case or case.get("ruling") or option not in {"A", "B", "C"} \
            or not any(choice["id"] == option for choice in case["options"]):
        raise WorkflowError("请为尚未裁决的案卷选择可用方案并填写依据。")
    ruling = {"option": option, "actor": actor, "note": note, "time": now()}
    issue = next((row for row in data["issues"] if row["id"] == case.get("issueId")
                  and row["manuscript"] == item["id"]), None)
    if option == "B" and issue and issue.get("blocking"):
        raise WorkflowError("阻断事项须核实并处理后重新审校。")
    if option == "A":
        if issue:
            decide_issues(data, [issue["id"]], "waiting", note, actor=actor)
        item["status"] = "revision"
        brief["status"] = "returned"
        label = "智能审校裁决 · 退回作者补正"
    elif option == "C":
        item["status"] = "rejected"
        brief["status"] = "rejected"
        label = "智能审校裁决 · 退稿归档"
    else:
        if issue:
            decide_issues(data, [issue["id"]], "dismissed", note, actor=actor)
        remaining = [row for row in brief["cases"] if row is not case and not row.get("ruling")]
        if not remaining:
            brief["status"] = "ready_for_signoff"
        label = "智能审校裁决 · 接受例外"
    case["ruling"] = ruling
    if option == "B":
        for check in brief["checks"]:
            if check["id"] == case.get("stage") and all(
                    row.get("ruling",{}).get("option") == "B"
                    for row in brief["cases"] if row.get("stage") == case.get("stage")):
                check["status"] = "accepted"
    log(item, label, f"{case['title']} · {note}")
    item["events"][0]["owner"] = actor
    run["inputHash"] = _inputs(data, item)


def approve(data, item, run, actor, note):
    brief = _decision_brief(data, item, run)
    if not note.strip() or brief["status"] != "ready_for_signoff" or item["status"] != "active" \
            or any(not case.get("ruling") for case in brief["cases"]):
        raise WorkflowError("仍有待裁决事项或稿件处于暂停／待补件状态，无法批准印制。")
    if any(task["manuscript"] == item["id"] and task["status"] in RUNNING for task in data.get("tasks", [])):
        raise WorkflowError("请等待稿件检查任务完成后再批准印制。")
    ids = set(brief.get("fixedIssueIds", []))
    fixed = [issue["id"] for issue in data["issues"] if issue["id"] in ids and issue["manuscript"] == item["id"]
             and issue["version"] == run["version"] and issue["state"] == "pending" and not issue.get("blocking")]
    for ident in fixed:
        decide_issues(data,[ident],"resolved","已在智能体 Word 修订中完成，随本次终审确认。",actor=actor)
    brief.update(status="approved", approval={"actor": actor, "note": note, "time": now()})
    item.update(status="archived", stage=4)
    log(item, "智能审校终审 · 批准进入印制", note)
    item["events"][0]["owner"] = actor
    run["inputHash"] = _inputs(data,item)


def artifact(store, item, run, kind):
    from agent_documents import revise_docx, text_docx

    brief = run["brief"]
    stem = item["title"][:75]
    if kind in {"tracked", "clean"} and brief["hasDocx"]:
        version = next(version for version in item["versions"] if version["id"] == run["version"])
        raw = (store.directory / version["file"]).read_bytes()
        contents = revise_docx(raw, brief["fixes"], tracked=kind == "tracked")
        if kind == "tracked":
            comments = []
            for case in brief["cases"]:
                evidence = " · ".join(row["url"] for row in case.get("evidence", []) if row.get("url"))
                comments.append({**case, "state": "dismissed" if case.get("ruling", {}).get("option") == "B" else "pending",
                                 "note": evidence[:500]})
            if any(case.get("anchor") is not None for case in comments):
                contents = annotate_docx(contents, comments)
        return contents, stem + ("-修订追踪稿.docx" if kind == "tracked" else "-修订清稿.docx")
    status_labels = {"pass": "通过", "review": "需复核", "not_applicable": "不适用", "accepted": "已裁决例外"}
    if kind == "brief":
        lines = [f"# 《{item['title']}》签发白皮书", "", f"稿件编号 {item['id']} · 版本 {run['version']}",
                 f"作者 {item['author']} · 生成时间 {brief['timestamp']}",
                 f"{brief['paragraphs']} 段，{brief['characters']} 字符，"
                 f"自动修订 {sum(fix.get('count',1) for fix in brief['fixes'])} 处", "",
                 "## 核验记录", ""]
        lines.extend(f"- {row['label']} · {status_labels[row['status']]}。{row['detail']}" for row in brief["checks"])
        lines.extend(("", "## 案卷与裁决", ""))
        for case in brief["cases"]:
            ruling = case.get("ruling")
            lines.append(f"- {case['title']} · {case.get('location') or '稿件整体'}。"
                         + (f"{ruling['actor']}选择 {ruling['option']}。{ruling['note']}" if ruling else "待裁决。"))
        if brief.get("approval"):
            lines.extend(("", f"批准人 {brief['approval']['actor']} · {brief['approval']['time']}",
                          brief["approval"]["note"]))
        return "\n".join(lines).encode(), stem + "-签发白皮书.md"
    if kind == "citations":
        lines = [f"# 《{item['title']}》引文核验记录", "", f"稿件编号 {item['id']} · {brief['timestamp']}", ""]
        lines.extend(row["detail"] for row in brief["checks"] if row["id"] == "citations")
        for row in brief["citations"]:
            lines.append(f"- {row.get('doi') or row.get('number') or '文末条目'} · "
                         f"{row.get('detail') or row.get('status','待核')}")
        return "\n".join(lines).encode(), stem + "-引文核验记录.md"
    reports = {"supplement": ("returned", "作者补正函（草稿）", "A"),
               "mail": ("returned", "作者补正邮件（草稿）", "A"),
               "termination": ("rejected", "退稿分析报告", "C"),
               "publish": ("approved", "印制批单", None)}
    if kind not in reports or reports[kind][0] != brief["status"]:
        raise WorkflowError("当前审校结果没有该成果物。")
    _, label, option = reports[kind]
    paragraphs = [f"稿件《{item['title']}》 · {item['id']}", f"稿件版本 {run['version']}"]
    if option:
        case = next(case for case in brief["cases"] if case.get("ruling", {}).get("option") == option)
        ruling = case["ruling"]
        paragraphs += [f"裁决人 {ruling['actor']} · {ruling['time']}", case["title"], case["detail"], ruling["note"]]
        if option == "A":
            paragraphs.append(case.get("authorRequest") or "请核对上述问题，提交修订稿及所需依据。")
    else:
        approval = brief["approval"]
        paragraphs += [f"批准人 {approval['actor']} · {approval['time']}", approval["note"], "批准进入印制。"]
    if kind == "mail":
        message = EmailMessage()
        message["Subject"] = "稿件补正 · " + " ".join(item["title"].split())
        message.set_content("\n\n".join(paragraphs))
        return message.as_bytes(), f"{stem}-{label}.eml"
    return text_docx(label, paragraphs), f"{stem}-{label}.docx"
