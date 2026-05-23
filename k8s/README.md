# Kubernetes deployment

Full stack in namespace **`redis-memory`**:

| Component | Service | Port |
|-----------|---------|------|
| Redis Stack + RediSearch | `redis-stack` | 6379 (cluster-internal) |
| TEI embeddings | `embeddings` | 80 (cluster-internal) |
| MCP server (TCP bridge) | `mcp-redis-memory` | **3006** |

## GitHub Actions

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| **CI** | PR / push to `main` | Docker build, kubeconform, Python compile |
| **Deploy** | push to `main` (server/k8s paths) or manual | Build/push GHCR image, deploy to cluster |

### Required secrets (repository settings)

Same as `monorepo_mcp`:

| Secret | Purpose |
|--------|---------|
| `KUBE_CONFIG` | Base64 kubeconfig or raw YAML for self-hosted deploy runner |
| `GHCR_TOKEN` | Optional; falls back to `GITHUB_TOKEN` for pull secret |

### Image

```
ghcr.io/<owner>/redis-memory-mcp/mcp-server:latest
```

## Manual deploy

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh build
IMAGE_TAG=latest ./scripts/deploy.sh deploy
./scripts/deploy.sh status
```

## URLs (monorepo-mcp.dev.stortz.tech)

| Purpose | URL |
|---------|-----|
| MCP TCP | `socat TCP:monorepo-mcp.dev.stortz.tech:3006 STDIO` |
| HTTP docs | https://monorepo-mcp.dev.stortz.tech/redis-memory/ |
| Endpoints JSON | https://monorepo-mcp.dev.stortz.tech/redis-memory/endpoints.json |
| Main MCP portal | https://monorepo-mcp.dev.stortz.tech/ |

## Cursor MCP config

```json
"redis-memory-mcp": {
  "command": "socat",
  "args": ["TCP:monorepo-mcp.dev.stortz.tech:3006", "STDIO"]
}
```

Ingress nginx must expose TCP **3006** (`k8s/ingress-tcp.yaml` merges into `tcp-services`).

## Local development

Keep using Docker Compose + local Python stdio — see [EVAL.md](EVAL.md).
