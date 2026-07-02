#!/usr/bin/env bash
# Create the local kind cluster. Idempotent: skips creation if it already exists.
set -euo pipefail

CLUSTER_NAME="edge-lab"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "✓ kind cluster '$CLUSTER_NAME' already exists."
else
  echo "→ Creating kind cluster '$CLUSTER_NAME'..."
  kind create cluster --name "$CLUSTER_NAME" --config "$REPO_ROOT/k8s/kind-config.yaml"
fi

echo "→ Cluster nodes:"
kubectl get nodes
