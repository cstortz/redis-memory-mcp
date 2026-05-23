# Local evaluation notes (dev01)

## Port overrides

Host ports **6379** and **8001** are already in use. `docker-compose.override.yml` maps:

- Redis Stack: `6380` → 6379, `8011` → RedisInsight
- TEI embeddings: `8081` → 80

## Start / stop infrastructure

```bash
cd /home/cstortz/repos/redis-memory-mcp
docker compose up -d redis embeddings
docker compose run --rm redis-init   # only if index missing
docker compose down                  # stop
```

## Cursor MCP

Configured in `~/.cursor/mcp.json` — MCP server runs locally via Python venv (not Docker), talking to Redis on 6380 and TEI on 8081.

After changing MCP config: **Cursor Settings → MCP → reload** (or restart Cursor).

Playbook workflow instructions ship with the server (`server/INSTRUCTIONS.md` → MCP `instructions` on connect). See [PLAYBOOKS.md](PLAYBOOKS.md).

## UIs

- RedisInsight: http://localhost:8011
- TEI health: http://localhost:8081/health

## Try it in chat

Ask the agent to:

1. `mem_save` — "Remember that I prefer TypeScript over JavaScript for new projects" with tag `repos`
2. `mem_search` — "What language does the user prefer?"
3. `kv_set` / `kv_get` — store a named fact like `project:monorepo_mcp:stack` = `python-fastapi`
