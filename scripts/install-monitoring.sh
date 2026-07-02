#!/usr/bin/env bash
# Phase 2: install the monitoring stack and wire up scraping + the dashboard.
#
#   bash scripts/install-monitoring.sh
#
# Installs kube-prometheus-stack (Prometheus + Grafana + Alertmanager) into the
# `monitoring` namespace, applies the edge-cache ServiceMonitor, and provisions the
# Grafana dashboard from dashboards/edge-cache-dashboard.json via a labelled
# ConfigMap (the Grafana sidecar auto-imports it).
set -euo pipefail

RELEASE="kube-prom-stack"
MON_NS="monitoring"
APP_NS="edge-lab"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "→ Adding the prometheus-community Helm repo ..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

echo "→ Installing/upgrading kube-prometheus-stack (this pulls several images; first run is slow) ..."
helm upgrade --install "$RELEASE" prometheus-community/kube-prometheus-stack \
  --namespace "$MON_NS" --create-namespace \
  -f "$REPO_ROOT/k8s/monitoring-values.yaml" \
  --wait --timeout 10m

echo "→ Applying the edge-cache ServiceMonitor ..."
kubectl apply -f "$REPO_ROOT/k8s/servicemonitor.yaml"

echo "→ Applying SLO recording + alerting rules ..."
kubectl apply -f "$REPO_ROOT/k8s/slo-rules.yaml"

echo "→ Provisioning the Grafana dashboard (labelled ConfigMap) ..."
kubectl create configmap edge-cache-dashboard \
  --namespace "$MON_NS" \
  --from-file=edge-cache-dashboard.json="$REPO_ROOT/dashboards/edge-cache-dashboard.json" \
  --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -

echo
echo "✓ Monitoring installed. Pods:"
kubectl -n "$MON_NS" get pods

# Discover the actual service names (chart truncates them by release-name length,
# and there are two prometheus svcs — skip the headless "prometheus-operated").
GRAF_SVC=$(kubectl -n "$MON_NS" get svc -l "app.kubernetes.io/name=grafana" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "${RELEASE}-grafana")
# `|| true` so a SIGPIPE from `head` under `set -o pipefail` doesn't abort the script.
PROM_SVC=$(kubectl -n "$MON_NS" get svc -l "app.kubernetes.io/name=prometheus" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
  | { grep -v 'operated' || true; } | head -1)
PROM_SVC=${PROM_SVC:-${RELEASE}-kube-prome-prometheus}

cat <<EOF

Next:
  # Grafana (admin / admin):
  kubectl -n $MON_NS port-forward svc/${GRAF_SVC} 3000:80
  # open http://localhost:3000  → Dashboards → "Edge Cache — Golden Signals + Cache"

  # Prometheus (to check the target is UP):
  kubectl -n $MON_NS port-forward svc/${PROM_SVC} 9090:9090
  # open http://localhost:9090/targets  → look for serviceMonitor/edge-lab/edge-cache

  # Generate traffic so the panels light up:
  bash scripts/generate-traffic.sh
EOF
