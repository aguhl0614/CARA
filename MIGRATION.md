# Moving CARA to a new machine

Everything CARA needs lives in this folder **except** LM Studio and the model weights
(LM Studio is a host app; its models live in `~/.lmstudio`). The whole system — code,
config (`.env`), and all state (`./data`) — is portable.

## What moves
- The entire **CARA project folder**, including:
  - `.env` (config + admin password + Fernet key reference)
  - `data/` — SQLite cache, Chroma vectors, uploaded documents, Open WebUI accounts/chats,
    and `data/cara/secret.key` (the key that decrypts your stored API credentials)
- **Not** moved automatically: LM Studio + the model, and any **external inventory file**
  that lives outside the folder (see step 4).

## Steps

1. **Copy the whole folder** (including `data/` and `.env`) to the new Mac.
   - Or restore from a backup: extract a `backups/cara-data-*.tar.gz` into the folder.

2. **Install prerequisites** on the new Mac: **Docker Desktop** and **LM Studio**.

3. **LM Studio**: download **Qwen3.6‑35B‑A3B** (MLX build; MoE) and start the server on port
   **1234**. CARA toggles thinking per request via `reasoning_effort`, so no special setup is needed.

4. **Local hostname.** Update local DNS/mDNS/hosts so `cara.local` points to the new Mac's
   reserved LAN IP. Direct IP access still works through `http://<LAN-IP>:3000`.

5. **Inventory file mount (machine-specific — edit this!).** The inventory file is read
   from a folder *outside* this project, so `docker-compose.yml` bind-mounts that one folder
   read-only. On a new machine the path differs, so:
   - In `docker-compose.yml`, update the inventory bind mount's `source:` **and** `target:`
     to the new machine's folder (keep `target:` = `/hostfs` + the new source path), e.g.
     `source: "/Users/NEWUSER/…/Inventory"` and `target: "/hostfs/Users/NEWUSER/…/Inventory"`.
   - In **Docker Desktop → Settings → Resources → File Sharing**, make sure that folder (or
     `/Users`) is shared.
   - After startup, set the matching full path in the admin UI (**Settings → Inventory file path**).
   - *If you don't use an external inventory file*, delete that bind-mount block and instead
     upload the spreadsheet via the admin **Documents** page (type `inventory`).

6. **Credentials / Fernet key.** Your QuickBooks/Monday/BigCommerce credentials are stored
   **encrypted** in `data/cara/cara.db`, and the key is `data/cara/secret.key` — both move with
   `data/`. Leave **`CARA_FERNET_KEY` blank** in `.env` so CARA uses that moved key, and your
   credentials (including the QBO refresh token) decrypt and keep working. If you change/lose
   the key, just re-enter credentials in the admin UI.

7. **Bring it up**:
   ```bash
   docker compose up -d --build
   ```

8. **Open WebUI**: your accounts and chats moved with `data/openwebui`. Confirm the OpenAI
   connection points to the **CARA proxy** `http://cara-backend:8000/llm/v1` (Settings →
   Connections) — chats route through the backend for thinking/non-thinking handling — and that
   the CARA tool server is registered at `http://cara-backend:8000/tools` (API key =
   `CARA_TOOLS_TOKEN`). The "CARA" model preset's base model must be `qwen/qwen3.6-35b-a3b`.
   Re-apply the CARA system prompt if needed.

## Verify (quick checklist)
- `curl http://cara.local:8000/healthz` or `curl http://<LAN-IP>:8000/healthz` → `{"status":"ok"}`
- Chat works in Open WebUI (http://cara.local:3000 or `http://<LAN-IP>:3000`) with `qwen/qwen3.6-35b-a3b`; a simple order
  question answers quickly (non-thinking) and a how-to question shows a thinking block
- An order question (e.g. "status of estimate 22736") returns QBO + Monday data
- A machine question returns the right manual
- `check_inventory` returns stock counts

## Backups

```bash
bash backup.sh           # hot snapshot of ./data + .env -> backups/cara-data-<timestamp>.tar.gz
bash backup.sh --cold    # stop the stack first for a fully consistent snapshot, then restart
KEEP=30 bash backup.sh   # change retention (default: keep newest 14)
```

**Restore:**
```bash
docker compose down
tar -xzf backups/cara-data-YYYYMMDD-HHMMSS.tar.gz   # run from the project folder
docker compose up -d
```

**Schedule** (optional) — e.g. a nightly cron entry on the host:
```
15 2 * * *  cd "/path/to/CARA" && /bin/bash backup.sh --cold >> data/cara/backup.log 2>&1
```

> Backups contain `secret.key` and `.env` (which can decrypt your API credentials). Store them securely.

## Everyday commands
```bash
docker compose up -d        # start
docker compose down         # stop (data preserved under ./data)
docker compose logs -f cara-backend
```
