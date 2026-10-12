# Bouncer

A self-hosted LLM gateway with security guardrails. Any OpenAI client can use it by changing
one URL: it checks who is calling, enforces per-key rate and concurrency limits, and streams
the model's answer back.

See [SPEC.md](SPEC.md) for the brief and [DESIGN.md](DESIGN.md) for every design decision,
its reasoning, and the loopholes it closes.

## Status

| Feature | State |
|---|---|
| OpenAI-compatible `/v1/chat/completions`, streaming and non-streaming | ✅ |
| Hashed per-team API keys, admin API (create / list / revoke, optional expiry) | ✅ |
| Sliding-window rate limit (Redis + Lua), per-key concurrency limit | ✅ |
| Docker Compose stack with a mock upstream for load tests | ✅ |
| Usage logging, monthly token budgets | Week 2 |
| Redaction of personal data, prompt-injection blocking | Week 2 |
| Dashboard, CI, Prometheus metrics, Kubernetes, load-test numbers | Weeks 2–3 |

## How a request flows

```
client ──Bearer key──▶ auth ─▶ rate limit ─▶ validate ─▶ concurrency slot ─▶ upstream (Ollama / mock)
                        401       429           400            429              stream back ◀──
```

## Run the whole stack in Docker

Needs Docker Desktop. For real answers, also Ollama running natively with a model pulled
(`ollama pull qwen2.5:0.5b`).

```powershell
copy .env.example .env      # then set BOUNCER_ADMIN_TOKEN (see the comment in the file)
docker compose --profile app up -d --build --wait
```

This starts Postgres, Redis, the migrations, the gateway on http://localhost:8000 and the
mock upstream on http://localhost:8001. [models.yaml](models.yaml) decides which model goes
where: `qwen2.5:0.5b` and `llama3.2:3b` go to Ollama on the host, and `mock` goes to the
mock upstream (fast and predictable, for load tests). Models not listed are rejected.

## Use it

Create a team key with the admin token. The full key is shown **once**:

```powershell
curl -X POST http://localhost:8000/admin/keys -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" -d "{\"team_name\": \"demo\", \"rpm_limit\": 60, \"max_concurrent\": 4}"
```

Call the model through the gateway, exactly like the OpenAI API:

```powershell
curl http://localhost:8000/v1/chat/completions -H "Authorization: Bearer <team key>" -H "Content-Type: application/json" -d "{\"model\":\"qwen2.5:0.5b\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}"
```

Or with the official OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="<team key>")
reply = client.chat.completions.create(model="qwen2.5:0.5b", messages=[{"role": "user", "content": "Say hi"}])
```

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | Chat, `stream: true` or `false` |
| `GET /v1/models` | Models the gateway routes to |
| `POST /admin/keys` | Create a key (returned once, stored as a SHA-256 hash) |
| `GET /admin/keys` | List keys (display prefix only) |
| `DELETE /admin/keys/{id}` | Revoke a key |
| `GET /healthz` | Liveness |

## Develop locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

docker compose up -d --wait   # just Postgres and Redis
alembic upgrade head
uvicorn app.main:create_app --factory --reload
```

(Stop the Docker gateway first if it's running, since both use port 8000.)

## Test

Tests use the `bouncer_test` database and Redis database 1, in the same containers.

```powershell
docker compose up -d --wait
pytest
```

## Security notes

- **Raw prompts are never stored.** Only the minimum needed for accounting will be logged.
- **API keys are stored only as SHA-256 hashes.** The full key is shown once, at creation.
- **The gateway refuses to start** without an admin token of at least 32 characters.
- **No TLS inside the gateway.** In a real deployment TLS ends at the ingress or load balancer.
