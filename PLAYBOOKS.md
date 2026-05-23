# MCP Workflow Playbooks

Generic workflow cache for **any MCP server**. Avoid rediscovering tool sequences on every run.

## Tools

| Tool | Use |
|------|-----|
| `playbook_resolve` | **Start here** — match user prompt to cached workflow |
| `playbook_get` | Exact lookup by `task_id` slug |
| `playbook_search` | Semantic search when slug unknown |
| `playbook_save` | Store workflow after first successful run |
| `playbook_list` | Browse saved playbooks |
| `playbook_delete` | Remove outdated playbook |

## Playbook JSON schema

```json
{
  "task_id": "my-workflow-slug",
  "version": 1,
  "description": "What this workflow accomplishes",
  "triggers": ["example user phrase 1", "example phrase 2"],
  "mcp_servers": ["postgres-mcp", "filesystem-mcp"],
  "variables": ["company_name", "record_id"],
  "steps": [
    {
      "order": 1,
      "name": "step_slug",
      "mcp_server": "postgres-mcp",
      "tool": "execute_prepared_select",
      "args": { "sql": "SELECT id FROM schema.table WHERE active = true LIMIT 1" },
      "notes": "Optional agent hints"
    }
  ]
}
```

- **mcp_server** / **tool**: slugs as configured in Cursor MCP (no domain-specific logic in the server).
- **args**: opaque JSON passed to the agent — SQL, paths, HTTP bodies, etc.
- **variables**: names the agent fills at runtime; not stored in the cached procedure.

## Example save (any MCP stack)

```
playbook_save(
  task_id="list-schemas-and-tables",
  description="List postgres schemas then tables in a chosen schema",
  mcp_servers="postgres-mcp",
  triggers="show database schema,list all tables",
  variables="schema_name",
  steps='[
    {"order":1,"name":"list_schemas","mcp_server":"postgres-mcp","tool":"list_schemas","args":{}},
    {"order":2,"name":"list_tables","mcp_server":"postgres-mcp","tool":"list_tables","args":{"schema":"$schema_name"}}
  ]'
)
```

## Storage

- Full JSON: `kv:playbook:{task_id}` (permanent until deleted)
- Semantic index: `mem_*` entry tagged `playbook` (+ optional server tags)

## Agent instructions (MCP protocol)

Workflow behavior is defined in [`server/INSTRUCTIONS.md`](server/INSTRUCTIONS.md) and sent to the
client on MCP connect via the standard `instructions` field. Any MCP client receives this —
no Cursor-specific rule file is required.
