# redis-memory-mcp

> Persistent cross-session memory for AI agents — semantic search + KV store with auto-expiry

Long-term self-managing memory for LLM agents (Cursor, Claude Code, etc.) via [MCP](https://modelcontextprotocol.io).

## Features

- **Semantic search** (`mem_*`) — save facts with vector embeddings, find by meaning
- **Key-value store** (`kv_*`) — instant O(1) lookup for named facts
- **MCP playbooks** (`playbook_*`) — cache multi-step MCP workflows for any server (see [PLAYBOOKS.md](PLAYBOOKS.md))
- **Auto-expiry** — TTL resets on every read; unused facts expire, popular ones live forever
- **Multi-project** — tag-based isolation between projects
- **Self-contained** — Docker Compose locally, Kubernetes + GitHub Actions for production (see [k8s/README.md](k8s/README.md))

## Quick Start (local)

```bash
git clone https://github.com/sergesha/redis-memory-mcp
cd redis-memory-mcp
docker compose up -d redis embeddings
python3 -m venv .venv
.venv/bin/pip install "mcp[cli]>=1.0.0" "redis>=5.0.0" "httpx>=0.27.0" "numpy>=1.26.0"
```

### Cursor — local dev (`~/.cursor/mcp.json`)

```json
"redis-memory-mcp": {
  "command": "/path/to/redis-memory-mcp/.venv/bin/python",
  "args": ["/path/to/redis-memory-mcp/server/memory_mcp.py"],
  "env": {
    "REDIS_URL": "redis://127.0.0.1:6380/0",
    "EMBED_URL": "http://127.0.0.1:8081",
    "INDEX_NAME": "idx:memories"
  }
}
```

### Cursor — Kubernetes (TCP, same as other MCP servers)

```json
"redis-memory-mcp": {
  "command": "socat",
  "args": ["TCP:monorepo-mcp.dev.stortz.tech:3006", "STDIO"]
}
```

Deploy via GitHub Actions **Deploy** workflow or `./scripts/deploy.sh deploy`. Details: [k8s/README.md](k8s/README.md).

### Claude Code

Works automatically via `.mcp.json` in the repo root when using as a Claude plugin.

## Tools (14 total)

### MCP workflow playbooks — any server

| Tool | Description |
|------|-------------|
| `playbook_resolve(user_request, mcp_server?, min_similarity?)` | Match prompt → cached workflow (start here) |
| `playbook_get(task_id)` | Load playbook JSON by slug |
| `playbook_search(query, mcp_server?, top_k?)` | Semantic playbook search |
| `playbook_save(task_id, description, steps, ...)` | Save workflow after first success |
| `playbook_list(mcp_server?)` | List saved playbooks |
| `playbook_delete(task_id)` | Remove playbook |

See [PLAYBOOKS.md](PLAYBOOKS.md) for the JSON step schema.

### Key-Value Storage — instant lookup

| Tool | Description |
|------|-------------|
| `kv_set(key, value, tags?, ttl_days?)` | Store a named fact |
| `kv_get(key)` | Retrieve by exact key (refreshes TTL) |
| `kv_delete(key)` | Delete by key |
| `kv_list(tag?, pattern?)` | List entries with filtering |

### Semantic Memory — vector search

| Tool | Description |
|------|-------------|
| `mem_save(text, code?, tags?, ttl_days?)` | Save fact with embedding |
| `mem_search(query, tags?, top_k?)` | Find by meaning (refreshes TTL on hits) |
| `mem_list(limit?, tag?)` | Browse by recency |
| `mem_delete(memory_id)` | Delete by ID |

## TTL & Auto-Expiry

| TTL | Use case |
|-----|----------|
| `ttl_days=90` (default) | Normal facts — expire if unused for 90 days |
| `ttl_days=0` | Permanent — API keys, critical config |
| `ttl_days=7` | Short-lived context |

- TTL **resets on every read** — frequently accessed facts never expire
- Redis `volatile-lru` evicts least-recently-used facts under memory pressure
- Only facts with TTL can be evicted; permanent facts (`ttl_days=0`) are safe

## Architecture

```
┌─────────────────┐     ┌────────────────────┐     ┌───────────────────┐
│  Cursor / Claude │────▶│  redis-memory-mcp  │────▶│   Redis Stack     │
│  (MCP client)    │ MCP │  (Python, stdio)   │     │   + RediSearch    │
└─────────────────┘     └────────┬───────────┘     │   + HNSW index    │
                                 │                  └───────────────────┘
                                 ▼
                        ┌────────────────────┐
                        │  HuggingFace TEI   │
                        │  (embeddings, CPU) │
                        └────────────────────┘
```

- **Redis Stack** — RediSearch module with HNSW vector index (768 dim, cosine)
- **TEI** — `paraphrase-multilingual-mpnet-base-v2` (multilingual, runs on CPU)
- **MCP server** — Python FastMCP over stdio; sends agent instructions from `server/INSTRUCTIONS.md` on connect

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `EMBED_URL` | `http://localhost:8081` | TEI embeddings endpoint |
| `INDEX_NAME` | `idx:memories` | Redis search index name |
| `DEFAULT_TTL` | `7776000` (90 days) | Default TTL in seconds |

## Redis UI

RedisInsight is included at **http://localhost:8001** — browse keys, run queries, analyze memory usage.

## Plugin Structure

```
redis-memory-mcp/
├── .claude-plugin/marketplace.json   # Marketplace registry
├── .claude/settings.json             # Auto-load config
├── redis-memory-mcp/                 # Claude plugin
│   ├── .claude-plugin/
│   │   ├── plugin.json               # Plugin metadata
│   │   └── mcp.json                  # MCP server docs
│   ├── .mcp.json                     # Runtime MCP config
│   ├── hooks/project-init.json       # Session start hook
│   └── skills/persistent-memory/
│       └── SKILL.md                  # Memory management skill
├── server/                           # MCP server source
│   ├── memory_mcp.py
│   ├── Dockerfile
│   └── pyproject.toml
├── docker-compose.yaml               # Full stack
└── README.md
```

## License

MIT
