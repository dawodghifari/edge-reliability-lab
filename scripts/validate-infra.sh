#!/usr/bin/env bash
# Validate the Kubernetes manifests and the Terraform without a cluster.
#
#   kubeconform  every manifest Terraform applies, strict, including the
#                Prometheus-operator CRDs (ServiceMonitor, PrometheusRule)
#   terraform    init without a backend, validate, and a format check
#
# kind-config.yaml and the *-values.yaml files are not Kubernetes objects (kind
# cluster config and Helm values), so they are not passed to kubeconform.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MANIFESTS=(
  k8s/namespace.yaml
  k8s/redis.yaml
  k8s/edge-cache.yaml
  k8s/edge-cache-nodeport.yaml
  k8s/servicemonitor.yaml
  k8s/slo-rules.yaml
)
CRD_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

echo "→ kubeconform"
KUBECONFORM="${KUBECONFORM:-docker run --rm -v $REPO_ROOT:/w -w /w ghcr.io/yannh/kubeconform:v0.6.7}"
$KUBECONFORM -strict -summary -schema-location default -schema-location "$CRD_SCHEMAS" "${MANIFESTS[@]}"

echo "→ terraform"
terraform -chdir=terraform init -backend=false -input=false >/dev/null
terraform -chdir=terraform validate
terraform -chdir=terraform fmt -check -recursive
