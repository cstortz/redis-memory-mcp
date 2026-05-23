#!/usr/bin/env python3
"""TCP ↔ stdio bridge for MCP clients (socat TCP:host:port STDIO)."""

import asyncio
import logging
import os
import sys
from pathlib import Path

HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_TCP_PORT", "3006"))
MAX_CONNECTIONS = int(os.getenv("MCP_MAX_CONNECTIONS", "32"))
SERVER_SCRIPT = Path(__file__).resolve().parent / "memory_mcp.py"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("tcp_server")


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    peer = client_writer.get_extra_info("peername")
    log.info("Client connected from %s", peer)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SERVER_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )

    assert proc.stdin and proc.stdout

    to_proc = asyncio.create_task(_pump(client_reader, proc.stdin))
    to_client = asyncio.create_task(_pump(proc.stdout, client_writer))

    done, pending = await asyncio.wait(
        {to_proc, to_client, asyncio.create_task(proc.wait())},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()

    if proc.returncode not in (0, None):
        err = b""
        if proc.stderr:
            err = await proc.stderr.read()
        if err:
            log.warning("MCP subprocess exited %s: %s", proc.returncode, err.decode(errors="replace")[:500])

    try:
        client_writer.close()
        await client_writer.wait_closed()
    except Exception:
        pass
    log.info("Client disconnected from %s", peer)


async def _run() -> None:
    server = await asyncio.start_server(_handle, HOST, PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    log.info("redis-memory-mcp TCP bridge listening on %s (script=%s)", addrs, SERVER_SCRIPT)
    async with server:
        await server.serve_forever()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
