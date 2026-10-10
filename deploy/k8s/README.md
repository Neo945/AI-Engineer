# Deploying coding-agent to Kubernetes

Kustomize manifests for the API gateway plus its PostgreSQL (pgvector) and
Redis dependencies. Everything is plain YAML; no Helm required.

```
deploy/k8s/
├── namespace.yaml          # coding-agent namespace
├── configmap.yaml          # non-secret environment
├── secret.example.yaml     # EXAMPLE secret (create the real one out of band)
├── postgres.yaml           # pgvector StatefulSet + headless Service
├── redis.yaml              # Redis Deployment + Service
├── api.yaml                # API Deployment (migrate initContainer) + Service
├── ingress.yaml            # ingress-nginx Ingress
└── kustomization.yaml      # ties it together, rewrites the image tag
```

## Prerequisites

- A cluster with a default `StorageClass` (the Postgres PVC needs it).
- `kubectl` and `kustomize` (or `kubectl -k`) on your machine.
- An image published by `.github/workflows/release.yml` to GHCR, or your own
  registry. The manifests reference `ghcr.io/your-org/coding-agent:latest`;
  override it with `kustomize edit set image`.

## 1. Create the namespace and secret

The database URL, auth secret, and LLM credentials are secrets and are **not**
committed. Create them out of band:

```sh
kubectl apply -f deploy/k8s/namespace.yaml

kubectl -n coding-agent create secret generic coding-agent-secrets \
  --from-literal=POSTGRES_USER=coding \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=POSTGRES_DB=coding_agent \
  --from-literal=AUTH_SECRET="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL="postgresql+asyncpg://coding:REPLACE@coding-agent-postgres:5432/coding_agent" \
  --from-literal=LLM_PROVIDER=openai \
  --from-literal=LLM_MODEL=big-pickle \
  --from-literal=LLM_BASE_URL=https://opencode.ai/zen/v1 \
  --from-literal=LLM_API_KEY=REPLACE
```

`secret.example.yaml` documents the expected keys. In production prefer the
External Secrets Operator or a sealed-secrets workflow so credentials are
reconciled from a vault.

## 2. Point the manifests at your image

```sh
cd deploy/k8s
kustomize edit set image ghcr.io/your-org/coding-agent=ghcr.io/acme/coding-agent:v0.1.0
```

## 3. Apply

```sh
kubectl apply -k deploy/k8s
kubectl -n coding-agent rollout status deploy/coding-agent-api
```

The API Deployment runs an `alembic upgrade head` **initContainer** before each
rollout, so migrations are applied exactly once per pod start.

## 4. Expose

Edit `ingress.yaml` to your host and controller, then:

```sh
kubectl -n coding-agent get ingress
```

Probes (already wired):

- Liveness: `GET /api/v1/healthz` — process is up.
- Readiness: `GET /api/v1/readyz` — Postgres **and** Redis reachable; returns
  `503` while degraded so the rollout drains bad pods.

## Configuration

Non-secret settings live in `configmap.yaml` (consumed via `envFrom`). Common
knobs:

| Key | Purpose |
| --- | --- |
| `EMBEDDING_PROVIDER` | `hash` (offline, default) or `openai` (needs `EMBEDDING_API_KEY`) |
| `RETRIEVAL_ENABLED` / `MEMORY_ENABLED` | Toggle context assembly / durable memory |
| `MCP_ENABLED` | Expose external MCP tools via `mcp_call` (also set `MCP_SERVERS_FILE`) |
| `OTEL_ENABLED` | Export OpenTelemetry traces/metrics |
| `SANDBOX_NETWORK_ENABLED` | Allow sandbox containers to reach the network (keep off) |

Anything in the Secret is injected as an environment variable too, so
`LLM_PROVIDER`, `LLM_API_KEY`, `DATABASE_URL`, and `AUTH_SECRET` come from there.

## Sandbox limitation

Terminal and test tools execute commands inside ephemeral Docker sandbox
containers (see `app/executor/`). A normal cluster pod has no container
runtime, so those two tools report a sandbox error under this deployment.
Filesystem and git tools, the LLM agent loop, retrieval, memory, and MCP all
work without it. To enable the sandbox in-cluster, mount a Docker socket
(commented example in `api.yaml`) or point the service at a dedicated executor
DaemonSet/sidecar.

## CI / CD

- `.github/workflows/ci.yml` — on every push/PR: `ruff check`,
  `ruff format --check`, `mypy app`, and the unit suite (`pytest -m "not
  integration"`). These need no database or Redis.
- `.github/workflows/release.yml` — on `v*` tags: builds `infra/Dockerfile`
  and pushes to GHCR with `latest`, the tag, and a short SHA.

## Teardown

```sh
kubectl delete -k deploy/k8s
kubectl delete namespace coding-agent   # removes the Secret and the PVCs
```
