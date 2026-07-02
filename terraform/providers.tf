# All three providers talk to the local kind cluster through your kubeconfig.
# kind writes a context named "kind-<clustername>", so the default context here is
# "kind-edge-lab" (see variables.tf).

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = var.kubeconfig_path
    config_context = var.kube_context
  }
}

provider "kubectl" {
  config_path      = var.kubeconfig_path
  config_context   = var.kube_context
  load_config_file = true
}
