"""SQLite persistence, local sessions and pilot calculations."""
from contextlib import closing
from datetime import date
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import re
import secrets
import sqlite3
from threading import RLock
from time import monotonic

from workflow import WorkflowError, seed
from research import CitationReceipts, projects

COLLECTIONS = ("manuscripts", "issues", "library", "trials", "accounts", "tasks", "deleted_manuscripts")
ATTACHMENT_NAME = re.compile(r"[a-f0-9]{14}\.(?:docx|txt|pdf|csv|xlsx)")
ROLES = {
    "admin":"系统管理员", "editor":"责任编辑", "reviewer":"学科编辑",
    "librarian":"资料管理员", "technical":"技术人员",
}
LOCAL = {"id":"local", "name":"林编辑", "role":"admin", "projects":"*", "local":True}
RIGHTS = {
    "editor": {"create", "flow", "upload", "decision", "review", "trial"},
    "reviewer": {"review", "decision", "approve", "trial"},
    "librarian": {"library", "maintenance"},
    "technical": {"maintenance", "audit"},
}
ACCOUNT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,31}")

class Forbidden(WorkflowError):
    pass

def permitted(account, ability):
    return bool(account and (account["role"] == "admin" or ability in RIGHTS.get(account["role"], ())))

def has_project(account, item):
    if not account:
        return False
    if account["role"] == "admin":
        return True
    scope = projects(account)
    return "*" in scope or item["id"] in scope or item.get("createdBy") == account["id"]

def visible_library(account, entry, manuscripts):
    if permitted(account, "library") or account["role"] == "admin":
        return True
    scope = projects(entry)
    if entry["status"] != "approved":
        return permitted(account, "approve") and ("*" in scope or any(m["id"] in scope for m in manuscripts))
    return "*" in scope or any(item["id"] in scope for item in manuscripts)

def public_account(account):
    return {key: value for key, value in account.items() if key not in {"salt","password"}}

def add_account(data, username, name, password, role, scope=""):
    username = username.strip().casefold()
    if not ACCOUNT_ID.fullmatch(username):
        raise WorkflowError("账号需以字母开头，长度 3–32 位，可用字母、数字、点、下划线和连字符。")
    if not name.strip() or role not in ROLES or len(password) < 10 or len(password) > 240:
        raise WorkflowError("请填写姓名、角色及至少 10 位口令。")
    if any(account["id"] == username for account in data.setdefault("accounts", [])):
        raise WorkflowError("账号已存在。")
    salt = secrets.token_hex(16)
    data["accounts"].append({"id":username,"name":name.strip(),"role":role,"projects":scope,
                             "salt":salt,"password":password_digest(password,salt)})

def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000).hex()

