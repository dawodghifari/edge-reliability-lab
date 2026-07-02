#!/usr/bin/env bash
# Apply the namespace, Redis, and edge-cache manifests and wait for them to be Ready.
set -euo pipefail

NS="edge-lab"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S="$REPO_ROOT/k8s"

echo "→ Applying manifests ..."
kubectl apply -f "$K8S/namespace.yaml"
kubectl apply -f "$K8S/redis.yaml"
kubectl apply -f "$K8S/edge-cache.yaml"
kubectl apply -f "$K8S/edge-cache-nodeport.yaml"

echo "→ Waiting for Redis to be ready ..."
kubectl -n "$NS" rollout status deployment/redis --timeout=120s

echo "→ Waiting for edge-cache to be ready ..."
kubectl -n "$NS" rollout status deployment/edge-cache --timeout=120s

echo
echo "✓ Deployed. Pods:"
kubectl -n "$NS" get pods -o wide
echo
echo "Try it:"
echo "  kubectl -n $NS port-forward svc/edge-cache 8000:8000"
echo "  curl -i localhost:8000/segment/abc   # MISS, then HIT on a second call"
