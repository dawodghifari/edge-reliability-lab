locals {
  repo_root = "${path.module}/.."

  # The existing k8s/*.yaml files stay the single source of truth — Terraform just
  # applies them, so there's no duplicated/ drifting resource definitions. The
  # namespace is applied on its own (below) so it exists before everything else.
  # kind-config.yaml (used by `kind create`) and monitoring-values.yaml (Helm
  # values, not a manifest) are intentionally excluded.
  app_manifest_files = [
    "${local.repo_root}/k8s/redis.yaml",
    "${local.repo_root}/k8s/edge-cache.yaml",
    "${local.repo_root}/k8s/edge-cache-nodeport.yaml",
    "${local.repo_root}/k8s/servicemonitor.yaml",
    "${local.repo_root}/k8s/slo-rules.yaml",
  ]
}

# 1) Monitoring stack (Prometheus + Grafana + Alertmanager) via Helm, using the
#    same values file the script used. create_namespace makes the monitoring ns.
resource "helm_release" "kube_prom_stack" {
  name             = var.release_name
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = var.chart_version
  namespace        = var.monitoring_namespace
  create_namespace = true

  values = [file("${local.repo_root}/k8s/monitoring-values.yaml")]

  # The chart installs a lot (CRDs, operator, Prometheus, Grafana). Give it room.
  timeout = 900
}

# 2) App namespace — created before any namespaced app resource.
resource "kubectl_manifest" "namespace" {
  yaml_body = file("${local.repo_root}/k8s/namespace.yaml")
}

# 3) App + Redis + ServiceMonitor + SLO rules. Split the multi-doc YAML files into
#    individual documents and apply each. depends_on ensures (a) the edge-lab
#    namespace exists, and (b) the Helm chart is installed first, so the
#    ServiceMonitor / PrometheusRule CRDs exist before we create those objects.
data "kubectl_file_documents" "app" {
  content = join("\n---\n", [for f in local.app_manifest_files : file(f)])
}

resource "kubectl_manifest" "app" {
  for_each  = data.kubectl_file_documents.app.manifests
  yaml_body = each.value

  depends_on = [
    kubectl_manifest.namespace,
    helm_release.kube_prom_stack,
  ]
}

# 4) Grafana dashboard — provisioned as a ConfigMap labelled grafana_dashboard=1,
#    which the Grafana sidecar auto-imports. Replaces the imperative
#    `kubectl create configmap ... | kubectl label ... | kubectl apply` in
#    scripts/install-monitoring.sh, so the dashboard is now IaC too.
resource "kubernetes_config_map_v1" "dashboard" {
  metadata {
    name      = "edge-cache-dashboard"
    namespace = var.monitoring_namespace
    labels = {
      grafana_dashboard = "1"
    }
  }

  data = {
    "edge-cache-dashboard.json" = file("${local.repo_root}/dashboards/edge-cache-dashboard.json")
  }

  depends_on = [helm_release.kube_prom_stack]
}
