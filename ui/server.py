from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_graph import run_agent_graph
from src.agents.solution_retrieval import retrieve_solutions
from src.intent import detect_intent
from src.llm_client import LLMClient, load_config
from src.rag.ingest import SUPPORTED
from src.rag.vector_store import get_cached_store, load_documents
from src.ticket_parser import parse_ticket
from src.web_search import search_web

INDEX = Path(__file__).with_name("index.html")
BACKEND_INDEX = Path(__file__).with_name("backend.html")
DB_PATH = Path(__file__).with_name("helpdesk.db")
SKILLS_PATH = Path(__file__).with_name("skills")
RAG_IMPORT_PATH = ROOT / "data" / "knowledge" / "imports"
MAX_REQUEST_BYTES = 25 * 1024 * 1024
BACKEND_ONLY = os.getenv("HELPDESK_UI_MODE", "").lower() == "backend"
_DB_SCHEMA_LOCK = threading.Lock()


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
    conn = db()
    conn.execute(
        "INSERT INTO access_logs(user_id,session_id,method,path,created_at) VALUES(?,?,?,?,?)",
        (user_id, session_id, method, path, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


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
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(self.path)
        route = parsed_url.path
        query = parse_qs(parsed_url.query)
        record_access(query.get("user_id", [""])[0].strip(), query.get("session_id", [""])[0].strip(), "GET", route)
        if route == "/healthz":
            self._send(200, b'{"status":"ok"}', "application/json; charset=utf-8")
            return
        if route in ("/", "/index.html") and BACKEND_ONLY:
            self._send(200, BACKEND_INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if route in ("/", "/index.html"):
            html = INDEX.read_text(encoding="utf-8")
            html = html.replace('<div class="skills"><div class="label">SKILLS</div><div id="skillList"></div><button class="import" id="importSkill">导入 Skill</button></div>', '', 1)
            toolbar = '<div class="online-tools"><label><input type="checkbox" id="onlineSearch"> 在线搜索</label><span id="searchHint">关闭：仅使用对话上下文和本地知识库</span></div>'
            html = html.replace('<div class="compose"><textarea', toolbar + '<div class="compose"><textarea', 1)
            html = html.replace('const $=id=>document.getElementById(id),labels={', 'const onlineSearch=document.getElementById("onlineSearch");onlineSearch?.addEventListener("change",()=>{document.getElementById("searchHint").textContent=onlineSearch.checked?"开启：将参考公开网页并标注为网络信息":"关闭：仅使用对话上下文和本地知识库"});const $=id=>document.getElementById(id),labels={web_search:"在线搜索",', 1)
            html = html.replace('session_id:sessionId,message:text})', 'session_id:sessionId,message:text,online_search:Boolean(onlineSearch?.checked)})', 1)
            html = html.replace('</style>', '.online-tools{max-width:720px;margin:0 auto 8px;padding:7px 10px;color:#71808a;font-size:11px;display:flex;gap:10px;align-items:center;border:1px solid #dbe3e7;border-radius:7px;background:#f7fafb}.online-tools label{color:#1268a5;font-weight:700}.online-tools input{accent-color:#1268a5}.online-tools strong{font-weight:600}\n</style>', 1)
            ticket_modal = '''<div id="ticketModal" class="ticket-modal"><div class="ticket-card"><h2>提交工单</h2><input id="ticketTitle" placeholder="标题"><input id="ticketRequester" placeholder="申请人"><textarea id="ticketDescription" placeholder="请描述现象、报错、已尝试操作和影响范围"></textarea><select id="ticketChannel"><option value="chat">聊天</option><option value="portal">门户</option><option value="email">邮件</option><option value="phone">电话</option></select><input id="ticketContact" class="ticket-contact" placeholder="联系方式（手机号 / 邮箱 / 分机号）"><div><button onclick="closeTicketModal()">取消</button><button class="ticket-primary" onclick="submitTicket()">提交</button></div><p id="ticketMessage"></p></div></div>'''
            html = html.replace('</body>', ticket_modal + '''<script>function openTicketModal(){document.getElementById("ticketModal").classList.add("open");document.getElementById("ticketRequester").value=localStorage.getItem("helpdesk_user")||""}function closeTicketModal(){document.getElementById("ticketModal").classList.remove("open")}async function submitTicket(){const title=document.getElementById("ticketTitle").value.trim(),requester=document.getElementById("ticketRequester").value.trim(),description=document.getElementById("ticketDescription").value.trim(),channel=document.getElementById("ticketChannel").value,contact=document.getElementById("ticketContact").value.trim(),msg=document.getElementById("ticketMessage");if(!title||!requester||!description){msg.textContent="请填写标题、申请人和描述";return}msg.textContent="提交中…";try{const response=await fetch("/api/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:localStorage.getItem("helpdesk_user")||requester,session_id:sessionId,ticket:{title,requester,description,channel,contact}})});const data=await response.json();if(!response.ok){msg.textContent=data.error||"提交失败";return}msg.textContent="工单已提交："+(data.ticket?.ticket_id||data.ticket_id||"已生成");setTimeout(closeTicketModal,1200)}catch(error){msg.textContent="提交失败："+error.message}}</script></body>''', 1)
            # The UI is intentionally compacted into one line, so replace the
            # stable interpolation rather than the surrounding function text.
            html = html.replace(
                '${esc(text)}</div>`;',
                '${esc(text)}${role===\'assistant\'&&text.includes(\'工单提交流程\')?\'<button class="ticket-action" onclick="openTicketModal()">提交工单</button>\':\'\'}</div>`;',
                1,
            )
            html = html.replace('</style>', '.ticket-action{display:block;margin-top:10px;border:0;border-radius:6px;padding:8px 12px;background:#1268a5;color:#fff;cursor:pointer}.ticket-modal{position:fixed;inset:0;background:#0e1b24aa;display:none;place-items:center;z-index:10}.ticket-modal.open{display:grid}.ticket-card{width:min(520px,92%);background:#fff;border-radius:9px;padding:20px}.ticket-card input,.ticket-card textarea,.ticket-card select{width:100%;margin:6px 0 10px;padding:10px;border:1px solid #dbe3e7;border-radius:6px;font:inherit}.ticket-card .ticket-contact{background:#f1f3f5;color:#596771;border-color:#d8dee2}.ticket-card .ticket-contact::placeholder{color:#87939b}.ticket-card textarea{min-height:130px}.ticket-card button{padding:9px 14px;border:0;border-radius:6px;margin-right:8px;cursor:pointer}.ticket-primary{background:#1268a5;color:#fff}.ticket-card p{font-size:12px;color:#71808a}\n</style>', 1)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route in ("/backend", "/backend.html", "/admin", "/admin.html"):
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
        if route == "/api/rag/files":
            RAG_IMPORT_PATH.mkdir(parents=True, exist_ok=True)
            files = [{"name": p.name, "size": p.stat().st_size} for p in sorted(RAG_IMPORT_PATH.iterdir()) if p.is_file()]
            self._send(200, json.dumps(files, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/status":
            index = Path(load_config()["rag"]["index_dir"])
            index = index if index.is_absolute() else ROOT / index
            ready = index / ".ready"
            indexed = int(ready.read_text(encoding="ascii").strip()) if ready.exists() else 0
            payload = {"ready": ready.exists(), "indexed_documents": indexed,
                       "source_documents": len(load_documents())}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if route == "/api/rag/documents":
            documents = load_documents()
            preview = [{"source": item["source"], "title": item["title"],
                        "steps": item.get("steps", []), "preview": item["text"][:240]}
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
        record_access("", "", "POST", route)
        if route not in ("/api/review", "/api/chat", "/api/rag/import", "/api/rag/rebuild", "/api/rag/search", "/api/tickets/update"):
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > MAX_REQUEST_BYTES:
                raise ValueError(f"请求体过大，最大允许 {MAX_REQUEST_BYTES // (1024 * 1024)} MB")
            payload = json.loads(self.rfile.read(size))
            if route == "/api/rag/import":
                import base64
                name = Path(str(payload.get("name", ""))).name
                suffix = Path(name).suffix.lower()
                if suffix not in SUPPORTED:
                    raise ValueError("仅支持 PDF、DOCX、MD、TXT")
                RAG_IMPORT_PATH.mkdir(parents=True, exist_ok=True)
                (RAG_IMPORT_PATH / name).write_bytes(base64.b64decode(payload.get("content_base64", "")))
                record_access(*request_identity(payload), "POST", route)
                result = {"saved": True, "name": name}
            elif route == "/api/rag/rebuild":
                record_access(*request_identity(payload), "POST", route)
                store = get_cached_store()
                result = {"count": store.rebuild(load_documents())}
            elif route == "/api/rag/search":
                query = str(payload.get("query", "")).strip()
                if not query:
                    raise ValueError("检索内容不能为空")
                record_access(*request_identity(payload), "POST", route)
                result = {"query": query, "matches": get_cached_store().search(query, top_k=5)}
            elif route == "/api/chat":
                user_id = str(payload.get("user_id", "")).strip()
                session_id = str(payload.get("session_id", "")).strip() or uuid4().hex
                message = str(payload.get("message", "")).strip()
                if not user_id or not message:
                    raise ValueError("用户标识和消息不能为空")
                record_access(user_id, session_id, "POST", route)
                online_search = bool(payload.get("online_search", False))
                result = chat_reply(user_id, session_id, message, online_search=online_search)
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
            self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except Exception as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            self._send(400, body, "application/json; charset=utf-8")

    def do_DELETE(self) -> None:
        from urllib.parse import parse_qs, urlparse

        parsed_url = urlparse(self.path)
        route = parsed_url.path
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
            self._send(200, b'{"deleted":true}', "application/json; charset=utf-8")
            return
        if route != "/api/sessions":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        query = parse_qs(parsed_url.query)
        user_id = query.get("user_id", [""])[0].strip()
        session_id = query.get("session_id", [""])[0].strip()
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
        print(f"[ui] {self.address_string()} - {format % args}")


def main() -> None:
    default_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    host = os.getenv("HELPDESK_HOST", default_host)
    port = int(os.getenv("PORT", os.getenv("HELPDESK_PORT", "8787")))
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        port = int(sys.argv[1])
    if len(sys.argv) > 2:
        host = sys.argv[2]
    if os.getenv("HELPDESK_RAG_PREWARM", "1") == "1":
        try:
            get_cached_store()
        except Exception as exc:
            print(f"RAG prewarm skipped: {exc}")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Helpdesk UI running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
