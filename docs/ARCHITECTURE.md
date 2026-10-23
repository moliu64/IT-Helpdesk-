# Architecture

## Overview

The application turns an unstructured IT support ticket into a normalized record, runs independent triage agents, and produces human-readable and machine-readable reports. The default runtime is pure Python, so DeepSeek Harness / LangGraph are optional; when LangGraph is installed `src/agent_graph.py` uses a real `StateGraph`, otherwise it falls back to a `ThreadPoolExecutor`.

The same process also hosts three web surfaces:

| Surface | Path | Purpose |
| --- | --- | --- |
| User chat | `/` | Conversation, online search, ticket submission, session history |
| Ops backend | `/backend` | Ticket management, run/trace monitor, RAG entry point, audit log |
| RAG knowledge base | `/rag` | Per-document knowledge entries with CRUD, move, enable/disable, upload, rebuild |

## Components

| Component | Responsibility |
| --- | --- |
| `src/ticket_parser.py` | Parse text or JSON into the ticket contract |
| `src/agents/classify.py` | Choose category and subcategory |
| `src/agents/priority.py` | Estimate P1-P4 priority and SLA |
| `src/agents/solution_retrieval.py` | Retrieve grounded KB and history matches |
| `src/agents/routing.py` | Recommend a configured support team |
| `src/report.py` | Reconcile results and render reports |
| `src/agent_graph.py` | Optional LangGraph orchestration + Python fallback, emits trace events |
| `src/intent.py` | Rule-first conversation intent detection with LLM fallback |
| `src/rag/vector_store.py` | Build and query the local Chroma index; index dir/env overrides |
| `src/rag/managed_store.py` | Persistent knowledge-entry store (seed, CRUD, move, import, reindex) |
| `src/rag/ingest.py` | PDF/DOCX/Markdown/TXT extraction and overlapping chunking |
| `ui/server.py` | Single HTTP process: static pages, JSON APIs, admin auth, audit log |
| `ui/index.html` / `backend.html` / `rag.html` / `login.html` | Self-contained front-end pages (no external assets) |

## Request Flow

### Ticket triage (CLI or `POST /api/review`)

1. `parse_ticket` normalizes a text file or JSON object.
2. The runner executes classification, priority assessment, and RAG retrieval concurrently.
3. Routing uses the classification result to select a configured team.
4. `build_report` applies SLA lookup and cross-agent consistency checks (category/route conflicts, P1 downgrade).
5. Reports are written below `outputs/<ticket_id>/`, and the ticket/trace are persisted to SQLite.

### Chat (`POST /api/chat`)

1. `detect_intent` classifies the message (fast rules first, LLM fallback).
2. Greeting / general messages go to the LLM conversation path.
3. Troubleshooting messages run local RAG retrieval; grounded answers may be summarized by the LLM, with a deterministic steps fallback when the model/API is unavailable.
4. Optional DuckDuckGo instant-answer results are included only when the user enables online search.
5. Trace events are persisted and returned for the UI's collapsible run trace.

### RAG knowledge management (`/rag`)

1. `managed_store` seeds itself from existing KB articles, historical tickets, and imported files on first use.
2. The `/rag` page reads documents/entries and sends CRUD/move/upload requests to `/api/rag/*`.
3. Every mutation persists atomically to `data/knowledge/managed_entries.json` and rebuilds the Chroma index from the enabled entries.
4. Uploads are parsed into chunks, replace same-name documents, and are indexed immediately; empty/scanned files return a friendly 400 instead of creating empty entries.

## Data and Configuration

- Curated synthetic KB articles live in `data/knowledge/`.
- Synthetic historical tickets live in `data/tickets/historical_tickets.json`.
- Managed knowledge entries live in `data/knowledge/managed_entries.json` (ignored by Git, seeded automatically).
- `scripts/build_index.py` generates the ignored `data/tickets/index/` Chroma directory.
- Runtime settings are in `config/config.yaml`; secrets are environment variables only.
- Test isolation: `HELPDESK_RAG_STORE`, `HELPDESK_RAG_INDEX_DIR`, `HELPDESK_RAG_SOURCE_ROOT`, and `HELPDESK_DB_PATH` can point a test server at temporary data.

## Extension Points

Agents communicate through small JSON contracts and can be replaced independently. The optional `workflow/helpdesk.workflow.js` mirrors the Python flow using DeepSeek Harness orchestration.
