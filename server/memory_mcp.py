"""
Redis Memory MCP — Server
Tool sets:
  kv_*        — simple key/value store (instant, no embeddings)
  mem_*       — semantic memory (vector search via TEI + Redis HNSW)
  playbook_*  — cached MCP workflow runbooks (any server, any task)

TTL strategy (volatile-lru):
  - Every key has a TTL (default 90 days)
  - TTL is refreshed on every read → popular facts never expire
  - Unused facts expire after TTL → Redis evicts them under memory pressure
"""

import json, re, struct, time, uuid, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as aio_redis
from mcp.server.fastmcp import FastMCP

REDIS_URL    = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
EMBED_URL    = os.getenv("EMBED_URL",    "http://localhost:8081")
INDEX        = os.getenv("INDEX_NAME",   "idx:memories")
MEM_PREFIX       = "mem:"
KV_PREFIX        = "kv:"
PLAYBOOK_PREFIX  = "playbook:"
PLAYBOOK_TAG     = "playbook"
TASK_ID_RE       = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
TOP_K            = int(os.getenv("TOP_K",   "5"))
DEFAULT_TTL      = int(os.getenv("DEFAULT_TTL", str(90 * 24 * 3600)))  # 90 days
_SERVER_DIR      = Path(__file__).resolve().parent


def _load_instructions() -> str | None:
    path = _SERVER_DIR / "INSTRUCTIONS.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


