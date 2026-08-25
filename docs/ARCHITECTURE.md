# Architecture

## Overview

The application turns an unstructured IT support ticket into a normalized record, runs independent triage agents, and produces human-readable and machine-readable reports. The default runtime is pure Python, so DeepSeek Harness is optional.

## Components

| Component | Responsibility |
| --- | --- |
| `src/ticket_parser.py` | Parse text or JSON into the ticket contract |
| `src/agents/classify.py` | Choose category and subcategory |
| `src/agents/priority.py` | Estimate P1-P4 priority and SLA |
| `src/agents/solution_retrieval.py` | Retrieve grounded KB and history matches |
| `src/agents/routing.py` | Recommend a configured support team |
| `src/report.py` | Reconcile results and render reports |
| `src/rag/vector_store.py` | Build and query the local Chroma index |
| `ui/server.py` | Provide the local UI and conversation history |

## Request Flow

1. `parse_ticket` normalizes a text file or JSON object.
2. The CLI runs classification, priority assessment, and RAG retrieval concurrently.
3. Routing uses the classification result to select a configured team.
4. `build_report` applies SLA lookup and cross-agent consistency checks.
5. Reports are written below `outputs/<ticket_id>/`.

## Data and Configuration

- Curated synthetic KB articles live in `data/knowledge/`.
- Synthetic historical tickets live in `data/tickets/historical_tickets.json`.
- `scripts/build_index.py` generates the ignored `data/tickets/index/` directory.
- Runtime settings are in `config/config.yaml`; secrets are environment variables only.

## Extension Points

Agents communicate through small JSON contracts and can be replaced independently. The optional `workflow/helpdesk.workflow.js` mirrors the Python flow using DeepSeek Harness orchestration.
