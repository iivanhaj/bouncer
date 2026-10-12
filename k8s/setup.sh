#!/usr/bin/env bash
# Create (or update) the local kind cluster and deploy Bouncer into it.
#
#   bash k8s/setup.sh
#
# Needs: Docker, kind, kubectl, and BOUNCER_ADMIN_TOKEN in .env. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=bouncer
INGRESS_NGINX=https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/kind/deploy.yaml

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config k8s/kind-config.yaml
fi
kubectl config use-context "kind-$CLUSTER"

echo "== ingress controller"
kubectl apply -f "$INGRESS_NGINX"
kubectl -n ingress-nginx wait --for=condition=ready pod -l app.kubernetes.io/component=controller --timeout=240s

echo "== images (built locally, loaded into the kind node)"
docker build -q -t bouncer-gateway:local .
docker build -q -t bouncer-dashboard:local dashboard
kind load docker-image bouncer-gateway:local bouncer-dashboard:local --name "$CLUSTER"

echo "== secrets and config (from .env and models.yaml; never committed)"
kubectl apply -f k8s/namespace.yaml
ADMIN_TOKEN=$(grep '^BOUNCER_ADMIN_TOKEN=' .env | cut -d= -f2-)
# Reuse the existing Postgres password: a new one would lock the gateway out of the existing volume.
PG_PASSWORD=$(kubectl -n bouncer get secret bouncer-secrets -o jsonpath='{.data.postgres-password}' 2>/dev/null | base64 -d || true)
if [ -z "$PG_PASSWORD" ]; then
  PG_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
fi
kubectl -n bouncer create secret generic bouncer-secrets \
  --from-literal=admin-token="$ADMIN_TOKEN" \
  --from-literal=postgres-password="$PG_PASSWORD" \
  --from-literal=database-url="postgresql+asyncpg://bouncer:${PG_PASSWORD}@postgres:5432/bouncer" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n bouncer create configmap gateway-models --from-file=models.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

echo "== deploy"
kubectl apply -k k8s
# Pick up newly loaded images even when the manifests didn't change.
kubectl -n bouncer rollout restart deployment/gateway deployment/mock-upstream deployment/dashboard
kubectl -n bouncer rollout status deployment/gateway --timeout=300s
kubectl -n bouncer rollout status deployment/dashboard --timeout=180s

cat <<'EOF'

Bouncer is running in kind.
  API (through the ingress):  http://localhost:8080/v1/chat/completions
  Dashboard:   kubectl -n bouncer port-forward svc/dashboard 3000:3000   -> http://127.0.0.1:3000
  Prometheus:  kubectl -n bouncer port-forward svc/prometheus 9090:9090  -> http://127.0.0.1:9090
EOF
