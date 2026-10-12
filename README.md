# Bouncer

A self-hosted LLM gateway with security guardrails. See [SPEC.md](SPEC.md) for the brief
and [DESIGN.md](DESIGN.md) for every design decision and its reasoning.

## Run locally (Windows, PowerShell)

Needs Docker Desktop (for Postgres) and Ollama running natively.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

copy .env.example .env      # then set BOUNCER_ADMIN_TOKEN (see the comment in the file)
docker compose up -d --wait # Postgres
alembic upgrade head        # create the tables

uvicorn app.main:create_app --factory --reload
```

Then open http://localhost:8000/healthz.

## Use it

Create a team key with the admin token (the full key is shown **once**):

```powershell
curl -X POST http://localhost:8000/admin/keys -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" -d "{\"team_name\": \"demo\"}"
```

Call the model through the gateway, exactly like the OpenAI API:

```powershell
curl http://localhost:8000/v1/chat/completions -H "Authorization: Bearer <team key>" -H "Content-Type: application/json" -d "{\"model\":\"qwen2.5:0.5b\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}]}"
```

## Test

Tests run against the `bouncer_test` database in the same Postgres container.

```powershell
docker compose up -d --wait
pytest
```
