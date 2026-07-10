#!/usr/bin/env bash
# Bring up Sonar's SearXNG: generate instance/settings.yml with a fresh secret_key
# on first run, start the container, and smoke-test the JSON API that web.search
# depends on. Idempotent — safe to re-run (settings are only generated once).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PORT="${SONAR_SEARXNG_PORT:-8888}"
SETTINGS="instance/settings.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker not found on PATH." >&2
  exit 1
fi

mkdir -p instance
if [ ! -f "$SETTINGS" ]; then
  echo "[searxng] first run — generating $SETTINGS with a fresh secret_key"
  if command -v openssl >/dev/null 2>&1; then
    secret="$(openssl rand -hex 32)"
  else
    secret="$(head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  # Replace ONLY the placeholder; sed with a | delimiter avoids hex-slash issues.
  sed "s|ultrasecretkey|${secret}|" settings.template.yml > "$SETTINGS"
fi

echo "[searxng] docker compose up -d ..."
docker compose up -d

url="http://127.0.0.1:${PORT}/search?q=ping&format=json"
echo "[searxng] waiting for JSON API at :${PORT} ..."
for i in $(seq 1 40); do
  if curl -fsS "$url" >/dev/null 2>&1; then
    echo "[searxng] ready — JSON API up at http://127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
done

echo "[searxng] WARN: JSON API not responding after 40s." >&2
echo "         Check: (cd infra/searxng && docker compose logs --tail=40)" >&2
exit 1
