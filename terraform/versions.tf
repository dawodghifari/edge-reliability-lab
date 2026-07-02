terraform {
  required_version = ">= 1.3.0"

  required_providers {
    # Installs the kube-prometheus-stack chart.
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    # Manages the Grafana dashboard ConfigMap.
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    # Applies our existing raw k8s/*.yaml manifests (handles multi-doc + CRDs
    # created by the Helm chart better than kubernetes_manifest).
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = "~> 1.14"
    }
  }
}
