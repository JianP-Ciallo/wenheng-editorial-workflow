"""Editorial decisions for third-party material and the version being signed off."""

from datetime import date
import re

from workflow import WorkflowError, issue_data, now
from workspace import expired_version

COPYRIGHT_CATEGORIES = {"版权核验", "图片授权", "地图审核"}
MEDIA = ("纸书", "电子书", "有声书", "网站", "社交媒体")
PURPOSES = ("内页", "封面", "宣传")
DECISIONS = {
    "pending": "待补证", "referral": "转专业复核", "licensed": "按许可使用",
    "quotation": "按适当引用保留", "original": "核实为自有或可用素材",
    "replaced": "已核实替换材料", "removed": "已删除材料",
}
SOURCE_CUES = (
    (re.compile(r"(?:图片|图件|插图|照片|资料)?来源\s*[：:]?\s*(?:于|自)?\s*(?:网络|互联网)"), "网络来源"),
    (re.compile(r"译自|翻译自|AI\s*翻译(?:自|自于)"), "译文"),
    (re.compile(r"转载自|授权转载|据.{1,32}(?:改绘|重绘)|作者重绘|节选自"), "转载、节选或改绘"),
)


def copyright_findings(blocks, affected=None, existing=()):
    image_anchors = {issue["anchor"] for issue in existing if issue["category"] == "图片授权"}
    findings = []
    bibliography = False
    for index, block in enumerate(blocks):
        text = block["text"]
        if re.fullmatch(r"\s*(参考文献|引用文献|Bibliography|References)\s*", text, re.I):
            bibliography = True
        if bibliography and block.get("kind") == "heading" and re.match(r"第.+[章节]|附录", text):
            bibliography = False
        if bibliography or (affected is not None and index not in affected):
            continue
        if index in image_anchors or (index - 1 in image_anchors and re.match(r"\s*(?:图片|图件|插图|照片)?来源", text)):
            continue
        cue = next((label for expression, label in SOURCE_CUES if expression.search(text)), "")
        if cue:
            findings.append(issue_data("版权核验", f"核查{cue}材料",
                                       "请确认原作品、权利依据及本书拟用版本和位置。",
                                       index, "normal", [{"label": cue, "text": text[:800]}], stage=4))
    return findings


def latest_copyright_version(item):
    return next((version for version in reversed(item.get("versions", []))
                 if version["type"] in {"原稿", "返修稿", "校样"}), None)


def parse_use_pairs(value):
    pairs = []
    for line in value.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in re.split(r"[/／]", line)]
        if len(parts) != 2 or parts[0] not in MEDIA or parts[1] not in PURPOSES:
            raise WorkflowError("每行填写一组「媒介 / 用途」，例如「纸书 / 内页」。")
        pair = " / ".join(parts)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def save_copyright_review(issue, item, record, actor):
    decision = record["decision"]
    if decision not in DECISIONS:
        raise WorkflowError("请选择有效的版权处置结论。")
    versions = {version["id"]: version for version in item["versions"]}
    for key, kinds in (("version", {"原稿", "返修稿", "校样"}), ("evidenceVersion", {"补件"})):
        if record[key]:
            version = versions.get(record[key])
            if not version or version["type"] not in kinds or expired_version(version):
                raise WorkflowError("请选择此书稿中可用且类型正确的核验版本或依据附件。")
    record["planned"] = parse_use_pairs(record["planned"])
    record["allowed"] = parse_use_pairs(record["allowed"])
    if record["expiry"]:
        record["expiry"] = date.fromisoformat(record["expiry"]).isoformat()
    if decision == "referral" and not (record["referTo"] and record["basis"]):
        raise WorkflowError("请填写复核人或部门，并说明转审原因。")
    if decision not in {"pending", "referral"}:
        if not (record["version"] and record["basis"]):
            raise WorkflowError("请登记复核版本和处置依据。")
        if decision != "removed" and not (record["source"] and record["planned"]):
            raise WorkflowError("请登记原作品或替换素材出处，以及实际拟用的媒介和用途。")
    if decision == "licensed":
        if not (record["rightsOwner"] and record["allowed"]):
            raise WorkflowError("请登记权利人和许可覆盖的媒介及用途。")
        if record["term"] not in {"dated", "indefinite"} or (record["term"] == "dated") != bool(record["expiry"]):
            raise WorkflowError("请按依据登记授权期限。有截止日期时请选择「有截止日期」。")
    previous = issue.get("rights", {})
    history = list(previous.get("history", []))
    if previous:
        history.insert(0, {key: value for key, value in previous.items() if key != "history"})
    issue["rights"] = {**record, "reviewer": actor, "reviewedAt": now(), "history": history}
    issue.pop("rightsRetired", None)


def rights_references(record):
    return {record.get("version"), record.get("evidenceVersion")} - {None, ""}


def redact_copyright_record(record, blocked):
    if blocked & rights_references(record):
        return {}
    if "history" in record:
        return {**record, "history": [entry for entry in record["history"] if not blocked & rights_references(entry)]}
    return record


def copyright_status(issue, item, today=None):
    today = today or date.today().isoformat()
    record = issue.get("rights", {})
    versions = item.get("versions", [])
    blocked = {ver["id"] for ver in versions if expired_version(ver, today)}
    if issue.get("rightsRetired") or blocked & (rights_references(record) | {issue.get("version")}):
        return {"status": "unavailable"}
    decision = record.get("decision")
    if decision not in DECISIONS or decision in {"pending", "referral"}:
        return {"status": "referral" if decision == "referral" else "pending"}
    version = latest_copyright_version(item)
    if not version or record["version"] != version["id"]:
        return {"status": "stale"}
    if decision == "licensed":
        signoff = next((event["time"][:10] for event in item.get("events", [])
                        if event["label"] == "签核归档"), today) if item.get("status") == "archived" else today
        if record["term"] == "dated" and record["expiry"] < signoff:
            return {"status": "expired"}
        missing = [pair for pair in record["planned"] if pair not in record["allowed"]]
        if missing:
            return {"status": "gap", "missing": missing}
    return {"status": "ready"}
