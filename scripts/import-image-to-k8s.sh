#!/usr/bin/env bash
set -euo pipefail

# Load a local Docker image into all Kubernetes worker nodes (containerd).
# Use when GHCR pull secrets are unavailable but images are built on dev01.
#
# Usage:
#   ./scripts/import-image-to-k8s.sh ghcr.io/cstortz/redis-memory-mcp/mcp-server:latest

IMAGE="${1:?image reference required}"
NODES=(192.168.68.21 192.168.68.22 192.168.68.23 192.168.68.24)
SSH_USER="${SSH_USER:-cstortz}"
TMP="/tmp/k8s-image-import-$$.tar"

cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

echo "==> Saving ${IMAGE}"
docker save "$IMAGE" -o "$TMP"

for node in "${NODES[@]}"; do
  echo "==> Importing on ${node}"
  scp -q "$TMP" "${SSH_USER}@${node}:/tmp/k8s-image-import.tar"
  ssh "${SSH_USER}@${node}" "sudo ctr -n k8s.io images import /tmp/k8s-image-import.tar && rm -f /tmp/k8s-image-import.tar"
done

echo "==> Done: ${IMAGE} on ${#NODES[@]} nodes"
