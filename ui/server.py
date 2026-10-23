from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_graph import run_agent_graph
from src.agents.solution_retrieval import retrieve_solutions
from src.intent import detect_intent
from src.llm_client import LLMClient, load_config
from src.rag import managed_store
from src.rag.ingest import SUPPORTED
from src.rag.vector_store import get_cached_store, index_directory, load_documents
from src.ticket_parser import parse_ticket
from src.web_search import search_web

# Load the local .env before route authentication is evaluated. The loader only
# imports environment values and never writes secrets back to the repository.
load_config()

INDEX = Path(__file__).with_name("index.html")
BACKEND_INDEX = Path(__file__).with_name("backend.html")
RAG_PAGE = Path(__file__).with_name("rag.html")
DB_PATH = Path(os.getenv("HELPDESK_DB_PATH", str(Path(__file__).with_name("helpdesk.db"))))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH
SKILLS_PATH = Path(__file__).with_name("skills")
RAG_IMPORT_PATH = ROOT / "data" / "knowledge" / "imports"
MAX_REQUEST_BYTES = 25 * 1024 * 1024
MAX_IMPORT_BYTES = 10 * 1024 * 1024
BACKEND_ONLY = os.getenv("HELPDESK_UI_MODE", "").lower() == "backend"
_DB_SCHEMA_LOCK = threading.Lock()
_SESSION_SECRET = os.getenv("HELPDESK_SESSION_SECRET", "") or secrets.token_urlsafe(32)
IDENTITY_COOKIE = "helpdesk_identity"
ADMIN_COOKIE = "helpdesk_admin"
LOGIN_PAGE = Path(__file__).with_name("login.html")
ADMIN_SESSION_DEFAULT_DAYS = 30
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 300
LOGIN_RETRY_AFTER = 60
# Very small in-memory throttle: per client IP, timestamps of rejected logins.
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()
# Set once the local BGE model/Chroma collection is loaded in this process.  The
# first embedding call costs several seconds, so a background warmup keeps the
# first real RAG query fast without delaying service startup.
_RAG_WARMED = threading.Event()

ADMIN_PAGE_ROUTES = {"/backend", "/backend.html", "/admin", "/admin.html", "/rag", "/rag.html"}
RAG_PAGE_ROUTES = {"/rag", "/rag.html"}
ADMIN_API_ROUTES = {
    "/api/tickets", "/api/tickets/update", "/api/backend/runs", "/api/backend/trace",
    "/api/rag/files", "/api/rag/status", "/api/rag/documents", "/api/rag/import",
    "/api/rag/rebuild", "/api/rag/search", "/api/rag/library", "/api/rag/entries",
    "/api/rag/entries/create", "/api/rag/entries/update", "/api/rag/entries/delete",
    "/api/rag/entries/move", "/api/rag/documents/delete",
    "/api/audit/users", "/api/audit/logs",
}
# Login/logout are intentionally absent from ADMIN_API_ROUTES: they must stay
# reachable without an existing session. POST routing also whitelists them.
POST_ROUTES = {
    "/api/review", "/api/chat", "/api/rag/import", "/api/rag/rebuild",
    "/api/rag/search", "/api/tickets/update", "/api/admin/login", "/api/admin/logout",
    "/api/rag/entries/create", "/api/rag/entries/update", "/api/rag/entries/delete",
    "/api/rag/entries/move", "/api/rag/documents/delete",
}
USER_GET_ROUTES = {"/api/history", "/api/conversation", "/api/sessions", "/api/messages", "/api/trace"}


def _admin_credentials() -> tuple[str, str]:
    return (os.getenv("HELPDESK_ADMIN_USER", "").strip(),
            os.getenv("HELPDESK_ADMIN_PASSWORD", ""))


def _is_admin_route(route: str) -> bool:
    return route in ADMIN_PAGE_ROUTES or route in ADMIN_API_ROUTES


def _check_basic_auth(header: str | None) -> bool:
    username, password = _admin_credentials()
    if not username or not password or not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        supplied_user, separator, supplied_password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return separator == ":" and hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password)


def _session_secret() -> str:
    """Read the signing secret lazily so tests/restarts pick up environment changes."""
    return os.getenv("HELPDESK_SESSION_SECRET", "").strip() or _SESSION_SECRET


def _admin_session_ttl() -> int:
    """Cookie lifetime in seconds; HELPDESK_ADMIN_SESSION_DAYS defaults to 30."""
    raw = os.getenv("HELPDESK_ADMIN_SESSION_DAYS", "").strip()
    try:
        days = float(raw) if raw else float(ADMIN_SESSION_DEFAULT_DAYS)
    except ValueError:
        days = float(ADMIN_SESSION_DEFAULT_DAYS)
    return int(min(max(days, 0.0), 3650.0) * 86400)


def _admin_cookie_key(username: str, password: str) -> bytes:
    """Derive the HMAC key from the deployment secret plus the admin credentials.

    Including username/password means rotating the password invalidates every
    outstanding admin cookie without a separate revocation list.
    """
    material = f"helpdesk-admin-v1\0{username}\0{password}".encode()
    return hmac.new(_session_secret().encode("utf-8"), material, "sha256").digest()


def _admin_token(username: str, password: str, issued: int | None = None) -> str:
    encoded = base64.urlsafe_b64encode(username.encode("utf-8")).decode("ascii").rstrip("=")
    issued_at = int(time.time()) if issued is None else int(issued)
    payload = f"{encoded}.{issued_at}"
    signature = hmac.new(_admin_cookie_key(username, password), payload.encode("ascii"), "sha256").digest()
    signed = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload}.{signed}"