class Workspace:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / "workspace.sqlite3"
        self.lock = RLock()
        self.receipts = CitationReceipts()
        self.sessions = {}
        self.login_failures = {}
        with self.connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                collection TEXT NOT NULL, record_id TEXT NOT NULL, position INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (collection, record_id)
            );
            CREATE TABLE IF NOT EXISTS workspace_revision (singleton INTEGER PRIMARY KEY CHECK(singleton=1), seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (time TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
                object_id TEXT NOT NULL, duration_ms INTEGER NOT NULL, status INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS workspace_order ON records(collection, position);
            """)
            self.revision = self.db_revision(connection)
        if self.revision == 0:
            legacy = directory / "workspace.json"
            saved = json.loads(legacy.read_text(encoding="utf-8")) if legacy.is_file() else seed()
            with self.lock:
                self.save(saved)
        else:
            self.data = self.load()

    def connect(self):
        conn = sqlite3.connect(self.database, timeout=5)
        conn.execute("PRAGMA secure_delete=ON")
        return closing(conn)

    @staticmethod
    def db_revision(connection):
        row = connection.execute("SELECT seq FROM workspace_revision WHERE singleton=1").fetchone()
        return row[0] if row else 0

    def load(self):
        data = {key: [] for key in COLLECTIONS}
        with self.connect() as connection:
            rows = connection.execute("SELECT collection,payload FROM records ORDER BY collection,position")
            for key, payload in rows:
                data[key].append(json.loads(payload))
        return data

    def sync(self):
        with self.connect() as connection:
            revision = self.db_revision(connection)
        if revision != self.revision:
            self.data = self.load()
            self.revision = revision

    def save(self, data):
        with self.connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.db_revision(connection) != self.revision:
                    raise WorkflowError("工作区已由另一进程更新，请刷新并重试。")
                for key in COLLECTIONS:
                    records = data.setdefault(key, [])
                    persisted = {record[0] for record in connection.execute(
                        "SELECT record_id FROM records WHERE collection=?", (key,))}
                    deleted = persisted.difference(record["id"] for record in records)
                    connection.executemany("DELETE FROM records WHERE collection=? AND record_id=?",
                                           ((key, ident) for ident in deleted))
                    connection.executemany("""
                      INSERT INTO records (collection,record_id,position,payload) VALUES (?,?,?,?)
                      ON CONFLICT (collection,record_id) DO UPDATE SET position=excluded.position,payload=excluded.payload
                      WHERE records.position<>excluded.position OR records.payload<>excluded.payload
                      """, ((key, record["id"], index, json.dumps(record, ensure_ascii=False, separators=(",",":")))
                            for index, record in enumerate(records)))
                connection.execute("INSERT INTO workspace_revision VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET seq=excluded.seq",
                                   (self.revision + 1,))
        self.revision += 1
        self.data = data

    def clear_deleted_files(self):
        """Retry file cleanup for manuscripts whose deletion has been committed."""
        deleted = self.data["deleted_manuscripts"]
        if not any(record["files"] for record in deleted):
            return
        updated = []
        for record in deleted:
            pending = []
            for name in record["files"]:
                try:
                    (self.directory / name).unlink(missing_ok=True)
                except OSError:
                    pending.append(name)
            updated.append({**record,"files":pending})
        if updated != deleted:
            self.save({**self.data,"deleted_manuscripts":updated})

    def identity(self, cookies):
        if not self.data.get("accounts"):
            return LOCAL
        jar = SimpleCookie()
        jar.load(cookies or "")
        token = jar.get("wenheng")
        session = self.sessions.get(token.value) if token else None
        if session and session[1] > monotonic():
            return next((account for account in self.data["accounts"] if account["id"] == session[0]), None)
        return None

    def login(self, username, password):
        username = username.casefold()
        expires, attempts = self.login_failures.get("local", (0,0))
        if expires <= monotonic():
            expires, attempts = monotonic() + 90, 0
        if attempts >= 20:
            raise Forbidden("尝试次数已达上限，请稍后登录。")
        account = next((account for account in self.data["accounts"] if account["id"] == username), None)
        salt = account["salt"] if account else "0" * 32
        digest = password_digest(password, salt)
        if not account or not hmac.compare_digest(account["password"], digest):
            self.login_failures["local"] = (expires, attempts + 1)
            raise Forbidden("账号或口令不正确。")
        self.login_failures.pop("local", None)
        self.sessions = {key:value for key,value in self.sessions.items() if value[1] > monotonic()}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (username, monotonic() + 8 * 3600)
        return token

    def logout(self, cookies):
        jar = SimpleCookie()
        jar.load(cookies or "")
        if "wenheng" in jar:
            self.sessions.pop(jar["wenheng"].value, None)

    def audit(self, when, actor, operation, object_id, duration, status):
        with self.connect() as connection, connection:
            connection.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)", (when,actor,operation,object_id,duration,status))

    def audit_rows(self):
        with self.connect() as connection:
            return [dict(zip(("time","actor","operation","objectId","duration","status"), row))
                    for row in connection.execute("SELECT * FROM audit ORDER BY rowid DESC LIMIT 100")]

def pilot_summary(trials):
    total = lambda key: sum(record[key] for record in trials)
    truth, false, missed = (total(key) for key in ("truePositive","falsePositive","falseNegative"))
    saved = total("traditionalMinutes") - total("assistedMinutes")
    words = total("words")
    per_case = [(row["traditionalMinutes"] - row["assistedMinutes"]) / 60 * row["hourly"] - row["runtimeCost"]
                for row in trials]
    fixed = [row["fixedCost"] / row["volume"] for row in trials]
    net = [round(value - costs, 2) for value, costs in zip(per_case,fixed)]
    return {"samples":len(trials), "words":words, "precision":truth/(truth+false) if truth+false else None,
            "recall":truth/(truth+missed) if truth+missed else None,
            "falsePositiveShare":false/(truth+false) if truth+false else None,
            "savedMinutes":saved, "savedMinutesPer10k":saved*10000/words if words else None,
            "netSavings":round(sum(net)/len(net),2) if net else None, "perCase":net,
            "breakEven":[int(-(-row["fixedCost"] // contribution)) if contribution>0 else None
                         for row,contribution in zip(trials,per_case)]}

def expired_version(version, today=None):
    return bool(version.get("retired") or (version.get("retentionUntil")
                and version["retentionUntil"] <= (today or date.today().isoformat())))

def retention_items(data):
    return [(item,version) for item in data["manuscripts"] if item["status"] in {"archived","rejected"}
            for version in item["versions"] if version.get("purgeFile") or not version.get("retired") and expired_version(version)]

def purge_retained(store, apply=False):
    from copy import deepcopy
    from copyright_review import redact_copyright_record
    with store.lock:
        store.sync()
        data = deepcopy(store.data)
        removals = retention_items(data)
        identifiers = [(item["id"],version["id"]) for item,version in removals]
        if not apply or not identifiers:
            return identifiers
        discard = {version["id"] for _,version in removals}
        for item, version in removals:
            version.update(retired=True, purgeFile=version.get("purgeFile",version["file"]),
                           file="", blocks=[], changes=[])
            for item_version in item["versions"]:
                item_version["changes"] = []
        for issue in data["issues"]:
            if issue["version"] in discard:
                issue.update(title="已到期版本的审校记录",detail="",evidence=[],note="",anchor=None,rights={})
            elif issue.get("rights"):
                rights = redact_copyright_record(issue["rights"], discard)
                issue["rights"] = rights
                if not rights:
                    issue["rightsRetired"] = True
        store.save(data)
        for _,version in removals:
            name = version.get("purgeFile","")
            if name and not ATTACHMENT_NAME.fullmatch(name):
                raise WorkflowError("待删除原件的文件标识异常。")
            if name:
                (store.directory/name).unlink(missing_ok=True)
            version["purgeFile"] = ""
        store.save(data)
        return identifiers
