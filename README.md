# Bouncer

A self-hosted LLM gateway with security guardrails. See [SPEC.md](SPEC.md) for the brief
and [DESIGN.md](DESIGN.md) for every design decision and its reasoning.

## Run locally (Windows, PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Then open http://localhost:8000/healthz.

## Test

```powershell
pytest
```
