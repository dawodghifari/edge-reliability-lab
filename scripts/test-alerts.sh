#!/usr/bin/env bash
# Unit-test the SLO recording rules and alerts with promtool.
#
# The rules live in a PrometheusRule (k8s/slo-rules.yaml) so the operator can
# load them. promtool wants a plain rule file, so the rule groups are extracted
# into build/ first, then checked and run against the synthetic series in
# k8s/tests/slo-rules.test.yaml. No cluster needed.
#
# Uses the promtool inside the pinned Prometheus image unless PROMTOOL is set.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
mkdir -p build

python3 - <<'PY'
import yaml

with open("k8s/slo-rules.yaml") as f:
    rule = yaml.safe_load(f)
with open("build/slo-rules.rules.yaml", "w") as f:
    yaml.safe_dump({"groups": rule["spec"]["groups"]}, f, sort_keys=False)
PY

PROMTOOL="${PROMTOOL:-docker run --rm -v $REPO_ROOT:/w -w /w --entrypoint promtool prom/prometheus:v2.53.0}"
$PROMTOOL check rules build/slo-rules.rules.yaml
$PROMTOOL test rules k8s/tests/slo-rules.test.yaml
