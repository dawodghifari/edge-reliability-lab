variable "kubeconfig_path" {
  description = "Path to the kubeconfig file."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context for the kind cluster (kind names it kind-<cluster>)."
  type        = string
  default     = "kind-edge-lab"
}

variable "app_namespace" {
  description = "Namespace for the edge-cache app + Redis."
  type        = string
  default     = "edge-lab"
}

variable "monitoring_namespace" {
  description = "Namespace for the kube-prometheus-stack release."
  type        = string
  default     = "monitoring"
}

variable "release_name" {
  description = "Helm release name for kube-prometheus-stack."
  type        = string
  default     = "kube-prom-stack"
}

variable "chart_version" {
  description = <<-EOT
    kube-prometheus-stack chart version. null = latest.
    Pin this for true reproducibility — find the version you already run with:
      helm list -n monitoring -o json | jq '.[].chart'
  EOT
  type        = string
  default     = null
}
