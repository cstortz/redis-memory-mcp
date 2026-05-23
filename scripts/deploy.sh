#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE_TAG="${IMAGE_TAG:-latest}"
NAMESPACE="${NAMESPACE:-redis-memory}"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/cstortz/redis-memory-mcp}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [build|deploy|status]

  build    Build mcp-server Docker image locally
  deploy   Apply k8s manifests (requires kubectl + image in GHCR or local load)
  status   Show deployment status

Environment:
  IMAGE_TAG      Image tag (default: latest)
  IMAGE_PREFIX   GHCR prefix (default: ghcr.io/cstortz/redis-memory-mcp)
  NAMESPACE      Kubernetes namespace (default: redis-memory)

Preferred: push to main or run GitHub Actions "Deploy" workflow.
EOF
}

build_image() {
  docker build -f server/Dockerfile -t "${IMAGE_PREFIX}/mcp-server:${IMAGE_TAG}" server
}

deploy() {
  kubectl apply -f k8s/namespace.yaml
  kubectl apply -f k8s/configmap.yaml
  kubectl apply -f k8s/redis-stack.yaml
  kubectl apply -f k8s/embeddings.yaml
  kubectl apply -f k8s/mcp-redis-memory.yaml
  kubectl apply -f k8s/portal.yaml
  kubectl apply -f k8s/ingress.yaml
  kubectl set image deployment/mcp-redis-memory \
    "mcp-redis-memory=${IMAGE_PREFIX}/mcp-server:${IMAGE_TAG}" \
    -n "$NAMESPACE"
  kubectl delete job redis-init-index -n "$NAMESPACE" --ignore-not-found
  kubectl apply -f k8s/redis-init-job.yaml
  kubectl patch configmap tcp-services -n ingress-nginx --type merge \
    -p '{"data":{"3006":"redis-memory/mcp-redis-memory:3006"}}' 2>/dev/null \
    || kubectl apply -f k8s/ingress-tcp.yaml
  kubectl rollout status deployment/redis-stack -n "$NAMESPACE" --timeout=300s
  kubectl rollout status deployment/embeddings -n "$NAMESPACE" --timeout=600s
  kubectl wait --for=condition=complete job/redis-init-index -n "$NAMESPACE" --timeout=120s || true
  kubectl rollout status deployment/mcp-redis-memory -n "$NAMESPACE" --timeout=180s
  kubectl rollout status deployment/redis-memory-portal -n "$NAMESPACE" --timeout=120s
}

status() {
  kubectl get pods,svc,pvc -n "$NAMESPACE"
}

case "${1:-}" in
  build) build_image ;;
  deploy) deploy ;;
  status) status ;;
  *) usage; exit 1 ;;
esac
