#!/bin/sh
set -eu
cluster=dmarc-ci
cleanup() { kind delete cluster --name "$cluster"; }
trap cleanup EXIT
if ! command -v kind >/dev/null 2>&1; then
  curl -fsSLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64
  curl -fsSLo /tmp/kind.sha256sum https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64.sha256sum
  expected=$(cut -d' ' -f1 /tmp/kind.sha256sum)
  echo "$expected  /tmp/kind" | sha256sum -c -
  chmod +x /tmp/kind
  export PATH="/tmp:$PATH"
fi
kind create cluster --name "$cluster" --wait 120s
kind load docker-image dmarc-ci:latest --name "$cluster"
docker run --rm --user root -v "$PWD:/workspace" -w /workspace dmarc-dev sh -c 'sh ops/install_helm.sh && "$HOME/.local/bin/helm" template dmarc-ci charts/zoho-dmarc-mcp --set image.digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | uv run --frozen python ops/fixture_manifest.py' > /tmp/dmarc-ci.yaml
kubectl --context kind-dmarc-ci apply -f /tmp/dmarc-ci.yaml
kubectl --context kind-dmarc-ci rollout status deployment/dmarc-ci --timeout=180s
pod=$(kubectl --context kind-dmarc-ci get pod -l app.kubernetes.io/instance=dmarc-ci -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-dmarc-ci exec "$pod" -c server -- python -c 'from zoho_dmarc.queries import Queries; q=Queries("/data/dmarc.sqlite"); assert q.summary("2024-10-04T00:00:00Z","2024-10-05T00:00:00Z")["totals"]["messages"]==12'
kubectl --context kind-dmarc-ci exec "$pod" -c server -- python -c 'import sqlite3; db=sqlite3.connect("file:/data/dmarc.sqlite?mode=ro",uri=True); exec("try:\n db.execute(\"DELETE FROM reports\")\nexcept sqlite3.OperationalError:\n pass\nelse:\n raise AssertionError(\"reader could write\")")'
kubectl --context kind-dmarc-ci delete pod "$pod" --wait=true
kubectl --context kind-dmarc-ci rollout status deployment/dmarc-ci --timeout=180s
pod=$(kubectl --context kind-dmarc-ci get pod -l app.kubernetes.io/instance=dmarc-ci -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-dmarc-ci exec "$pod" -c server -- python -c 'from zoho_dmarc.queries import Queries; assert Queries("/data/dmarc.sqlite").summary("2024-10-04T00:00:00Z","2024-10-05T00:00:00Z")["totals"]["messages"]==12'
