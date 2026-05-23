#!/usr/bin/env python3
"""End-to-end test of redis-memory-mcp tools via MCP stdio protocol."""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6380/0")
os.environ.setdefault("EMBED_URL", "http://127.0.0.1:8081")
os.environ.setdefault("INDEX_NAME", "idx:memories")

SERVER = Path(__file__).parent / "server" / "memory_mcp.py"
TAG = "cursor-eval-test"


async def rpc(proc, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    await proc.stdin.drain()
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stdout")
        data = json.loads(line.decode())
        if data.get("id") == req_id:
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]


async def call_tool(proc, name: str, arguments: dict, req_id: int) -> str:
    result = await rpc(
        proc,
        "tools/call",
        {"name": name, "arguments": arguments},
        req_id=req_id,
    )
    content = result.get("content") or []
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    return "\n".join(texts) if texts else str(result)


async def main() -> int:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SERVER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )

    failures: list[str] = []
    req_id = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    try:
        req_id += 1
        init = await rpc(
            proc,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "eval", "version": "1.0"},
            },
            req_id=req_id,
        )
        check("initialize", init.get("serverInfo", {}).get("name") == "Redis Memory")

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n")
        await proc.stdin.drain()

        req_id += 1
        tools = await rpc(proc, "tools/list", {}, req_id=req_id)
        tool_names = {t["name"] for t in tools.get("tools", [])}
        expected = {"kv_set", "kv_get", "kv_delete", "kv_list", "mem_save", "mem_search", "mem_list", "mem_delete"}
        check("tools/list", expected.issubset(tool_names), f"got {sorted(tool_names)}")

        # KV round-trip
        req_id += 1
        out = await call_tool(
            proc,
            "kv_set",
            {
                "key": "eval-stack-name",
                "value": "redis-memory-mcp on dev01",
                "label": "Eval stack identifier",
                "tags": TAG,
            },
            req_id,
        )
        check("kv_set", "Stored kv[eval-stack-name]" in out, out[:120])

        req_id += 1
        out = await call_tool(proc, "kv_get", {"key": "eval-stack-name"}, req_id)
        check("kv_get", "redis-memory-mcp on dev01" in out, out.split("\n")[0])

        req_id += 1
        out = await call_tool(proc, "kv_list", {"tag": TAG}, req_id)
        check("kv_list", "eval-stack-name" in out, f"{len(out.splitlines())} entries")

        # Semantic memory
        req_id += 1
        out = await call_tool(
            proc,
            "mem_save",
            {
                "text": "The user is evaluating redis-memory-mcp for Cursor agent memory and prefers TypeScript for new frontend work.",
                "label": "Eval context + language preference",
                "tags": TAG,
            },
            req_id,
        )
        check("mem_save", out.startswith("Saved mem["), out[:120])
        mem_id_prefix = out.split("mem[")[1].split("]")[0] if "mem[" in out else ""

        req_id += 1
        out = await call_tool(
            proc,
            "mem_save",
            {
                "text": "Redis vector search uses RediSearch HNSW indexes with 768-dimensional embeddings from TEI.",
                "label": "Redis memory architecture",
                "tags": TAG,
            },
            req_id,
        )
        check("mem_save #2", "Saved mem[" in out)

        req_id += 1
        out = await call_tool(
            proc,
            "mem_search",
            {"query": "What programming language does the user prefer for frontend?", "tags": TAG, "top_k": 3},
            req_id,
        )
        check(
            "mem_search (language)",
            "TypeScript" in out or "typescript" in out.lower(),
            out.split("\n")[0][:100] if out else "empty",
        )

        req_id += 1
        out = await call_tool(
            proc,
            "mem_search",
            {"query": "How does vector similarity search work in this setup?", "tags": TAG, "top_k": 3},
            req_id,
        )
        check(
            "mem_search (architecture)",
            "HNSW" in out or "embedding" in out.lower() or "RediSearch" in out,
            out.split("\n")[0][:100] if out else "empty",
        )

        req_id += 1
        out = await call_tool(proc, "mem_list", {"tag": TAG, "limit": 10}, req_id)
        check("mem_list", "mem[" in out or "Eval" in out, f"{len(out.splitlines())} lines")

        # Cleanup
        req_id += 1
        out = await call_tool(proc, "kv_delete", {"key": "eval-stack-name"}, req_id)
        check("kv_delete", "Deleted kv[eval-stack-name]" in out)

        # Delete mem entries by listing keys in redis directly
        import redis.asyncio as aio_redis

        r = aio_redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        keys = [k async for k in r.scan_iter("mem:*", count=200)]
        deleted = 0
        for k in keys:
            data = await r.hgetall(k)
            if TAG in (data.get("tags") or ""):
                await r.delete(k)
                deleted += 1
        await r.aclose()
        check("cleanup", deleted >= 2, f"removed {deleted} test memories")

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
