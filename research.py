"""Local evidence retrieval, verified citations and bibliographic lookup."""
import base64
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import hmac
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
import math
import os
import re
import secrets
from time import monotonic, time
from urllib.parse import quote, urlsplit
import unicodedata
from threading import Lock

from workflow import WorkflowError

DOI = re.compile(r"10\.\d{4,9}/[-._;()/:+A-Z0-9]+", re.I)
ALIASES = (
    ("百分比", "百分率"), ("瑕疵", "缺陷"), ("欠佳品", "不合格品"), ("不良品", "不合格品"),
    ("置信区间", "可信区间"), ("增长率", "增幅"), ("公厘", "毫米"), ("信息来源", "数据来源"),
)

def projects(entry):
    return set(re.split(r"[,，\s]+", entry.get("projects", "")))

def grant_active(entry, project):
    return bool(projects(entry) & {project, "*"}) and (
        not entry.get("approvalUntil") or entry["approvalUntil"] > date.today().isoformat())

def can_cite(entry, project):
    return grant_active(entry, project) and entry["status"] == "approved"

def valid_evidence(entry, item, target):
    return (entry.get("type", "evidence") == "evidence" and can_cite(entry, item["id"])
            and (not entry.get("validFrom") or entry["validFrom"] <= target)
            and (not entry.get("validUntil") or entry["validUntil"] > target)
            and all(not entry.get(key) or entry[key].casefold() == item.get(key, "").casefold()
                    for key in ("region", "audience"))
            and all(entry.get(key) for key in ("text", "chapter", "page"))
            and (not entry.get("requiresFootnotes") or entry.get("footnotes"))
            and (entry.get("evidenceType") != "formula" or all(entry.get(key) for key in ("latex", "symbols"))))

def compact(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)

@lru_cache(maxsize=256)
def vector(text):
    for left, right in ALIASES:
        text = text.replace(left, right)
    features = Counter()
    for token in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            for width in (2, 3):
                for index in range(len(token) - width + 1):
                    features[token[index:index + width]] += 1
        else:
            features[token] += 1
    return features

def sparse_similarity(a, b):
    if not a or not b:
        return 0.0
    dot = sum(value * b.get(feature, 0) for feature, value in a.items())
    return dot / math.sqrt(sum(value * value for value in a.values()) * sum(value * value for value in b.values()))

