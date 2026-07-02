#!/usr/bin/env bash
# Build the edge-cache image and load it into the kind cluster's node images,
# so Kubernetes can run it without pushing to a registry.
set -euo pipefail

CLUSTER_NAME="edge-lab"
IMAGE="edge-cache:dev"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "→ Building $IMAGE ..."
docker build -t "$IMAGE" "$REPO_ROOT/app"

echo "→ Loading $IMAGE into kind cluster '$CLUSTER_NAME' ..."
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"

echo "✓ Image built and loaded."