def _admin_token_user(token: str | None, username: str, password: str) -> str:
    """Return the signed-in username for a valid admin cookie, else ''.

    Verification is constant-time on the signature and rejects expired tokens
    (HELPDESK_ADMIN_SESSION_DAYS) as well as tokens issued too far in the future.
    """
    if not token:
        return ""
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    encoded, issued_text, supplied_signature = parts
    try:
        issued = int(issued_text)
    except (TypeError, ValueError):
        return ""
    now = int(time.time())
    if issued > now + 300 or now - issued > _admin_session_ttl():
        return ""
    payload = f"{encoded}.{issued}"
    expected_signature = base64.urlsafe_b64encode(
        hmac.new(_admin_cookie_key(username, password), payload.encode("ascii"), "sha256").digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return ""
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return decoded if hmac.compare_digest(decoded, username) else ""


def _safe_next(raw: str | None, default: str = "/backend") -> str:
    """Only allow same-origin absolute paths (single leading '/') as redirects."""
    candidate = (raw or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return default
    if any(ord(char) < 0x20 for char in candidate):
        return default
    return candidate


def _login_throttled(client_ip: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        attempts = [stamp for stamp in _LOGIN_FAILURES.get(client_ip, ()) if now - stamp < LOGIN_WINDOW_SECONDS]
        if attempts:
            _LOGIN_FAILURES[client_ip] = attempts
        else:
            _LOGIN_FAILURES.pop(client_ip, None)
        return len(attempts) >= LOGIN_MAX_FAILURES


def _record_login_failure(client_ip: str) -> None:
    now = time.time()
    with _LOGIN_LOCK:
        attempts = [stamp for stamp in _LOGIN_FAILURES.get(client_ip, ()) if now - stamp < LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        # Bound memory usage: only the most recent window worth of stamps matter.
        _LOGIN_FAILURES[client_ip] = attempts[-(LOGIN_MAX_FAILURES * 2):]


def _clear_login_failures(client_ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(client_ip, None)


def _identity_token(user_id: str) -> str:
    encoded = base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), "sha256").digest()
    signed = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded}.{signed}"


def _identity_from_token(token: str | None) -> str:
    if not token or "." not in token:
        return ""
    encoded, supplied_signature = token.split(".", 1)
    try:
        expected = _identity_token_from_encoded(encoded).split(".", 1)[1]
    except (UnicodeEncodeError, ValueError):
        return ""
    if not hmac.compare_digest(supplied_signature, expected):
        return ""
    try:
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _identity_token_from_encoded(encoded: str) -> str:
    signature = hmac.new(_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), "sha256").digest()
    return f"{encoded}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


class ChatReplyItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1)


class ChatReplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[ChatReplyItem]


def llm_reply(messages: list[dict[str, str]]) -> str:
    """Call the shared client and validate the required object contract."""
    payload = LLMClient().json_completion(messages, retries=3)
    result = ChatReplyResult.model_validate(payload)
    if not result.results:
        raise ValueError("LLM results 为空")
    return result.results[0].reply.strip()


