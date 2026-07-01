# CARA — Collegiate Awards & Recognition Assistant

A local AI assistant for the office. Staff ask questions about **orders** (QuickBooks
Online, BigCommerce, with Monday.com pipeline status) and about **machines & software**
(answered from the correct manual). Everything runs on one Mac and lives in this folder.

## Components

| Part | What it is | Where |
|------|------------|-------|
| **LM Studio** | Hosts the LLM (**Qwen3.6-35B-A3B**, MoE) on an OpenAI-compatible API | Host app, port `1234` |
| **Open WebUI** | Chat frontend (accounts, admin) | Docker, http://cara.local:3000 or `http://<LAN-IP>:3000` |
| **CARA backend** | Data sync, document RAG, the tools the model calls, and the **LLM proxy** (auto thinking/non-thinking routing + per-mode sampling) | Docker, http://cara.local:8000 or `http://<LAN-IP>:8000` |

The backend keeps a **local cache** of orders/inventory (so we don't flood the SaaS APIs)
and a **vector store** of manuals tagged per machine. The model answers questions by calling
the backend's tools — no live SaaS calls happen at question time.

**Thinking vs non-thinking.** Open WebUI sends chats to the backend's **LLM proxy** (`/llm/v1`),
which classifies each message: simple order/inventory questions are answered in a fast
**non-thinking** mode, while how-to / machine / maintenance questions use the model's **thinking**
mode. The toggle is the model's `reasoning_effort`, and the per-mode sampling parameters
(temperature, top_p, top_k, presence/repetition penalty) are tunable in the CARA admin panel
(**LLM sampling parameters**). Because chat now flows through the backend, the backend must be running.

## First run

1. **LM Studio** (host): download **Qwen3.6-35B-A3B** (MLX build; MoE) and start the server on
   port `1234`. CARA toggles thinking per request via `reasoning_effort`, so just serve the model.
2. **Config**: `cp .env.example .env`, set a strong `CARA_ADMIN_PASSWORD` and
   `CARA_SECRET_KEY`, and make sure local DNS/mDNS/hosts resolves `cara.local` to this Mac's
   reserved LAN IP.
3. **Start**:
   ```bash
   docker compose up -d --build
   ```
4. **Open WebUI**: open http://cara.local:3000 or `http://<LAN-IP>:3000`, create the first account
   (becomes admin). The
   OpenAI connection should point at the CARA proxy `http://cara-backend:8000/llm/v1` (Settings →
   Connections) — **not** LM Studio directly — so chats get thinking/non-thinking routing; confirm
   `qwen/qwen3.6-35b-a3b` is listed.
5. **CARA admin**: open http://cara.local:8000/admin or `http://<LAN-IP>:8000/admin`, log in, add your QuickBooks / Monday /
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
