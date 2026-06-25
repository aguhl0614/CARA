# CARA — Collegiate Awards & Recognition Assistant

A local AI assistant for the office. Staff ask questions about **orders** (QuickBooks
Online, BigCommerce, with Monday.com pipeline status) and about **machines & software**
(answered from the correct manual). Everything runs on one Mac and lives in this folder.

## Components

| Part | What it is | Where |
|------|------------|-------|
| **LM Studio** | Hosts the LLM (Qwen3.6-27B + MTP) on an OpenAI-compatible API | Host app, port `1234` |
| **Open WebUI** | Chat frontend (accounts, admin) | Docker, http://localhost:3000 |
| **CARA backend** | Data sync, document RAG, and the tools the model calls | Docker, http://localhost:8000 |

The backend keeps a **local cache** of orders/inventory (so we don't flood the SaaS APIs)
and a **vector store** of manuals tagged per machine. The model answers questions by calling
the backend's tools — no live SaaS calls happen at question time.

## First run

1. **LM Studio** (host): download **Qwen3.6-27B** (MLX build incl. MTP head), enable
   *speculative decoding (MTP)*, and start the server on port `1234`.
2. **Config**: `cp .env.example .env` and set a strong `CARA_ADMIN_PASSWORD` and `CARA_SECRET_KEY`.
3. **Start**:
   ```bash
   docker compose up -d --build
   ```
4. **Open WebUI**: open http://localhost:3000, create the first account (becomes admin),
   confirm the Qwen model is listed (Settings → Connections).
5. **CARA admin**: open http://localhost:8000/admin, log in, add your QuickBooks / Monday /
   BigCommerce credentials, define machines, and upload manuals.
6. **Register the tools in Open WebUI**: Admin → Settings → Tools → add tool server
   `http://cara-backend:8000/tools` (container-to-container URL; this spec exposes only
   the read-only tools, never the admin endpoints).

## Everyday commands

```bash
docker compose up -d        # start
docker compose down         # stop (data is preserved under ./data)
docker compose logs -f cara-backend
```

## Layout

- `backend/` — FastAPI app (connectors, sync, rag, tools, admin).
- `data/` — **all** persistent state (bind-mounted): `openwebui/`, `cara/` (SQLite), `chroma/`
  (vectors), `documents/` (manuals/workflows/inventory).
- `MIGRATION.md` — how to move CARA to another machine.

See the full build plan referenced in the project notes for architecture and rationale.