def dense_embeddings(texts):
    model = os.environ.get("EDITOR_EMBED_MODEL", "").strip()
    if not model or model.endswith(":cloud") or "-cloud" in model or len(texts) > 24:
        return None
    connection = HTTPConnection("127.0.0.1", 11434, timeout=2.0)
    try:
        payload = json.dumps({"model": model, "input": [text[:3500] for text in texts]})
        connection.request("POST", "/api/embed", payload, {"Content-Type":"application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        vectors = json.loads(response.read(4 * 1024 * 1024))["embeddings"]
        if len(vectors) == len(texts) and len({len(vector) for vector in vectors}) == 1:
            return vectors
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        return None
    finally:
        connection.close()
    return None

def dense_similarity(a, b):
    scale = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / scale if scale else 0.0

def retrieve(library, query, target, item):
    try:
        target = date.fromisoformat(target).isoformat()
    except (TypeError, ValueError) as exc:
        raise WorkflowError("请填写有效的核验基准日。") from exc
    query = query.strip()[:350]
    if not query:
        raise WorkflowError("请输入待核主张或条款关键词。")
    tokens = {token.casefold() for token in re.split(r"[\s，。、；,;]+", query) if token}
    q_vector = vector(query)
    eligible = [entry for entry in library if valid_evidence(entry, item, target)]
    candidates = []
    for entry in eligible:
        title = " ".join(entry.get(key, "") for key in ("title", "keywords", "chapter", "identifier"))
        targets = [title, entry["text"], entry.get("footnotes", ""), entry.get("conditions", "")]
        targets.extend(entry.get("tableRows", "").splitlines())
        exact = sum(any(token in text.casefold() for text in targets) for token in tokens)
        lexical = max((sparse_similarity(q_vector, vector(text[:3200])) for text in targets), default=0)
        candidates.append((exact, lexical, entry))
    candidates.sort(key=lambda hit: (hit[0], hit[1]), reverse=True)
    dense = dense_embeddings([query] + [entry["title"] + " " + entry["text"] for _, _, entry in candidates[:16]]) if candidates else None
    ranked = []
    for index, (exact, lexical, entry) in enumerate(candidates):
        semantic = dense_similarity(dense[0], dense[index+1]) if dense and index < len(dense)-1 else 0
        if exact or lexical >= .22 or semantic >= .65:
            ranked.append((min(exact, 4) * .45 + lexical + max(0, semantic) * .7, entry))
    ranked.sort(key=lambda match: match[0], reverse=True)
    return [entry for _, entry in ranked[:8]]

class CitationReceipts:
    def __init__(self):
        self.secret = secrets.token_bytes(32)

    @staticmethod
    def digest(entry):
        return hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def issue(self, entry, project, version, anchor, target):
        payload = [entry["id"], project, version, anchor, target, int(time()) + 900, self.digest(entry)]
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        signature = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()
        return raw + "." + signature

    def verify(self, ticket, data, item, version, anchor):
        failure = "待补证：引证已过期、发生变更或与本次召回不一致，请重新检索。"
        try:
            raw, signature = ticket.split(".", 1)
            if not hmac.compare_digest(signature, hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()):
                raise ValueError
            ident, project, version_id, index, target, expiry, digest = json.loads(base64.urlsafe_b64decode(raw))
            if (project, version_id, index) != (item["id"], version["id"] if version else "", anchor) or expiry < time():
                raise ValueError
            entry = next(entry for entry in data["library"] if entry["id"] == ident)
            if not valid_evidence(entry, item, target) or not hmac.compare_digest(digest, self.digest(entry)):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, StopIteration) as exc:
            raise WorkflowError(failure) from exc
        return entry

def evidence_excerpt(entry):
    parts = [entry["text"]]
    for label, key in (("表格", "tableRows"), ("脚注", "footnotes"), ("适用条件", "conditions"),
                       ("LaTeX", "latex"), ("符号及单位", "symbols")):
        if entry.get(key):
            parts.append(label + "：" + entry[key])
    return "\n".join(parts)

def redact_issue(issue, library):
    full = []
    for evidence in issue.get("evidence", []):
        source_id = evidence.get("evidenceId")
        source = library.get(source_id)
        if source_id and (not source or not can_cite(source, issue["manuscript"])):
            full.append({"label":"受限证据", "text":"出处的授权已失效或暂停，原文停止展示。"})
        else:
            full.append(evidence)
    return {**issue, "evidence":full}

def compare_book(blocks, source):
    if isinstance(source,str):
        source = [{"text":line.strip(),"location":f"第 {index+1} 段"} for index,line in enumerate(source.splitlines())]
    source_lines = [line for line in source if len(compact(line["text"])) >= 22][:360]
    semantic_seeds = [(index,block["text"][:1200]) for index,block in enumerate(blocks[:8]) if len(compact(block["text"]))>=22]
    dense = dense_embeddings([text for _,text in semantic_seeds] + [line["text"][:1200] for line in source_lines[:12]]) if semantic_seeds and source_lines else None
    dense_rows = {index:offset for offset,(index,_) in enumerate(semantic_seeds)}
    matches = []
    for index, block in enumerate(blocks[:1000]):
        origin = block["text"][:1200]
        if len(compact(origin)) < 22 or block.get("kind") in {"heading", "formula", "image", "footnote"}:
            continue
        origin_vector = vector(origin)
        for source_index, line in enumerate(source_lines):
            excerpt = line["text"][:1500]
            score = sparse_similarity(origin_vector, vector(excerpt))
            meaning = (dense_similarity(dense[dense_rows[index]],dense[len(semantic_seeds)+source_index])
                       if dense and index in dense_rows and source_index < 12 else 0)
            if score < .23 and meaning < .75:
                continue
            original_text, matching_text = compact(origin), compact(excerpt)
            run = SequenceMatcher(None, original_text, matching_text, autojunk=False).find_longest_match()
            if run.size < 18 and score < .48 and meaning < .75:
                continue
            actual = original_text[run.a:run.a + run.size]
            sentences = [part for part in re.split(r"(?<=[。！？；])", origin) if len(compact(part)) >= 8][:8]
            targets = [part for part in re.split(r"(?<=[。！？；])", excerpt) if len(compact(part)) >= 8][:8]
            aligned = []
            for sentence in sentences:
                if targets:
                    best = max(targets, key=lambda other: sparse_similarity(vector(sentence), vector(other)))
                    if sparse_similarity(vector(sentence), vector(best)) >= .22:
                        aligned.append({"original":sentence, "matched":best})
            matches.append({"anchor":index, "location":block["location"], "target":source_index + 1,
                            "sourceLocation":line.get("location",""),
                            "kind":"连续原文" if run.size >= 18 else "语义线索", "score":round(max(score,meaning), 3),
                            "original":origin, "matched":excerpt, "alignment":aligned, "overlap":actual if run.size >= 18 else ""})
    return sorted(matches, key=lambda hit: (hit["kind"] == "连续原文", hit["score"]), reverse=True)[:12]

def excerpts(item, question):
    today = date.today().isoformat()
    version = next((entry for entry in reversed(item["versions"]) if entry.get("type") != "补件" and entry["blocks"]
                    and not entry.get("retired") and (not entry.get("retentionUntil") or entry["retentionUntil"]>today)), None)
    if not version:
        return {"version":"", "items":[]}
    query = vector(question)
    def relevance(block):
        line = block["text"]
        signal = bool(re.search(r"研究问题|方法|样本|贡献|数据|证据|相较|限于|案例", line))
        return sparse_similarity(query, vector(line[:2000])) + (.12 if signal else 0)
    ranks = [(relevance(block), index, block) for index, block in enumerate(version["blocks"])
             if block.get("kind") in {"text", "table"} and len(block["text"]) > 16]
    hits = [{"anchor":index, "location":block["location"], "text":block["text"][:750]}
            for score, index, block in sorted(ranks, key=lambda row: (-row[0], row[1]))[:5] if score]
    return {"version":version["id"], "items":hits, "comparisons":item.get("comparisons", [])[:5]}

_cache = {}
_cache_lock = Lock()
_last_lookup = 0.0

def crossref_request(doi):
    global _last_lookup
    with _cache_lock:
        entry = _cache.get(doi)
        if entry and entry[0] > monotonic():
            return entry[1]
        if monotonic() - _last_lookup < .35:
            return "throttled"
        _last_lookup = monotonic()
    conn = HTTPSConnection("api.crossref.org", timeout=5)
    try:
        conn.request("GET", "/works/" + quote(doi, safe="/"), headers={
            "Accept":"application/json", "User-Agent":"WenhengEditorialWorkspace/1.0"
        })
        response = conn.getresponse()
        if response.status == 404:
            result = "noCoverage"
        elif response.status == 429:
            return "throttled"
        elif response.status != 200:
            return "unavailable"
        else:
            result = json.loads(response.read(3 * 1024 * 1024))["message"]
        with _cache_lock:
            if len(_cache) > 256:
                _cache.pop(next(iter(_cache)))
            _cache[doi] = (monotonic() + 3600, result)
        return result
    except (OSError, HTTPException, ValueError, KeyError):
        return "unavailable"
    finally:
        conn.close()

def crossref_metadata(value):
    doi = re.sub(r"^(https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()
    if not DOI.fullmatch(doi) or len(doi) > 240:
        raise WorkflowError("请输入有效的 DOI。查询只发送 DOI。")
    result = crossref_request(doi)
    if isinstance(result, str):
        return {"doi":doi, "status":result}
    if not isinstance(result, dict) or str(result.get("DOI", "")).casefold() != doi:
        return {"doi":doi, "status":"unavailable"}
    notices = [notice.get("DOI", "") for notice in result.get("update-to", [])
               if isinstance(notice, dict) and notice.get("type") == "retraction"]
    year = (result.get("issued", {}).get("date-parts") or [[None]])[0][0]
    return {
        "doi":doi, "status":"found", "title":(result.get("title") or [""])[0],
        "journal":(result.get("container-title") or [""])[0],
        "authors":[" ".join(part for part in (author.get("given"), author.get("family") or author.get("name")) if part)
                   for author in result.get("author", [])[:12]],
        "year":year, "volume":result.get("volume", ""), "issue":result.get("issue", ""),
        "pages":result.get("page", ""), "retracted":bool(notices), "notices":notices
    }

def check_public_source(value):
    """Check an explicitly configured publisher source. Do not follow redirects."""
    hosts = {host.strip().lower() for host in os.environ.get("EDITOR_SOURCE_HOSTS","").split(",") if host.strip()}
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in hosts or url.port not in (None,443)
            or url.username or url.password or url.query or url.fragment
            or not re.fullmatch(r"(?=.{3,253}$)(?:[a-z0-9-]+\.)+[a-z0-9-]+",url.hostname or "")):
        raise WorkflowError("自动巡检仅接受已配置的公开 HTTPS 来源域名及不带参数的地址。")
    conn = HTTPSConnection(url.hostname,timeout=5)
    try:
        conn.request("HEAD",url.path or "/")
        response = conn.getresponse()
        return {"status":("ok" if 200<=response.status<300 else "redirect" if 300<=response.status<400
                          else "broken" if response.status in (404,410) else "unavailable"), "http":response.status}
    except (OSError,HTTPException):
        return {"status":"unavailable","http":None}
    finally:
        conn.close()
