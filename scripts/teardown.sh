#!/usr/bin/env bash
# Tear down the lab. By default removes app resources; pass --cluster to also
# delete the whole kind cluster.
set -euo pipefail

CLUSTER_NAME="edge-lab"
NS="edge-lab"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "→ Deleting app resources in namespace '$NS' ..."
kubectl delete namespace "$NS" --ignore-not-found

if [[ "${1:-}" == "--cluster" ]]; then
  echo "→ Deleting kind cluster '$CLUSTER_NAME' ..."
  kind delete cluster --name "$CLUSTER_NAME"
fi

echo "✓ Teardown complete."