mcp = FastMCP("Redis Memory", instructions=_load_instructions())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encode(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

async def _embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{EMBED_URL}/embed", json={"inputs": text})
        r.raise_for_status()
        return r.json()[0]

def _redis():
    return aio_redis.from_url(REDIS_URL, decode_responses=False)

async def _ensure_index(r):
    try:
        await r.execute_command("FT.INFO", INDEX)
    except Exception:
        await r.execute_command(
            "FT.CREATE", INDEX, "ON", "HASH", "PREFIX", "1", MEM_PREFIX, "SCHEMA",
            "text",      "TEXT",
            "label",     "TEXT",
            "code",      "TEXT",
            "tags",      "TAG",    "SEPARATOR", ",",
            "vector",    "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", "768", "DISTANCE_METRIC", "COSINE",
            "timestamp", "NUMERIC",
        )

def _decode(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)

def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"

def _fmt_ttl(seconds: int) -> str:
    if seconds < 0:
        return "no TTL"
    days = seconds // 86400
    if days > 0:
        return f"{days}d"
    hours = seconds // 3600
    return f"{hours}h"

def _sanitize_tag(tag: str) -> str:
    """Safe TAG index token: alnum + underscores (hyphens become underscores for RediSearch)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "", tag.strip())
    return cleaned.replace("-", "_")


def _sanitize_task_id(task_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", task_id.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug or not TASK_ID_RE.match(slug):
        raise ValueError(
            f"Invalid task_id '{task_id}'. Use lowercase letters, digits, and hyphens (3-64 chars)."
        )
    return slug


def _playbook_kv_key(task_id: str) -> str:
    return f"{PLAYBOOK_PREFIX}{_sanitize_task_id(task_id)}"


def _parse_tags_csv(tags: str) -> str:
    if not tags:
        return ""
    return ",".join(_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t))


def _parse_json_field(raw: str, field_name: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc


def _validate_playbook(doc: dict[str, Any]) -> dict[str, Any]:
    required = ("task_id", "description", "steps")
    for key in required:
        if key not in doc:
            raise ValueError(f"Playbook missing required field: {key}")
    _sanitize_task_id(str(doc["task_id"]))
    if not isinstance(doc["steps"], list) or not doc["steps"]:
        raise ValueError("Playbook steps must be a non-empty JSON array")
    doc.setdefault("version", 1)
    doc.setdefault("triggers", [])
    doc.setdefault("mcp_servers", [])
    doc.setdefault("variables", [])
    return doc


async def _load_playbook_doc(r, task_id: str) -> dict[str, Any] | None:
    data = await r.hgetall(f"{KV_PREFIX}{_playbook_kv_key(task_id)}")
    if not data:
        return None
    return _validate_playbook(json.loads(_decode(data[b"value"])))


async def _playbook_search_text(query: str, mcp_server: str, top_k: int) -> str:
    tags = PLAYBOOK_TAG
    if mcp_server:
        safe = _sanitize_tag(mcp_server)
        if safe:
            tags = f"{PLAYBOOK_TAG},{safe}"
    return await mem_search(query=query, tags=tags, top_k=top_k)


async def _store_memory(
    text: str,
    *,
    label: str = "",
    code: str = "",
    tags: str = "",
    ttl_days: int = 90,
) -> str:
    embed_input = f"{text}\n{code}" if code else text
    vector_bytes = _encode(await _embed(embed_input))
    mid = str(uuid.uuid4())

    r = _redis()
    try:
        await _ensure_index(r)
        redis_key = f"{MEM_PREFIX}{mid}"
        mapping = {
            b"text": text.encode(),
            b"vector": vector_bytes,
            b"timestamp": str(int(time.time())).encode(),
            b"ttl_days": str(ttl_days).encode(),
        }
        safe_tags = _parse_tags_csv(tags)
        if label:
            mapping[b"label"] = label.encode()
        if code:
            mapping[b"code"] = code.encode()
        if safe_tags:
            mapping[b"tags"] = safe_tags.encode()
        await r.hset(redis_key, mapping=mapping)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
    finally:
        await r.aclose()
    return mid


# ── KV tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def kv_set(key: str, value: str, label: str = "", tags: str = "", ttl_days: int = 90) -> str:
    """Store a key/value fact — instant lookup, no embeddings.
    Use for discrete facts with a known name: credentials, config, settings, names.

    Parameters:
    - key (required): Unique identifier. Use slugs like 'prod-db-url', 'user-timezone'.
      Saving with an existing key overwrites the previous value.
    - value (required): The value to store — any string (URL, password, number, JSON, etc).
    - label: Short human-readable description (shown in lists). Example: 'Production DB connection string'.
    - tags: Comma-separated labels for grouping. Example: 'db,production'.
    - ttl_days: OMIT this parameter in most cases — default is 90 days and TTL resets on
      every read so popular facts never expire. Only set explicitly when needed:
      ttl_days=365 for long-lived facts, ttl_days=7 for temporary context.
      Do NOT pass ttl_days=0 unless the fact must be permanent (no expiry ever).

    Examples: kv_set('openai-api-key', 'sk-...', label='OpenAI API key', tags='secrets,ai')
              kv_set('user-language', 'Russian', label='User preferred language', ttl_days=365)
    """
    r = _redis()
    try:
        redis_key = f"{KV_PREFIX}{key}"
        safe_tags = ",".join(_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t)) if tags else ""
        mapping = {
            b"value":     value.encode(),
            b"tags":      safe_tags.encode(),
            b"timestamp": str(int(time.time())).encode(),
            b"ttl_days":  str(ttl_days).encode(),
        }
        if label: mapping[b"label"] = label.encode()
        await r.hset(redis_key, mapping=mapping)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
    finally:
        await r.aclose()

    ttl_info = f"ttl={ttl_days}d (resets on read)" if ttl_days > 0 else "no expiry"
    desc = f" ({label})" if label else ""
    return f"Stored kv[{key}]{desc} = {value[:80]}" + (f"  tags=[{safe_tags}]" if safe_tags else "") + f"  {ttl_info}"


@mcp.tool()
async def kv_get(key: str) -> str:
    """Retrieve a value by its exact key — O(1), instant, always consistent.
    Automatically refreshes the TTL on read, so frequently accessed facts never expire.

    Parameters:
    - key (required): The exact key used when calling kv_set.
      Example: kv_get('prod-db-url') → 'postgresql://...'
    """
    r = _redis()
    try:
        redis_key = f"{KV_PREFIX}{key}"
        data = await r.hgetall(redis_key)
        if not data:
            return f"Not found: '{key}'"
        # Refresh TTL on read
        ttl_days = int(_decode(data.get(b"ttl_days", b"90")) or 90)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
        ttl_left = await r.ttl(redis_key)
    finally:
        await r.aclose()

    value = _decode(data.get(b"value", b""))
    label = _decode(data.get(b"label", b""))
    tags  = _decode(data.get(b"tags",  b""))
    ts    = _fmt_ts(data.get(b"timestamp", b"0"))
    desc = f" ({label})" if label else ""
    result = f"kv[{key}]{desc} = {value}\nsaved: {ts}  ttl: {_fmt_ttl(ttl_left)} remaining"
    if tags:
        result += f"  tags=[{tags}]"
    return result


@mcp.tool()
async def kv_delete(key: str) -> str:
    """Delete a key/value entry by its exact key.

    Parameters:
    - key (required): The exact key to delete. Cannot be undone.
    """
    r = _redis()
    try:
        deleted = await r.delete(f"{KV_PREFIX}{key}")
    finally:
        await r.aclose()
    return f"Deleted kv[{key}]" if deleted else f"Not found: '{key}'"


@mcp.tool()
async def kv_list(tag: str = "", pattern: str = "") -> str:
    """List stored key/value entries with their TTL.

    Parameters:
    - tag: Filter by tag (e.g. tag='secrets').
    - pattern: Glob pattern for key names (e.g. pattern='prod-*').
    """
    r = _redis()
    try:
        glob = f"{KV_PREFIX}{pattern}*" if pattern else f"{KV_PREFIX}*"
        keys = [k async for k in r.scan_iter(glob, count=200)]

        results = []
        for k in sorted(keys):
            data = await r.hgetall(k)
            ttl_left = await r.ttl(k)
            name  = _decode(k).replace(KV_PREFIX, "")
            value = _decode(data.get(b"value", b""))
            label = _decode(data.get(b"label", b""))
            tags_ = _decode(data.get(b"tags",  b""))
            ts    = _fmt_ts(data.get(b"timestamp", b"0"))
            if tag and tag not in tags_.split(","):
                continue
            desc = f" ({label})" if label else ""
            line = f"[{ts} | ttl:{_fmt_ttl(ttl_left)}] {name}{desc} = {value[:60]}"
            if tags_:
                line += f"  [{tags_}]"
            results.append(line)
    finally:
        await r.aclose()

    return "\n".join(results) if results else "No key/value entries found."


# ── Semantic Memory tools ─────────────────────────────────────────────────────

@mcp.tool()
async def mem_save(text: str, label: str = "", code: str = "", tags: str = "", ttl_days: int = 90) -> str:
    """Save a fact to semantic memory with a vector embedding for similarity search.
    Use for knowledge that needs to be found by meaning: decisions, patterns, context, docs.

    Parameters:
    - text (required): Full human-readable description. Written as a complete sentence.
      Example: "We use JWT with 24h expiry. Refresh tokens stored in Redis with 30d TTL."
    - label: Short human-readable description (shown in lists and search results).
      Example: "JWT refresh token strategy". Keep under 60 chars.
    - code: Code snippet or structured data associated with this fact.
    - tags: Comma-separated labels for pre-filtering. Example: "auth,jwt,backend".
    - ttl_days: OMIT this parameter in most cases — default is 90 days and TTL resets on
      every search hit so popular facts never expire. Only set explicitly when needed:
      ttl_days=365 for long-lived facts, ttl_days=7 for temporary context.
      Do NOT pass ttl_days=0 unless the fact must be permanent (no expiry ever).

    Returns the memory ID (use mem_delete to remove it).
    """
    embed_input = f"{text}\n{code}" if code else text
    mid = await _store_memory(text, label=label, code=code, tags=tags, ttl_days=ttl_days)

    display = f"'{label}'" if label else f"'{text[:60]}'"
    parts = [f"label={display}"]
    if code:      parts.append(f"code='{code[:30]}'")
    safe_tags = _parse_tags_csv(tags)
    if safe_tags: parts.append(f"tags=[{safe_tags}]")
    ttl_info = f"ttl={ttl_days}d (resets on hit)" if ttl_days > 0 else "no expiry"
    return f"Saved mem[{mid[:8]}] {', '.join(parts)}  {ttl_info}"


@mcp.tool()
async def mem_search(query: str, tags: str = "", top_k: int = 5) -> str:
    """Search semantic memory by meaning — finds relevant facts even without exact word matches.
    Automatically refreshes TTL for every result, so popular memories never expire.

    Parameters:
    - query (required): Natural language question or topic.
    - tags: Comma-separated tag pre-filter. Example: tags="auth,backend".
    - top_k: Number of results (default 5).

    Call at the start of conversations to load relevant context.
    Results show similarity %, TTL remaining, tags, and memory ID.
    """
    vector_bytes = _encode(await _embed(query))

    if tags:
        tag_filter = "|".join(_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t))
        ft_query = f"(@tags:{{{tag_filter}}})=>[KNN {top_k} @vector $vec AS score]"
    else:
        ft_query = f"*=>[KNN {top_k} @vector $vec AS score]"

    r = _redis()
    try:
        await _ensure_index(r)
        raw = await r.execute_command(
            "FT.SEARCH", INDEX, ft_query,
            "PARAMS", "2", "vec", vector_bytes,
            "RETURN", "7", "label", "text", "code", "tags", "timestamp", "score", "ttl_days",
            "SORTBY", "score",
            "DIALECT", "2",
        )

        if raw[0] == 0:
            return "No memories found."

        results = []
        items = raw[1:]
        for i in range(0, len(items), 2):
            redis_key = _decode(items[i])
            mid = redis_key.replace(MEM_PREFIX, "")
            fields = items[i + 1]
            fd = {}
            for j in range(0, len(fields), 2):
                fd[_decode(fields[j])] = _decode(fields[j + 1])

            # Refresh TTL on hit
            ttl_days = int(fd.get("ttl_days", "90") or 90)
            if ttl_days > 0:
                await r.expire(redis_key, ttl_days * 86400)
            ttl_left = await r.ttl(redis_key)

            sim  = round((1 - float(fd.get("score", 1.0))) * 100, 1)
            dt   = _fmt_ts(fd.get("timestamp", 0))
            label = fd.get("label") or fd.get("text", "")[:60]
            head = f"[{sim}% | {dt} | ttl:{_fmt_ttl(ttl_left)}] {label}  ID:{mid[:8]}"
            if fd.get("tags"): head += f"  tags=[{fd['tags']}]"
            body = fd.get("text", "")
            if fd.get("code"): body += f"\n```\n{fd['code']}\n```"
            results.append(f"{head}\n{body}")
    finally:
        await r.aclose()

    return "\n\n---\n".join(results)


@mcp.tool()
async def mem_list(limit: int = 20, tag: str = "") -> str:
    """Browse semantic memories sorted by recency with TTL info.

    Parameters:
    - limit: Maximum number of results (default 20).
    - tag: Filter by a single tag. Example: tag='auth'.
    """
    r = _redis()
    try:
        await _ensure_index(r)
        if tag:
            safe_tag = _sanitize_tag(tag)
            raw = await r.execute_command(
                "FT.SEARCH", INDEX, f"@tags:{{{safe_tag}}}",
                "RETURN", "5", "label", "text", "tags", "timestamp", "ttl_days",
                "LIMIT", "0", str(limit),
                "SORTBY", "timestamp", "DESC",
            )
            results = []
            items = raw[1:]
            for i in range(0, len(items), 2):
                redis_key = _decode(items[i])
                mid = redis_key.replace(MEM_PREFIX, "")
                fields = items[i + 1]
                fd = {_decode(fields[j]): _decode(fields[j+1]) for j in range(0, len(fields), 2)}
                ttl_left = await r.ttl(redis_key)
                dt = _fmt_ts(fd.get("timestamp", 0))
                label = fd.get("label") or fd.get("text", "")[:60]
                line = f"[{dt} | ttl:{_fmt_ttl(ttl_left)}] {label}  ID:{mid[:8]}"
                if fd.get("tags"): line += f"  [{fd['tags']}]"
                line += f"\n{fd.get('text','')[:100]}"
                results.append(line)
        else:
            keys = [k async for k in r.scan_iter(f"{MEM_PREFIX}*", count=100)][:limit]
            results = []
            for k in keys:
                data = await r.hgetall(k)
                if b"vector" not in data:
                    continue
                mid   = _decode(k).replace(MEM_PREFIX, "")
                label_ = _decode(data.get(b"label", b""))
                text  = _decode(data.get(b"text",  b""))
                tags_ = _decode(data.get(b"tags",  b""))
                ttl_left = await r.ttl(k)
                dt    = _fmt_ts(data.get(b"timestamp", b"0"))
                label = label_ or text[:60]
                line  = f"[{dt} | ttl:{_fmt_ttl(ttl_left)}] {label}  ID:{mid[:8]}"
                if tags_: line += f"  [{tags_}]"
                line += f"\n{text[:100]}"
                results.append(line)
    finally:
        await r.aclose()

    return "\n\n".join(results) if results else "No semantic memories found."


@mcp.tool()
async def mem_delete(memory_id: str) -> str:
    """Permanently delete a semantic memory by its ID.

    Parameters:
    - memory_id (required): The full UUID from mem_save or mem_search results.
    """
    r = _redis()
    try:
        deleted = await r.delete(f"{MEM_PREFIX}{memory_id}")
    finally:
        await r.aclose()
    return f"Deleted mem[{memory_id}]" if deleted else f"Not found: '{memory_id}'"


# ── MCP workflow playbooks (any server) ───────────────────────────────────────

@mcp.tool()
async def playbook_get(task_id: str) -> str:
    """Load a cached MCP workflow by exact task_id — use before rediscovering tool steps.

    Playbooks store reusable procedures: which MCP servers/tools to call, in what order,
    with parameterized args/SQL. Works for any MCP server (postgres, filesystem, REST, etc.).

    Parameters:
    - task_id (required): Stable slug, e.g. 'sync-user-profile', 'export-report-csv'.
    """
    r = _redis()
    try:
        redis_key = f"{KV_PREFIX}{_playbook_kv_key(task_id)}"
        data = await r.hgetall(redis_key)
        if not data:
            return f"No playbook found for task_id '{_sanitize_task_id(task_id)}'. Try playbook_search()."
        ttl_days = int(_decode(data.get(b"ttl_days", b"0")) or 0)
        if ttl_days > 0:
            await r.expire(redis_key, ttl_days * 86400)
        doc = _validate_playbook(json.loads(_decode(data[b"value"])))
    finally:
        await r.aclose()

    return json.dumps(doc, indent=2)


@mcp.tool()
async def playbook_search(query: str, mcp_server: str = "", top_k: int = 3) -> str:
    """Find a cached MCP workflow by natural language — fallback when task_id is unknown.

    Searches playbook summaries indexed at save time. Optionally filter by MCP server slug
    (e.g. mcp_server='postgres-mcp').

    Parameters:
    - query (required): User request or task description.
    - mcp_server: Optional filter tag matching a server used in the playbook.
    - top_k: Max results (default 3).

    After a match, call playbook_get(task_id) for the full step list.
    """
    result = await _playbook_search_text(query, mcp_server, top_k)
    if result == "No memories found.":
        return "No playbooks found. Run discovery once, then playbook_save()."
    return result


@mcp.tool()
async def playbook_save(
    task_id: str,
    description: str,
    steps: str,
    triggers: str = "",
    mcp_servers: str = "",
    variables: str = "",
    version: int = 1,
) -> str:
    """Save or update a reusable MCP workflow runbook after a successful first execution.

    Parameters:
    - task_id (required): Stable slug (lowercase, hyphens). Example: 'create-report-from-db'.
    - description (required): One-line summary of what the workflow accomplishes.
    - steps (required): JSON array of step objects. Each step should include:
        order, name, mcp_server (optional), tool, args (object), notes (optional).
      Example:
      [{"order":1,"name":"fetch_config","mcp_server":"postgres-mcp",
        "tool":"execute_prepared_select","args":{"sql":"SELECT ..."},"notes":"..."}]
    - triggers: Comma-separated example user phrases that should match this playbook.
    - mcp_servers: Comma-separated MCP server slugs used (for filtering), e.g.
      'postgres-mcp,filesystem-mcp,rest-api-mcp'.
    - variables: Comma-separated runtime parameter names (company_name, user_id, etc.).
    - version: Increment when steps or tools change (default 1).

    Playbooks are permanent (no TTL) until deleted. Also indexes a semantic alias for search.
    """
    safe_id = _sanitize_task_id(task_id)
    steps_list = _parse_json_field(steps, "steps")
    if not isinstance(steps_list, list):
        raise ValueError("steps must be a JSON array")

    trigger_list = [t.strip() for t in triggers.split(",") if t.strip()]
    server_list = [s.strip() for s in mcp_servers.split(",") if s.strip()]
    variable_list = [v.strip() for v in variables.split(",") if v.strip()]

    doc = _validate_playbook({
        "task_id": safe_id,
        "version": version,
        "description": description.strip(),
        "triggers": trigger_list,
        "mcp_servers": server_list,
        "variables": variable_list,
        "steps": steps_list,
    })
    payload = json.dumps(doc, indent=2)

    r = _redis()
    mem_id = ""
    try:
        redis_key = f"{KV_PREFIX}{PLAYBOOK_PREFIX}{safe_id}"
        existing = await r.hgetall(redis_key)
        mem_id = _decode(existing.get(b"mem_id", b"")) if existing else ""

        await r.hset(
            redis_key,
            mapping={
                b"value": payload.encode(),
                b"label": description.encode(),
                b"tags": _parse_tags_csv(f"{PLAYBOOK_TAG},{mcp_servers}").encode(),
                b"timestamp": str(int(time.time())).encode(),
                b"ttl_days": b"0",
            },
        )

        if mem_id:
            await r.delete(f"{MEM_PREFIX}{mem_id}")
    finally:
        await r.aclose()

    search_text = (
        f"Playbook {safe_id}: {description}. "
        f"MCP servers: {', '.join(server_list) or 'any'}. "
        f"Triggers: {', '.join(trigger_list) or 'none'}. "
        f"Variables: {', '.join(variable_list) or 'none'}. "
        f"Use playbook_get('{safe_id}') for full steps."
    )
    server_tags = ",".join(_sanitize_tag(s) for s in server_list if _sanitize_tag(s))
    mem_tags = PLAYBOOK_TAG if not server_tags else f"{PLAYBOOK_TAG},{server_tags}"
    new_mem_id = await _store_memory(
        search_text,
        label=f"Playbook: {description[:50]}",
        code=payload,
        tags=mem_tags,
        ttl_days=0,
    )

    r = _redis()
    try:
        await r.hset(f"{KV_PREFIX}{PLAYBOOK_PREFIX}{safe_id}", b"mem_id", new_mem_id.encode())
    finally:
        await r.aclose()

    return (
        f"Saved playbook[{safe_id}] v{version} — {len(steps_list)} steps, "
        f"servers=[{', '.join(server_list) or 'any'}]  indexed for playbook_search"
    )


@mcp.tool()
async def playbook_list(mcp_server: str = "") -> str:
    """List cached MCP workflow playbooks.

    Parameters:
    - mcp_server: Optional filter — only playbooks tagged with this server slug.
    """
    r = _redis()
    try:
        rows = []
        async for k in r.scan_iter(f"{KV_PREFIX}{PLAYBOOK_PREFIX}*", count=200):
            name = _decode(k).replace(KV_PREFIX, "").replace(PLAYBOOK_PREFIX, "")
            if name.startswith("_"):
                continue
            data = await r.hgetall(k)
            tags_ = _decode(data.get(b"tags", b""))
            if mcp_server:
                safe = _sanitize_tag(mcp_server)
                if safe and safe not in tags_.split(","):
                    continue
            doc = json.loads(_decode(data[b"value"]))
            ts = _fmt_ts(data.get(b"timestamp", b"0"))
            servers = ", ".join(doc.get("mcp_servers") or []) or "any"
            rows.append(
                f"[{ts}] {name} v{doc.get('version', 1)} — {doc.get('description', '')[:70]}  "
                f"servers=[{servers}]  steps={len(doc.get('steps') or [])}"
            )
    finally:
        await r.aclose()

    return "\n".join(sorted(rows)) if rows else "No playbooks saved yet."


@mcp.tool()
async def playbook_delete(task_id: str) -> str:
    """Delete a cached MCP workflow and its semantic search alias.

    Parameters:
    - task_id (required): The playbook slug passed to playbook_save().
    """
    safe_id = _sanitize_task_id(task_id)
    r = _redis()
    mem_id = ""
    try:
        redis_key = f"{KV_PREFIX}{PLAYBOOK_PREFIX}{safe_id}"
        data = await r.hgetall(redis_key)
        if not data:
            return f"No playbook found for '{safe_id}'"
        mem_id = _decode(data.get(b"mem_id", b""))
        await r.delete(redis_key)
    finally:
        await r.aclose()

    if mem_id:
        await mem_delete(mem_id)

    return f"Deleted playbook[{safe_id}]"


@mcp.tool()
async def playbook_resolve(user_request: str, mcp_server: str = "", min_similarity: float = 35.0) -> str:
    """Resolve a user request to a cached MCP workflow — preferred entry point before multi-step tasks.

    1. Tries exact task_id if user_request looks like a slug.
    2. Runs playbook_search() for semantic matches.
    3. Returns full playbook JSON when similarity >= min_similarity (default 35%).

    Parameters:
    - user_request (required): Raw user prompt or task description.
    - mcp_server: Optional MCP server filter.
    - min_similarity: Minimum similarity percent to accept a semantic match (default 35).
    """
    candidate = _sanitize_task_id(user_request) if TASK_ID_RE.match(user_request.strip().lower()) else ""
    if candidate:
        r = _redis()
        try:
            if await r.exists(f"{KV_PREFIX}{PLAYBOOK_PREFIX}{candidate}"):
                doc = await _load_playbook_doc(r, candidate)
                if doc:
                    return json.dumps({"match": "exact", "similarity": 100.0, "playbook": doc}, indent=2)
        finally:
            await r.aclose()

    search_out = await _playbook_search_text(user_request, mcp_server, top_k=1)
    if search_out == "No memories found.":
        return (
            "No cached playbook. Discover the workflow using available MCP tools, "
            "then playbook_save() before repeating this task."
        )

    first_block = search_out.split("\n")[0]
    sim_match = re.search(r"\[(\d+(?:\.\d+)?)%", first_block)
    similarity = float(sim_match.group(1)) if sim_match else 0.0
    if similarity < min_similarity:
        return (
            f"Best playbook match was {similarity}% (below {min_similarity}%). "
            "Discover the workflow, then playbook_save()."
        )

    id_match = re.search(r"ID:([0-9a-f]{8})", first_block)
    if not id_match:
        return search_out + "\n\n(Could not resolve task_id — call playbook_search() manually.)"

    prefix = id_match.group(1)
    r = _redis()
    task_id = ""
    try:
        async for k in r.scan_iter(f"{KV_PREFIX}{PLAYBOOK_PREFIX}*", count=200):
            data = await r.hgetall(k)
            linked = _decode(data.get(b"mem_id", b""))
            if linked.startswith(prefix):
                task_id = _decode(k).replace(KV_PREFIX, "").replace(PLAYBOOK_PREFIX, "")
                break
        if not task_id:
            return search_out
        doc = await _load_playbook_doc(r, task_id)
    finally:
        await r.aclose()

    if not doc:
        return search_out

    return json.dumps({"match": "semantic", "similarity": similarity, "playbook": doc}, indent=2)


# ── Unified search ────────────────────────────────────────────────────────────

@mcp.tool()
async def search(query: str, tags: str = "", top_k: int = 5) -> str:
    """Search ALL memory at once — both key-value and semantic.
    Use this as the default search tool. Combines results from both stores.

    Parameters:
    - query (required): Natural language question, topic, or key name.
    - tags: Comma-separated tag pre-filter.
    - top_k: Max semantic results (default 5). All matching kv entries are always included.

    Returns kv matches (by key/value substring) + semantic matches (by meaning), clearly separated.
    """
    parts = []

    # 1. Search kv by substring in key and value
    r = _redis()
    try:
        kv_results = []
        q_lower = query.lower()
        async for k in r.scan_iter(f"{KV_PREFIX}*", count=200):
            data = await r.hgetall(k)
            name  = _decode(k).replace(KV_PREFIX, "")
            value = _decode(data.get(b"value", b""))
            label = _decode(data.get(b"label", b""))
            tags_ = _decode(data.get(b"tags",  b""))
            if tags:
                filter_tags = {_sanitize_tag(t) for t in tags.split(",") if _sanitize_tag(t)}
                entry_tags = set(tags_.split(",")) if tags_ else set()
                if not filter_tags & entry_tags:
                    continue
            if q_lower in name.lower() or q_lower in value.lower() or q_lower in label.lower():
                ttl_left = await r.ttl(k)
                ttl_days = int(_decode(data.get(b"ttl_days", b"90")) or 90)
                if ttl_days > 0:
                    await r.expire(k, ttl_days * 86400)
                dt = _fmt_ts(data.get(b"timestamp", b"0"))
                desc = f" ({label})" if label else ""
                line = f"[{dt} | ttl:{_fmt_ttl(ttl_left)}] {name}{desc} = {value[:80]}"
                if tags_: line += f"  [{tags_}]"
                kv_results.append(line)
    finally:
        await r.aclose()

    if kv_results:
        parts.append("── Key-Value matches ──\n" + "\n".join(kv_results))

    # 2. Semantic search
    mem_result = await mem_search(query=query, tags=tags, top_k=top_k)
    if mem_result and mem_result != "No memories found.":
        parts.append("── Semantic matches ──\n" + mem_result)

    if not parts:
        return "Nothing found in any memory store."

    return "\n\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
