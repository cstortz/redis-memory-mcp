#!/bin/bash
# redis-memory-mcp — self-installing start script
# All setup output goes to stderr; only MCP server uses stdout (JSON-RPC)
set -e

REPO_URL="https://raw.githubusercontent.com/sergesha/redis-memory-mcp/main"
WORK_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/redis-memory-mcp"

log() { echo "🧠 redis-memory-mcp: $*" >&2; }

# ── 1. Download docker-compose.yaml if missing or stale (1 day) ───────────────
mkdir -p "$WORK_DIR"
COMPOSE_FILE="$WORK_DIR/docker-compose.yaml"
if [ ! -f "$COMPOSE_FILE" ] || [ "$(find "$COMPOSE_FILE" -mtime +1 2>/dev/null)" ]; then
  log "Downloading docker-compose.yaml..."
  curl -fsSL "$REPO_URL/docker-compose.yaml" -o "$COMPOSE_FILE"
fi

# ── 2. Download server/ source if missing ─────────────────────────────────────
SERVER_DIR="$WORK_DIR/server"
if [ ! -d "$SERVER_DIR" ]; then
  log "Downloading server source..."
  mkdir -p "$SERVER_DIR"
  for f in memory_mcp.py Dockerfile pyproject.toml INSTRUCTIONS.md tcp_server.py; do
    curl -fsSL "$REPO_URL/server/$f" -o "$SERVER_DIR/$f"
  done
fi

# ── 3. Start Redis + TEI (idempotent) ─────────────────────────────────────────
log "Starting infrastructure..."
docker compose -f "$COMPOSE_FILE" up -d redis embeddings redis-init >/dev/null 2>&1

# ── 4. Wait for Redis ─────────────────────────────────────────────────────────
log "Waiting for Redis..."
until docker exec redis-stack redis-cli ping >/dev/null 2>&1; do sleep 1; done

# ── 5. Build MCP server image if not present ──────────────────────────────────
if ! docker image inspect redis-memory-mcp >/dev/null 2>&1; then
  log "Building redis-memory-mcp image..."
  docker build -t redis-memory-mcp "$SERVER_DIR" >&2
fi

log "Ready."

# ── 6. Launch MCP server — only this writes to stdout ─────────────────────────
exec docker run --rm -i \
  -e "REDIS_URL=${REDIS_URL:-redis://host.docker.internal:6379/0}" \
  -e "EMBED_URL=${EMBED_URL:-http://host.docker.internal:8081}" \
  -e "INDEX_NAME=${INDEX_NAME:-idx:memories}" \
  redis-memory-mcp