def load_skills() -> list[dict]:
    SKILLS_PATH.mkdir(exist_ok=True)
    skills = []
    for path in sorted(SKILLS_PATH.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        skills.append({"name": path.stem, "content": text, "enabled": True})
    return skills


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    with _DB_SCHEMA_LOCK:
        conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            report_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unresolved',
            resolution_note TEXT NOT NULL DEFAULT '',
            status_updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(user_id, ticket_id)
        )""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        migrations = {
            "status": "ALTER TABLE conversations ADD COLUMN status TEXT NOT NULL DEFAULT 'unresolved'",
            "resolution_note": "ALTER TABLE conversations ADD COLUMN resolution_note TEXT NOT NULL DEFAULT ''",
            "status_updated_at": "ALTER TABLE conversations ADD COLUMN status_updated_at TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        node TEXT NOT NULL,
        status TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        result_count INTEGER,
        error TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS access_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    # Indexes must be created after every referenced table exists.  This order
    # also keeps first-run and legacy-schema migrations safe.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(user_id, session_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_traces_session ON agent_traces(user_id, session_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_logs_user_created ON access_logs(user_id, created_at)")
    conn.commit()
    return conn


def ticket_row(row: sqlite3.Row) -> dict:
    """Return a stable, UI-safe ticket summary with triage fields flattened."""
    try:
        report = json.loads(row["report_json"] or "{}")
    except (TypeError, ValueError):
        report = {}
    ticket = report.get("ticket", {}) if isinstance(report, dict) else {}
    triage = report.get("triage", {}) if isinstance(report, dict) else {}
    classification = triage.get("classification", {}) if isinstance(triage, dict) else {}
    routing = triage.get("routing", {}) if isinstance(triage, dict) else {}
    return {
        "ticket_id": row["ticket_id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "status": row["status"] or "unresolved",
        "resolution_note": row["resolution_note"] or "",
        "status_updated_at": row["status_updated_at"] or "",
        "requester": ticket.get("requester", ""),
        "description": ticket.get("description", ""),
        "category": classification.get("category", "待确认"),
        "subcategory": classification.get("subcategory", ""),
        "priority": triage.get("priority", "待确认"),
        "sla_hours": triage.get("sla_hours", ""),
        "team": routing.get("team", "待分派"),
        "report": report,
    }


def save_message(user_id: str, session_id: str, role: str, content: str) -> None:
    conn = db()
    conn.execute("INSERT INTO chat_messages(user_id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
                 (user_id, session_id, role, content, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def save_trace(user_id: str, session_id: str, event: dict) -> None:
    conn = db()
    conn.execute("INSERT INTO agent_traces(user_id,session_id,node,status,timestamp,result_count,error) VALUES(?,?,?,?,?,?,?)",
                 (user_id, session_id, event.get("node", ""), event.get("status", ""), event.get("timestamp", datetime.now().isoformat()), event.get("result_count"), event.get("error")))
    conn.commit()
    conn.close()


def record_access(user_id: str, session_id: str, method: str, path: str) -> None:
    """Persist a lightweight audit record for later per-user monitoring."""
    try:
        conn = db()
        conn.execute(
            "INSERT INTO access_logs(user_id,session_id,method,path,created_at) VALUES(?,?,?,?,?)",
            (user_id, session_id, method, path, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        # Audit logging is best effort and must not take down health checks or
        # user-facing requests when the database is temporarily unavailable.
        print(f"[ui] access log skipped: {exc}", file=sys.stderr)


def compact_history(rows: list[sqlite3.Row]) -> list[dict[str, str]]:
    """Keep the latest three turns in chronological order.

    ``chat_reply`` reads rows newest-first for the SQL limit.  Select from the
    front of that result before reversing; slicing the tail would silently
    discard the newest messages and make the model appear to lose context.
    Content is bounded independently by role so one verbose answer cannot
    crowd out the user's recent turns.
    """
    compacted: list[dict[str, str]] = []
    previous_assistant = ""
    for row in reversed(rows[:6]):
        role = row["role"]
        limit = 700 if role == "assistant" else 500
        content = " ".join(str(row["content"]).split())[:limit]
        if role == "assistant" and content == previous_assistant:
            continue
        if role == "assistant":
            previous_assistant = content
        compacted.append({"role": role, "content": content})
    return compacted


def retrieval_context(message: str, history: list[dict[str, str]]) -> str:
    """Build a short retrieval query for follow-ups without replaying answers.

    A follow-up such as “还是不行” has little lexical signal on its own.  Keep
    the current message first, then add at most the two latest user turns.  We
    deliberately exclude assistant text so a previous model hallucination or
    long answer cannot pollute the RAG query.
    """
    current = " ".join(message.split())[:300]
    previous_user = [" ".join(item["content"].split())[:100]
                     for item in history if item.get("role") == "user"][-2:]
    parts = previous_user + ([current] if current else [])
    return "\n".join(parts)[:500]


def request_identity(payload: dict) -> tuple[str, str]:
    return str(payload.get("user_id", "")).strip(), str(payload.get("session_id", "")).strip()


def chat_reply(user_id: str, session_id: str, message: str, online_search: bool = False) -> dict:
    skills = load_skills()
    conn = db()
    rows = conn.execute("SELECT role,content FROM chat_messages WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT 8", (user_id, session_id)).fetchall()
    conn.close()
    history = compact_history(rows)
    save_message(user_id, session_id, "user", message)
    context = "\n\n".join(f"# Skill: {item['name']}\n{item['content']}" for item in skills)
    system = "你是一个 IT 运维 Helpdesk Agent。用中文回答，先理解用户目标，再给出可执行步骤。不要编造知识库内容；无法确认时明确说明。若用户提供完整工单字段或要求分诊，建议用户使用分诊工具。" + (f"\n用户导入的 Skills：\n{context}" if context else "")
    trace_events: list[dict] = []
    intent_payload = detect_intent(message)
    intent = intent_payload.get("results", [{}])[0] if intent_payload.get("results") else {"intent": "general", "reply_mode": "conversation"}
    trace_events.append({"node": "intent", "status": "completed", "intent": intent.get("intent"), "reply_mode": intent.get("reply_mode")})
    triage: dict = {}
    web_results = search_web(message) if online_search else []
    if online_search:
        trace_events.append({"node": "web_search", "status": "completed", "result_count": len(web_results)})
    if intent.get("reply_mode") == "ticket_form":
        reply = "可以，我会为你进入工单提交流程。请点击右侧或下方的“提交工单”入口，填写标题、描述、申请人和影响范围；提交后系统会进行分类、优先级、RAG 检索与路由。"
    elif intent.get("reply_mode") == "rag":
        query_text = retrieval_context(message, history)
        ticket = parse_ticket({"title": message[:120], "description": query_text, "channel": "chat"})["ticket"]
        # Questions use the local knowledge base first; the full triage graph is reserved for ticket submission.
        def _event(node, status, **extra):
            return trace_events.append({"node": node, "status": status, **extra})
        _event("parse", "completed")
        _event("solution_retrieval", "running")
        # ``retrieve_solutions`` owns the index-readiness and model-error
        # fallback.  Do not construct the cached store outside it: a missing
        # local model/index must result in an empty match set, not an HTTP 500.
        solutions = retrieve_solutions(ticket, top_k=3)
        _event("solution_retrieval", "completed", result_count=len(solutions.get("results", [])))
        matches = solutions.get("results", [{}])[0].get("matches", []) if solutions.get("results") else []
        strong_matches = [match for match in matches if match.get("relevance") == "高"]
        grounded = "\n".join(f"来源：{m['source']}｜{m['title']}\n步骤：" + "；".join(m.get("steps", [])) for m in strong_matches)
        web_context = "\n".join(f"- {item['title']}：{item['snippet']}（{item['url']}）" for item in web_results)
        subject = message.replace("\n", " ").strip()[:80]
        prompt = system + f"\n你现在是运维 Agent。回复必须以‘针对“{subject}”这一现象：’开头，明确回答当前这一轮，不要把上一轮问题的步骤混入本轮。仅依据高相关本地检索结果回答；在确有联网结果时参考公开网页；不要声称执行了未执行的操作。给出判断、步骤和追问。\n本地检索结果：\n" + (grounded or "（没有高相关命中）") + ("\n公开网页结果（仅供参考）：\n" + web_context if web_context else "")
        try:
            if not grounded and not web_context:
                raise RuntimeError("没有高相关本地结果，等待用户开启在线搜索")
            reply = llm_reply([{"role": "system", "content": prompt}, *history, {"role": "user", "content": message}])
        except (ValidationError, RuntimeError, TypeError, ValueError):
            if grounded:
                steps = []
                for match in strong_matches[:2]:
                    steps.extend(match.get("steps", [])[:4])
                reply = "我根据当前症状整理了这组排查建议：\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
                reply += "\n\n请告诉我执行到哪一步、出现了什么结果；如果仍未解决，可以提交工单让运维人员处理。"
            else:
                if online_search:
                    online_hint = "在线搜索暂时没有返回可用结果，请稍后重试或补充完整报错、设备和已尝试操作"
                else:
                    online_hint = "请打开对话输入框下方的“在线搜索”后重试，我会补充检索公开资料"
                reply = f"针对“{message.replace(chr(10), ' ').strip()[:80]}”这一现象：当前本地知识库没有高相关的解决方案。{online_hint}；也可以提交工单让运维人员进一步排查。"
        if not reply.startswith("针对"):
            reply = f"针对“{subject}”这一现象：\n{reply}"
        if "提交工单" not in reply:
            reply += "\n\n如仍未解决，可以提交工单让运维人员处理。"
    else:
        if intent.get("intent") == "greeting":
            reply = "你好，我是 Helpdesk Agent。你可以直接描述遇到的现象、报错和已尝试操作，我会先判断问题类型，再给出排查建议；需要人工处理时也可以提交工单。"
        else:
            reply = ""
        if reply:
            save_message(user_id, session_id, "assistant", reply)
            for event in trace_events:
                save_trace(user_id, session_id, event)
            return {"session_id": session_id, "reply": reply, "intent": intent, "skills_used": [item["name"] for item in skills], "trace": trace_events, "graph": {"nodes": ["intent", "conversation"], "edges": [["intent", "conversation"]]}, "triage": triage}
        web_context = "\n".join(f"- {item['title']}：{item['snippet']}（{item['url']}）" for item in web_results)
        prompt = system + "\n当前意图是一般对话。请自然回答；不要虚构已执行的操作。" + ("\n已启用在线搜索，以下公开网页仅供参考：\n" + web_context if web_context else "")
        try:
            reply = llm_reply([{"role": "system", "content": prompt}, *history, {"role": "user", "content": message}])
        except (ValidationError, RuntimeError, TypeError, ValueError):
            reply = "你好，我是 Helpdesk Agent，可以陪你梳理问题、检索运维知识；如果需要人工处理，也可以随时提交工单。"
    if not reply:
        reply = "我已收到你的问题，但暂时无法生成有效回复。请补充更多上下文后重试。"
    save_message(user_id, session_id, "assistant", reply)
    for event in trace_events:
        save_trace(user_id, session_id, event)
    return {"session_id": session_id, "reply": reply, "intent": intent, "skills_used": [item["name"] for item in skills], "trace": trace_events,
            "graph": {"nodes": ["parse", "classify", "priority", "solution_retrieval", "routing", "aggregate"],
                      "edges": [["parse", "classify"], ["parse", "priority"], ["parse", "solution_retrieval"], ["classify", "routing"], ["priority", "aggregate"], ["solution_retrieval", "aggregate"], ["routing", "aggregate"]]},
            "triage": triage}


def review_ticket(payload: dict) -> dict:
    user_id = str(payload.get("user_id", "")).strip()
    if not user_id:
        raise ValueError("请先填写用户标识")
    fields = payload.get("ticket") or {}
    if not fields.get("title") or not fields.get("description"):
        raise ValueError("标题和描述不能为空")
    fields = dict(fields)
    original_ticket_id = fields.get("ticket_id", "").strip()
    base_id = original_ticket_id or "T"
    # Every submission is a new isolated conversation, even when fields are edited or
    # the user reuses the same visible ticket number.
    fields["ticket_id"] = f"{base_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}"
    fields["source_ticket_id"] = original_ticket_id
    parsed = parse_ticket({"ticket": fields})
    ticket = parsed["ticket"]
    session_id = str(payload.get("session_id", "")).strip() or ticket["ticket_id"]
    state = run_agent_graph(
        ticket,
        sink=lambda event: save_trace(user_id, session_id, event),
    )
    report = state["report"]
    report["session_id"] = session_id
    conn = db()
    now = datetime.now().isoformat(timespec="seconds")
    report["management"] = {"status": "unresolved", "resolution_note": "", "status_updated_at": now}
    conn.execute("""INSERT OR REPLACE INTO conversations(
        user_id,ticket_id,title,created_at,report_json,status,resolution_note,status_updated_at
    ) VALUES(?,?,?,?,?,?,?,?)""", (
        user_id, ticket["ticket_id"], ticket["title"], now,
        json.dumps(report, ensure_ascii=False), "unresolved", "", now,
    ))
    conn.commit()
    conn.close()
    return report


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str, set_cookie_user: str = "",
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        if set_cookie_user:
            self._set_identity_cookie(set_cookie_user)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_value(self, name: str) -> str:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except (CookieError, ValueError):
            return ""
        morsel = cookies.get(name)
        return morsel.value if morsel else ""

    def _cookie_user_id(self) -> str:
        return _identity_from_token(self._cookie_value(IDENTITY_COOKIE))

    def _bind_request_user(self, payload: dict) -> str | None:
        """Use a signed server identity instead of trusting user_id from URLs."""
        current = self._cookie_user_id()
        if current:
            # The client value is only a legacy/display field. Always bind the
            # operation to the signed cookie identity.
            payload["user_id"] = current
            return ""
        # A browser-supplied identifier is only a display hint. The first
        # server request receives a fresh opaque identity, preventing callers
        # from selecting another user's database namespace.
        user_id = uuid4().hex
        payload["user_id"] = user_id
        return user_id

    def _user_for_query(self) -> str | None:
        user_id = self._cookie_user_id()
        if not user_id:
            self._send(401, json.dumps({"error": "请先通过聊天或提交工单建立会话"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return None
        return user_id

    def _set_identity_cookie(self, user_id: str) -> None:
        secure = "; Secure" if os.getenv("HELPDESK_COOKIE_SECURE", "0") == "1" else ""
        self.send_header("Set-Cookie", f"{IDENTITY_COOKIE}={_identity_token(user_id)}; Path=/; HttpOnly; SameSite=Lax{secure}")

    def _admin_cookie_secure(self) -> bool:
        """Secure cookie when explicitly enabled, else auto-detect HTTPS proxy."""
        raw = os.getenv("HELPDESK_COOKIE_SECURE")
        if raw is not None and raw.strip() != "":
            return raw.strip() == "1"
        forwarded = self.headers.get("X-Forwarded-Proto", "") or ""
        return forwarded.split(",")[0].strip().lower() == "https"

    def _admin_authenticated(self) -> bool:
        username, password = _admin_credentials()
        if not username or not password:
            return False
        # Basic auth stays accepted for curl/back-compat, but is never challenged.
        if _check_basic_auth(self.headers.get("Authorization")):
            return True
        return bool(_admin_token_user(self._cookie_value(ADMIN_COOKIE), username, password))

    def _admin_cookie_header(self, username: str, password: str) -> str:
        ttl = _admin_session_ttl()
        secure = "; Secure" if self._admin_cookie_secure() else ""
        token = _admin_token(username, password)
        return f"{ADMIN_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ttl}{secure}"

    def _cleared_admin_cookie_header(self) -> str:
        secure = "; Secure" if self._admin_cookie_secure() else ""
        return (f"{ADMIN_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; "
                f"Expires=Thu, 01 Jan 1970 00:00:00 GMT{secure}")

    def _require_admin(self, route: str, page: bool = False) -> bool:
        """Gate admin routes without ever emitting a WWW-Authenticate challenge.

        The native Basic-auth dialog is browser-triggered by that header, so
        API callers receive JSON 401 and page navigations are redirected to the
        cookie-based login page instead.
        """
        username, password = _admin_credentials()
        if not username or not password:
            self._send(503, json.dumps({"error": "后台未配置管理员账号，请设置 HELPDESK_ADMIN_USER 和 HELPDESK_ADMIN_PASSWORD"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return False
        if self._admin_authenticated():
            return True
        if page:
            self._send(302, b"", "text/html; charset=utf-8",
                       extra_headers={"Location": f"/login?next={quote(route, safe='/')}"})
        else:
            body = json.dumps({"error": "登录已过期，请重新登录", "login": "/login?next=/backend"}, ensure_ascii=False).encode()
            self._send(401, body, "application/json; charset=utf-8")
        return False

    def _handle_admin_login(self, payload: dict) -> None:
        username, password = _admin_credentials()
        if not username or not password:
            self._send(503, json.dumps({"error": "后台未配置管理员账号，请设置 HELPDESK_ADMIN_USER 和 HELPDESK_ADMIN_PASSWORD"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        client_ip = self.client_address[0] if self.client_address else ""
        if _login_throttled(client_ip):
            self._send(429, json.dumps({"error": "登录尝试次数过多，请稍后再试"}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", extra_headers={"Retry-After": str(LOGIN_RETRY_AFTER)})
            return
        supplied_user = str(payload.get("username", ""))
        supplied_password = str(payload.get("password", ""))
        user_ok = hmac.compare_digest(supplied_user.encode("utf-8"), username.encode("utf-8"))
        password_ok = hmac.compare_digest(supplied_password.encode("utf-8"), password.encode("utf-8"))
        if not (user_ok and password_ok):
            _record_login_failure(client_ip)
            self._send(401, json.dumps({"error": "用户名或密码错误"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        _clear_login_failures(client_ip)
        body = json.dumps({"ok": True, "redirect": "/backend"}, ensure_ascii=False).encode()
        self._send(200, body, "application/json; charset=utf-8",
                   extra_headers={"Set-Cookie": self._admin_cookie_header(username, password)})

    def do_GET(self) -> None:
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(self.path)
        route = parsed_url.path
        query = parse_qs(parsed_url.query)
        if route == "/healthz":
            self._send(200, b'{"status":"ok"}', "application/json; charset=utf-8")
            return
        # Serve the login page before the admin guard; it is the destination for
        # unauthenticated page navigations and must never be gated itself.
        if route in ("/login", "/login.html"):
            if self._admin_authenticated():
                self._send(302, b"", "text/html; charset=utf-8",
                           extra_headers={"Location": _safe_next(query.get("next", [""])[0])})
                return
            self._send(200, LOGIN_PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if route in ADMIN_PAGE_ROUTES and not self._require_admin(route, page=True):
            return
        if route in ADMIN_API_ROUTES and not self._require_admin(route):
            return
        if route in USER_GET_ROUTES:
            current_user = self._cookie_user_id()
            requested_user = query.get("user_id", [""])[0].strip()
            if not current_user:
                # Keep the first page load usable, while returning no data
                # until the browser has established its signed identity.
                if route in {"/api/history", "/api/sessions"}:
                    self._send(200, b"[]", "application/json; charset=utf-8")
                else:
                    self._send(401, json.dumps({"error": "请先通过聊天或提交工单建立会话"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            if requested_user != current_user:
                self._send(403, json.dumps({"error": "用户身份不匹配"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
        audit_user = self._cookie_user_id() if route in USER_GET_ROUTES else query.get("user_id", [""])[0].strip()
        record_access(audit_user, query.get("session_id", [""])[0].strip(), "GET", route)
        if route in ("/", "/index.html") and BACKEND_ONLY:
            self._send(200, BACKEND_INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if route in ("/", "/index.html"):
            # The user-facing page is a self-contained static file.  All client
            # behaviour (chat, ticket modal, online search, identity) lives in
            # ui/index.html; the server no longer rewrites the HTML at runtime.
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if route in RAG_PAGE_ROUTES:
            # ui/rag.html is ui-eng's page; serve it when deployed, otherwise a
            # friendly JSON 404 instead of a generic "Not found".
            if not RAG_PAGE.is_file():
                self._send(404, json.dumps({"error": "RAG 管理页面尚未部署"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            self._send(200, RAG_PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if route in ADMIN_PAGE_ROUTES:
            self._send(200, BACKEND_INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        from urllib.parse import parse_qs, urlparse
        if parsed_url.path == "/api/tickets":
            status = query.get("status", ["all"])[0].strip().lower()
            keyword = query.get("q", [""])[0].strip()
            if status not in {"all", "resolved", "unresolved"}:
                self._send(400, b'{"error":"status must be all, resolved or unresolved"}', "application/json; charset=utf-8")
                return
            conn = db()
            where = ["1=1"]
            params: list[str | int] = []
            if status != "all":
                where.append("status=?")
                params.append(status)
            if keyword:
                where.append("(ticket_id LIKE ? OR title LIKE ? OR user_id LIKE ? OR report_json LIKE ?)")
                pattern = f"%{keyword}%"
                params.extend([pattern, pattern, pattern, pattern])
            rows = conn.execute(
                "SELECT * FROM conversations WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 200",
                params,
            ).fetchall()
            counts = conn.execute("SELECT status, COUNT(*) AS count FROM conversations GROUP BY status").fetchall()
            conn.close()
            stats = {"all": 0, "resolved": 0, "unresolved": 0}
            for item in counts:
                stats[item["status"] if item["status"] in stats else "unresolved"] += item["count"]
            stats["all"] = stats["resolved"] + stats["unresolved"]
            self._send(200, json.dumps({"tickets": [ticket_row(row) for row in rows], "stats": stats}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/history":
            query = parse_qs(urlparse(self.path).query)
            user_id = query.get("user_id", [""])[0].strip()
            keyword = query.get("q", [""])[0].strip()
            conn = db()
            rows = conn.execute("SELECT ticket_id,title,created_at,status,resolution_note FROM conversations WHERE user_id=? AND (title LIKE ? OR ticket_id LIKE ?) ORDER BY id DESC",
                                (user_id, f"%{keyword}%", f"%{keyword}%")).fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/conversation":
            query = parse_qs(urlparse(self.path).query)
            user_id, ticket_id = query.get("user_id", [""])[0], query.get("ticket_id", [""])[0]
            conn = db()
            row = conn.execute("SELECT report_json FROM conversations WHERE user_id=? AND ticket_id=?", (user_id, ticket_id)).fetchone()
            conn.close()
            if not row:
                self._send(404, b"{}", "application/json; charset=utf-8")
            else:
                self._send(200, row["report_json"].encode(), "application/json; charset=utf-8")
            return
        if route == "/api/sessions":
            from urllib.parse import parse_qs, urlparse
            user_id = parse_qs(urlparse(self.path).query).get("user_id", [""])[0].strip()
            conn = db()
            rows = conn.execute("""SELECT session_id, MAX(created_at) AS updated_at,
                COUNT(*) AS message_count,
                COALESCE((SELECT content FROM chat_messages first_message
                    WHERE first_message.user_id=chat_messages.user_id
                      AND first_message.session_id=chat_messages.session_id
                      AND first_message.role='user' ORDER BY first_message.id LIMIT 1), '新对话') AS summary
                FROM chat_messages WHERE user_id=? GROUP BY session_id ORDER BY updated_at DESC""", (user_id,)).fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/messages":
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            user_id, session_id = query.get("user_id", [""])[0], query.get("session_id", [""])[0]
            conn = db()
            rows = conn.execute("SELECT role,content,created_at FROM chat_messages WHERE user_id=? AND session_id=? ORDER BY id", (user_id, session_id)).fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/trace":
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            user_id, session_id = query.get("user_id", [""])[0], query.get("session_id", [""])[0]
            conn = db()
            rows = conn.execute("SELECT node,status,timestamp,result_count,error FROM agent_traces WHERE user_id=? AND session_id=? ORDER BY id", (user_id, session_id)).fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/backend/runs":
            conn = db()
            rows = conn.execute("""SELECT user_id, session_id, MAX(timestamp) AS updated_at,
                COUNT(*) AS event_count, GROUP_CONCAT(DISTINCT node) AS nodes
                FROM agent_traces GROUP BY user_id, session_id
                HAVING SUM(CASE WHEN node = 'aggregate' THEN 1 ELSE 0 END) > 0
                ORDER BY updated_at DESC LIMIT 50""").fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/backend/trace":
            # Admin counterpart of /api/trace: the backend browser has no chat
            # identity cookie, so the admin session authorises the read instead.
            user_id = query.get("user_id", [""])[0].strip()
            session_id = query.get("session_id", [""])[0].strip()
            if not user_id or not session_id:
                self._send(200, b"[]", "application/json; charset=utf-8")
                return
            conn = db()
            rows = conn.execute(
                "SELECT node,status,timestamp,result_count,error FROM agent_traces WHERE user_id=? AND session_id=? ORDER BY id",
                (user_id, session_id),
            ).fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/files":
            RAG_IMPORT_PATH.mkdir(parents=True, exist_ok=True)
            files = [{"name": p.name, "size": p.stat().st_size} for p in sorted(RAG_IMPORT_PATH.iterdir()) if p.is_file()]
            self._send(200, json.dumps(files, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/status":
            index = index_directory()
            ready = index / ".ready"
            indexed = 0
            ready_ok = False
            try:
                indexed = max(0, int(ready.read_text(encoding="ascii").strip()))
                ready_ok = True
            except (OSError, ValueError):
                # A partial/corrupt marker means the index is not ready.
                pass
            payload = {"ready": ready_ok, "indexed_documents": indexed,
                       "source_documents": len(load_documents()),
                       "managed_entries": managed_store.stats()["entries"],
                       "library_ready": True,
                       "search_ready": _RAG_WARMED.is_set()}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/library":
            self._send(200, json.dumps({"documents": managed_store.list_documents(),
                                        "stats": managed_store.stats()},
                                       ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/entries":
            document = query.get("document", [""])[0].strip()
            keyword = query.get("q", [""])[0].strip()
            entries = managed_store.list_entries(document=document or None, query=keyword)
            self._send(200, json.dumps({"entries": entries}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/documents":
            documents = load_documents()
            preview = [{"id": item["id"], "source": item["source"], "document": item["source"],
                        "title": item["title"], "steps": item.get("steps", []),
                        "enabled": True, "preview": item["text"][:240]}
                       for item in documents]
            self._send(200, json.dumps(preview, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/audit/users":
            conn = db()
            rows = conn.execute("""SELECT user_id, COUNT(*) AS access_count,
                MAX(created_at) AS last_seen, COUNT(DISTINCT session_id) AS sessions
                FROM access_logs WHERE user_id <> '' GROUP BY user_id
                ORDER BY last_seen DESC LIMIT 100""").fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/audit/logs":
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            user_id = query.get("user_id", [""])[0].strip()
            conn = db()
            if user_id:
                rows = conn.execute("SELECT user_id,session_id,method,path,created_at FROM access_logs WHERE user_id=? ORDER BY id DESC LIMIT 200", (user_id,)).fetchall()
            else:
                rows = conn.execute("SELECT user_id,session_id,method,path,created_at FROM access_logs WHERE user_id <> '' ORDER BY id DESC LIMIT 200").fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        from urllib.parse import urlparse
        route = urlparse(self.path).path
        if route not in POST_ROUTES:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        # /api/admin/login and /api/admin/logout are intentionally not admin
        # routes: they are how a browser obtains/clears the admin session.
        if _is_admin_route(route) and not self._require_admin(route):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > MAX_REQUEST_BYTES:
                raise ValueError(f"请求体过大，最大允许 {MAX_REQUEST_BYTES // (1024 * 1024)} MB")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            identity_cookie = ""
            if route in {"/api/chat", "/api/review"}:
                identity_cookie = self._bind_request_user(payload) or ""
                if not payload.get("user_id"):
                    raise ValueError("用户标识不能为空")
            if route == "/api/admin/login":
                self._handle_admin_login(payload)
                return
            if route == "/api/admin/logout":
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8",
                           extra_headers={"Set-Cookie": self._cleared_admin_cookie_header()})
                return
            if route == "/api/rag/entries/create":
                result = managed_store.create_entry(
                    document=str(payload.get("document", "")),
                    title=str(payload.get("title", "")),
                    text=str(payload.get("text", "")),
                    steps=payload.get("steps"),
                    enabled=bool(payload.get("enabled", True)),
                )
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            if route == "/api/rag/entries/update":
                update_kwargs: dict = {}
                if "title" in payload:
                    update_kwargs["title"] = payload.get("title")
                if "text" in payload:
                    update_kwargs["text"] = payload.get("text")
                if "steps" in payload:
                    update_kwargs["steps"] = payload.get("steps")
                if "enabled" in payload:
                    update_kwargs["enabled"] = bool(payload.get("enabled"))
                result = managed_store.update_entry(str(payload.get("id", "")), **update_kwargs)
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            if route == "/api/rag/entries/delete":
                result = managed_store.delete_entry(str(payload.get("id", "")))
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            if route == "/api/rag/entries/move":
                result = managed_store.move_entry(str(payload.get("id", "")),
                                                  str(payload.get("target_document", "")))
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            if route == "/api/rag/documents/delete":
                result = managed_store.remove_document(str(payload.get("document", "")))
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            if route == "/api/rag/import":
                import base64
                name = Path(str(payload.get("name", ""))).name
                suffix = Path(name).suffix.lower()
                encoded = str(payload.get("content_base64", ""))
                if not name or suffix not in SUPPORTED:
                    raise ValueError("仅支持 PDF、DOCX、MD、TXT")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    raise ValueError("文件内容不是有效的 Base64") from None
                if not content:
                    raise ValueError("上传文件不能为空")
                if len(content) > MAX_IMPORT_BYTES:
                    raise ValueError("单个文件最大允许 10 MB")
                RAG_IMPORT_PATH.mkdir(parents=True, exist_ok=True)
                target = RAG_IMPORT_PATH / name
                target.write_bytes(content)
                record_access(*request_identity(payload), "POST", route)
                try:
                    imported = managed_store.import_file(target)
                except managed_store.StoreError:
                    # Empty/unparseable uploads must never leave a broken file
                    # (or destroy the previous entries for that filename).
                    target.unlink(missing_ok=True)
                    raise
                result = {"saved": True, "name": name, "document": imported["document"],
                          "chunks": imported["chunks"], "replaced": imported["replaced"],
                          "mode": imported.get("mode", "rule"),
                          "warnings": imported.get("warnings", []),
                          "indexed": imported["indexed"], "stats": imported["stats"]}
                print(f"[ui] RAG import {name}: {imported['chunks']} entries "
                      f"mode={result['mode']} indexed={result['indexed']}", flush=True)
            elif route == "/api/rag/rebuild":
                record_access(*request_identity(payload), "POST", route)
                documents = managed_store.load_documents()
                if not documents:
                    raise ValueError("知识库为空，无法重建索引")
                started = time.perf_counter()
                sync = managed_store.sync_index()
                print(f"[ui] RAG rebuild finished in {time.perf_counter() - started:.2f}s "
                      f"({sync.get('count', 0)} docs, indexed={sync.get('indexed')})", flush=True)
                result = {"count": sync.get("count", len(documents)),
                          "indexed": bool(sync.get("indexed")),
                          "stats": managed_store.stats()}
            elif route == "/api/rag/search":
                query = str(payload.get("query", "")).strip()
                if not query:
                    raise ValueError("检索内容不能为空")
                record_access(*request_identity(payload), "POST", route)
                started = time.perf_counter()
                matches = get_cached_store().search(query, top_k=5)
                elapsed = time.perf_counter() - started
                if elapsed >= 0.3:
                    print(f"[ui] RAG search slow: {elapsed:.2f}s hits={len(matches)} query={query[:40]!r}")
                result = {"query": query, "matches": matches}
            elif route == "/api/chat":
                user_id = str(payload.get("user_id", "")).strip()
                session_id = str(payload.get("session_id", "")).strip() or uuid4().hex
                message = str(payload.get("message", "")).strip()
                if not user_id or not message:
                    raise ValueError("用户标识和消息不能为空")
                record_access(user_id, session_id, "POST", route)
                online_search = bool(payload.get("online_search", False))
                started = time.perf_counter()
                result = chat_reply(user_id, session_id, message, online_search=online_search)
                elapsed = time.perf_counter() - started
                if elapsed >= 2.0:
                    mode = (result.get("intent") or {}).get("reply_mode", "")
                    print(f"[ui] chat reply slow: {elapsed:.2f}s mode={mode} chars={len(message)}")
            elif route == "/api/tickets/update":
                ticket_id = str(payload.get("ticket_id", "")).strip()
                status = str(payload.get("status", "")).strip().lower()
                note = str(payload.get("resolution_note", "")).strip()[:2000]
                if not ticket_id or status not in {"resolved", "unresolved"}:
                    raise ValueError("ticket_id 必填，status 必须为 resolved 或 unresolved")
                conn = db()
                now = datetime.now().isoformat(timespec="seconds")
                existing = conn.execute("SELECT report_json FROM conversations WHERE ticket_id=?", (ticket_id,)).fetchone()
                if existing is None:
                    conn.close()
                    raise ValueError("工单不存在")
                try:
                    report = json.loads(existing["report_json"] or "{}")
                except (TypeError, ValueError):
                    report = {}
                report["management"] = {"status": status, "resolution_note": note, "status_updated_at": now}
                cursor = conn.execute(
                    "UPDATE conversations SET status=?, resolution_note=?, status_updated_at=?, report_json=? WHERE ticket_id=?",
                    (status, note, now, json.dumps(report, ensure_ascii=False), ticket_id),
                )
                row = conn.execute("SELECT * FROM conversations WHERE ticket_id=?", (ticket_id,)).fetchone()
                conn.commit()
                conn.close()
                if cursor.rowcount == 0 or row is None:
                    raise ValueError("工单不存在")
                result = {"ticket": ticket_row(row)}
            else:
                ticket_payload_user = str(payload.get("user_id", "")).strip()
                ticket_payload_session = str(payload.get("session_id", "")).strip()
                record_access(ticket_payload_user, ticket_payload_session, "POST", route)
                result = review_ticket(payload)
            if route in {"/api/chat", "/api/review"}:
                result["user_id"] = payload["user_id"]
            self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8", identity_cookie)
        except managed_store.NotFoundError as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            self._send(404, body, "application/json; charset=utf-8")
        except managed_store.EmptyTextError as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            self._send(400, body, "application/json; charset=utf-8")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            self._send(400, body, "application/json; charset=utf-8")
        except Exception as exc:
            print(f"[ui] POST {route} failed: {exc}", file=sys.stderr, flush=True)
            self._send(500, b'{"error":"server internal error"}', "application/json; charset=utf-8")

    def do_DELETE(self) -> None:
        from urllib.parse import parse_qs, urlparse

        parsed_url = urlparse(self.path)
        route = parsed_url.path
        if route == "/api/rag/files" and not self._require_admin(route):
            return
        record_access("", "", "DELETE", route)

        if route == "/api/rag/files":
            name = parse_qs(parsed_url.query).get("name", [""])[0].strip()
            if not name or Path(name).name != name:
                self._send(400, b'{"error":"invalid file name"}', "application/json; charset=utf-8")
                return
            target = RAG_IMPORT_PATH / name
            if not target.is_file():
                self._send(404, b'{"error":"file not found"}', "application/json; charset=utf-8")
                return
            target.unlink()
            result = managed_store.remove_file(target)
            payload = {"deleted": True, "document": name,
                       "removed": result.get("removed", 0),
                       "stats": result.get("stats", {}),
                       "indexed": bool(result.get("indexed"))}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route != "/api/sessions":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        query = parse_qs(parsed_url.query)
        user_id = query.get("user_id", [""])[0].strip()
        session_id = query.get("session_id", [""])[0].strip()
        current_user = self._cookie_user_id()
        if not current_user:
            self._send(401, json.dumps({"error": "请先通过聊天或提交工单建立会话"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if user_id != current_user:
            self._send(403, json.dumps({"error": "用户身份不匹配"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if not user_id or not session_id:
            self._send(400, b'{"error":"user_id and session_id are required"}', "application/json; charset=utf-8")
            return
        conn = db()
        conn.execute("DELETE FROM chat_messages WHERE user_id=? AND session_id=?", (user_id, session_id))
        conn.execute("DELETE FROM agent_traces WHERE user_id=? AND session_id=?", (user_id, session_id))
        conn.commit()
        conn.close()
        self._send(200, b'{"deleted":true}', "application/json; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        print(f"[ui] {self.address_string()} - {format % args}", flush=True)


def _warm_rag_index() -> None:
    """Load the local BGE model and Chroma collection ahead of the first query.

    The first embedding call costs ~5-6 s (torch + model weights + Chroma).  The
    HTTP server is already accepting connections when this runs, so startup and
    /healthz stay instant while the first real RAG search is fast.
    """
    started = time.perf_counter()
    try:
        store = get_cached_store()
        # A tiny query initialises torch kernels; the result is intentionally unused.
        store.search("RAG 预热", top_k=1)
    except Exception as exc:  # optional dependency/model missing: keep serving
        print(f"[ui] RAG warmup skipped: {exc}", flush=True)
        return
    _RAG_WARMED.set()
    print(f"[ui] RAG warmup ready in {time.perf_counter() - started:.2f}s", flush=True)


def main() -> None:
    default_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    host = os.getenv("HELPDESK_HOST", default_host)
    port = int(os.getenv("PORT", os.getenv("HELPDESK_PORT", "8787")))
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        port = int(sys.argv[1])
    if len(sys.argv) > 2:
        host = sys.argv[2]

    # RAG prewarm policy:
    #   1 / true / sync      -> load BGE/Chroma before binding the socket, so
    #                           the first real query never pays the cold start
    #   background (default) -> load in a daemon thread after binding; startup
    #                           stays fast but a query arriving during warmup waits
    #   0 / off              -> never prewarm (first query pays the cold start)
    prewarm = os.getenv("HELPDESK_RAG_PREWARM", "background").strip().lower()
    background_warmup = False
    if prewarm not in {"0", "off", "false", "no", "none"}:
        if prewarm in {"1", "true", "sync", "yes", "on"}:
            _warm_rag_index()
        else:
            background_warmup = True

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Helpdesk UI running at http://{host}:{port}", flush=True)
    if background_warmup:
        threading.Thread(target=_warm_rag_index, name="rag-warmup", daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
