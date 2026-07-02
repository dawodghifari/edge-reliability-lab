output "grafana_port_forward" {
  description = "Run this, then open http://localhost:3000 (admin / admin)."
  value       = "kubectl -n ${var.monitoring_namespace} port-forward svc/${var.release_name}-grafana 3000:80"
}

output "prometheus_port_forward" {
  description = "Run this, then open http://localhost:9090."
  value       = "kubectl -n ${var.monitoring_namespace} port-forward svc/${var.release_name}-kube-prome-prometheus 9090:9090"
}

output "app_namespace" {
  description = "Namespace where edge-cache + Redis run."
  value       = var.app_namespace
}

output "load_test_url" {
  description = "k6 target (NodePort 30080, mapped to localhost by kind)."
  value       = "http://localhost:30080"
}
