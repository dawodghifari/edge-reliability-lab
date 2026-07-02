# Terraform — reproducible in-cluster stack

Declares the cluster add-ons and app resources so the whole lab comes back with
`terraform apply`. It uses a pragmatic split:

- **The kind cluster and the image** are created by scripts (`scripts/setup.sh`,
  `scripts/build-and-load.sh`) — creating a kind cluster from Terraform is flakier
  and harder to demo.
- **Everything inside the cluster is Terraform's job:** the `kube-prometheus-stack`
  Helm release, Redis, the edge-cache Deployment/Service (+ NodePort), the
  ServiceMonitor, the SLO recording/alerting rules, and the Grafana dashboard
  ConfigMap.

The Kubernetes resources are **your existing `k8s/*.yaml` applied as-is** (via the
`kubectl` provider) — the YAML stays the single source of truth, so there's no
duplication or drift. Terraform effectively replaces `scripts/install-monitoring.sh`.

## Prerequisites

- `terraform` >= 1.3, plus `kind`, `kubectl`, `helm`, `docker` (same tools as the rest
  of the lab).
- Provider plugins are fetched by `terraform init` (needs network the first time).

## Bring the stack up from scratch

```bash
# 1) Cluster + image (scripts, not Terraform)
bash scripts/setup.sh
bash scripts/build-and-load.sh

# 2) Everything in-cluster (Terraform)
cd terraform
terraform init
terraform apply
```

`terraform apply` prints the Grafana / Prometheus port-forward commands and the k6
target URL as outputs.

> **Order matters:** build-and-load the `edge-cache:dev` image *before* `apply`, or the
> edge-cache pods will `ImagePullBackOff` (the image only lives in the kind node, not a
> registry). Redis uses the public `redis:7-alpine`, so it needs no pre-loading.

## Adopting an already-running cluster

If you already installed the stack with the scripts, Terraform doesn't know about those
objects yet. The `kubectl_manifest` and ConfigMap resources use server-side apply, so
they'll adopt cleanly — but the Helm release must be imported once:

```bash
terraform import helm_release.kube_prom_stack monitoring/kube-prom-stack
terraform apply
```

Or just start clean: `bash scripts/teardown.sh` then follow "from scratch" above.

## Pinning the chart version (reproducibility)

`chart_version` defaults to `null` (latest) so a first apply always works. For a truly
reproducible rebuild, pin it to the version you run:

```bash
helm list -n monitoring -o json | jq '.[].chart'   # e.g. kube-prometheus-stack-65.5.0
terraform apply -var 'chart_version=65.5.0'
```

## Tear down

```bash
cd terraform && terraform destroy   # removes in-cluster resources
bash ../scripts/teardown.sh         # deletes the kind cluster itself
```

## What's here

| File | Purpose |
|------|---------|
| `versions.tf` | Terraform + provider version constraints |
| `providers.tf` | helm / kubernetes / kubectl providers pointed at the kind context |
| `variables.tf` | kube context, namespaces, release name, chart version |
| `main.tf` | Helm release + namespace + app manifests + dashboard ConfigMap |
| `outputs.tf` | port-forward commands + k6 URL |
