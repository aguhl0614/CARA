#!/usr/bin/env bash
#
# CARA backup — snapshot all state into ./backups/ as a timestamped tar.gz.
#
# State captured: the ./data folder (SQLite cache, Chroma vectors, documents,
# Open WebUI accounts/chats) + .env. Regenerable model caches are excluded.
#
# Usage:
#   bash backup.sh            # hot backup (stack keeps running; fine for normal use)
#   bash backup.sh --cold     # stop the stack first for a fully consistent snapshot, then restart
#   KEEP=30 bash backup.sh    # keep the newest 30 archives (default 14)
#
# Restore (see MIGRATION.md):
#   docker compose down && tar -xzf backups/cara-data-YYYYMMDD-HHMMSS.tar.gz && docker compose up -d
#
# SECURITY: backups include data/cara/secret.key and .env, which can decrypt your stored
# QuickBooks/Monday/BigCommerce credentials. Keep the backup files somewhere secure.

set -euo pipefail
cd "$(dirname "$0")"

KEEP="${KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/cara-data-${STAMP}.tar.gz"
mkdir -p backups

COLD=0
[ "${1:-}" = "--cold" ] && COLD=1

if [ "$COLD" -eq 1 ]; then
  echo "Stopping stack for a consistent snapshot…"
  docker compose stop
fi

FILES=(data)
[ -f .env ] && FILES+=(.env)

echo "Backing up ${FILES[*]} -> ${OUT}"
tar --exclude='data/cara/hf-cache' \
    --exclude='data/cara/fastembed-cache' \
    --exclude='data/openwebui/cache' \
    -czf "${OUT}" "${FILES[@]}"

if [ "$COLD" -eq 1 ]; then
  echo "Restarting stack…"
  docker compose up -d
fi

# Retention: keep only the newest $KEEP archives.
ls -1t backups/cara-data-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done

echo "Done: ${OUT} ($(ls -lh "${OUT}" | awk '{print $5}'))"
echo "Archives on disk:"
ls -1t backups/cara-data-*.tar.gz | head -n "${KEEP}"
