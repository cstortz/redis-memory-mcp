# Redis Memory MCP — Agent Instructions

This server provides persistent memory and **MCP workflow playbooks** for any MCP client.
Playbooks cache multi-step procedures so agents skip rediscovering tool sequences on repeat tasks.

## Before any multi-step MCP task

Applies when the user request involves **multiple MCP tool calls**, schema/API discovery,
or a workflow you have run before — for **any** MCP server (postgres, filesystem, REST, custom).

1. Call **`playbook_resolve(user_request=<exact user prompt>, mcp_server=<slug if obvious>)`**.
2. If a playbook is returned, execute its `steps` in order. Substitute **variables** only
   (runtime inputs like ids, names, dates). Do not rediscover schemas or tool names.
3. If no playbook matches: use other MCP servers to discover and complete the task, then
   call **`playbook_save()`** so the next run is cached.

## After the first successful run

Call **`playbook_save()`** with:

| Field | Content |
|-------|---------|
| `task_id` | Stable lowercase slug, e.g. `export-active-config` |
| `description` | One-line summary |
| `steps` | JSON array (see schema below) |
| `mcp_servers` | Comma-separated slugs used, e.g. `postgres-mcp,filesystem-mcp` |
| `triggers` | Example user phrases that should match this workflow later |
| `variables` | Runtime parameter names **not** stored in the cache |
| `version` | Increment when tools, args, or APIs change |

## Step schema (generic — any MCP server)

```json
{
  "order": 1,
  "name": "step_slug",
  "mcp_server": "postgres-mcp",
  "tool": "execute_prepared_select",
  "args": { "sql": "SELECT ...", "parameters": {} },
  "notes": "Optional hints for the agent"
}
```

- **`mcp_server`** / **`tool`**: slugs as configured in the client's MCP settings.
- **`args`**: opaque JSON (SQL, paths, HTTP bodies, etc.) — no domain logic in this server.

## Tool reference

| Tool | When to use |
|------|-------------|
| `playbook_resolve` | **Start here** — match user prompt to cached workflow |
| `playbook_get` | Exact lookup by `task_id` |
| `playbook_search` | Fuzzy search when slug unknown |
| `playbook_save` | Store workflow after first success |
| `playbook_list` | Browse saved playbooks |
| `playbook_delete` | Remove outdated playbooks |
| `mem_*` / `kv_*` | Facts and key-value memory (separate from playbooks) |

## Rules

- Do not skip playbook lookup because a task "seems simple".
- Do not cache secrets, tokens, or PII — use `variables` and fetch live data.
- Avoid tag names that break RediSearch (e.g. do not use `cursor` as a tag).

Full schema and examples: `PLAYBOOKS.md` in the repo root.
