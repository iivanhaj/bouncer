# Bouncer — Design Decisions

This file records every design decision, the reasoning behind it, and the
loopholes it closes. Each section ends with a plain-language explanation for
interview prep.

Guiding principle: no design is free of weaknesses. Every weakness here is
**chosen, written down, and defensible**.

---

## Request pipeline

```
 1. Body size limit                → 413
 2. Auth                           → 401
 3. Rate limit (requests/min)      → 429
 4. Validate (model allowlist,
    field allowlist, n=1, caps)    → 400 / 404
 5. Injection check (raw text)     → 403
 6. Redaction
 7. Acquire concurrency slot       → 429
 8. Budget check                   → 429 insufficient_quota
 9. Upstream call (stream or not)
10. finally: add tokens to budget counter → release slot → queue usage row → metrics
```

**Ordering rules that keep the budget guarantee true:**
- Step 7 comes before step 8: every request that has passed the budget check
  but hasn't had its tokens counted must hold a slot.
- In step 10, tokens are counted before the slot is released, so the next
  request's budget check sees them.

Injection and redaction run before the slot is acquired, so slots are only
held by requests that will actually reach the model.

### Changes from SPEC.md
- Injection check runs **before** redaction (spec had the reverse).
- Added concurrency limit, key expiry, field allowlist, `/readyz`.
- Data model additions listed under "Data model".

---

## 1. Authentication

| Decision | Choice |
|---|---|
| Key format | `bnc_` + 32 random bytes (`secrets.token_bytes`) + 6-char checksum |
| Storage | SHA-256 of the key. HMAC-SHA256 with a server secret is the upgrade path |
| Lookup | `WHERE key_hash = ?` on a unique index |
| Display | First ~8 chars after `bnc_` stored as `key_prefix`, for display only |
| Caching | None in v1. Redis cache, deleted on revoke, is a week-3 optimization candidate |
| Errors | Missing, malformed, unknown, revoked and expired all return the **same** 401 in OpenAI error format |
| Admin | `ADMIN_TOKEN` env var. Refuse to start if missing or under 32 chars. Compare with `hmac.compare_digest`. Team keys can't use `/admin/*`; the admin token can't use `/v1/*` |
| Lifecycle | Key shown once at creation. Revocation is a soft delete (`revoked_at`). Rotation = create new + revoke old |
| Expiry | Optional `expires_in_days` at creation, no default |

**Why SHA-256 and not bcrypt?** bcrypt is slow on purpose, to stop people
guessing weak passwords. Our keys are 256 random bits, so they can't be
guessed. A slow hash would only add latency to every request.

**Why is lookup by hash safe from timing attacks?** Timing can only reveal
something about the hash, and knowing a hash doesn't help you build a key that
produces it.

**Why the `bnc_` prefix?** Secret scanners like GitHub's can recognize it if
a key is committed to a repo.

**Why is expiry optional rather than default?** Keys are used by machines.
Forced expiry with no warning system would cause surprise production outages.
Optional expiry lets teams limit the lifetime of keys they might forget about.

**Known trap:** when the key cache is added, cached entries must store
`expires_at` and check it on every cache hit.

**Leak prevention:**
- Keys are only accepted in the `Authorization` header, never in the URL.
- Headers and request bodies are never logged.
- TLS ends at the ingress in Kubernetes, and the README says so.
- Brute force is pointless at 256 bits. Rate-limiting failed auth by IP is out
  of scope (the ingress's job).

---

## 2. Rate limiting, concurrency and budget

Three limits, each protecting something different:

| Limit | Protects | Unit |
|---|---|---|
| Rate limit | Against bursts of requests | Requests per minute |
| Concurrency | Model capacity (a CPU model can only run a few generations at once) | Requests in progress |
| Budget | Cost | Tokens per calendar month (UTC) |

### Rate limit: sliding-window log
- Redis sorted set per key: member = request ID, score = timestamp.
- One **Lua script**: remove entries older than 60 s → count → add if allowed.
- Uses Redis's clock (`TIME`), not each pod's clock.
- Rejected requests are **not** recorded, so a client retrying in a loop
  doesn't lock itself out forever.
- 429 with `Retry-After` and OpenAI's `x-ratelimit-*` headers.

### Concurrency: `max_concurrent` per key (default 4)
- Redis sorted set of in-progress request IDs, scored by start time.
- Acquire: remove entries older than the cleanup age → count → add.
- Release in `finally`.
- Cleanup age must be longer than the total request timeout (300 s).
  Timestamped entries clean themselves up, so a crash can't leak slots forever
  (a plain `INCR`/`DECR` counter would).

### Budget: hard limit with headroom
```
headroom        = max_concurrent × (max_prompt_tokens + MAX_TOKENS_CAP)
effective_limit = monthly_token_budget − headroom
```
- A request is allowed if the tokens spent this month are below `effective_limit`.
- In-progress streams are allowed to finish. Their overshoot fits inside the
  headroom, so the **real budget is never exceeded**.
- At key creation, reject a budget smaller than `10 × headroom` (keeps headroom ≤ 10%).
- The dashboard and error messages show both the usable and the full budget.
- `monthly_token_budget = null` means unlimited.
- 429 with `code: "insufficient_quota"`.

**Counter:** Redis key `budget:{key_id}:{YYYY-MM}`, increased with `INCRBY`
when a request finishes. Expires after ~40 days. A new month means a new key,
so no reset job is needed.

**What counts:** prompt + completion tokens for successful requests; partial
tokens for client disconnects; whatever usage the upstream reports for
upstream errors; 0 for requests blocked by injection detection.

**If Redis loses data:** when the counter key is missing, rebuild it from
`SUM(tokens)` in Postgres for the month and write it with `SET NX`.
Accepted caveat: usage rows not yet written to Postgres are missed, a small
and rare undercount after an infrastructure failure.

### When Redis is down: fail closed
- Return 503, with a 100 ms Redis timeout.
- Auth already depends on Postgres. A security gateway shouldn't become
  "unlimited" when a dependency fails.
- Upgrade path: a per-pod in-memory fallback limiter.

**Plain-language explanations:**
- *Concurrency:* requests per minute counts how many requests start.
  Concurrency counts how many are running at once. Only concurrency protects
  a slow model.
- *Atomicity:* without a script, two requests' commands can interleave. Both
  read 59 of 60, both pass, and the limit is broken. A Lua script runs as one
  step, which prevents this.
- *Headroom:* the gateway decides before it knows the cost. So it stops
  accepting requests early enough that requests already in progress finish
  inside the real budget.

---

## 3. Redaction

**Scope:** prevents *accidental* leaks by honest users. It isn't designed to
stop someone deliberately getting around it ("a seatbelt, not a vault").

| Category | Detection |
|---|---|
| Email | Regex |
| Credit card | 13–19 digits with optional separators + **Luhn check** |
| Phone | Explicit list of formats; requires `+` or separators |
| National IDs | US SSN with its validity rules; others chosen explicitly, using their checksum where they have one |
| IPv4 | Regex + each octet ≤ 255 |
| Secrets | Known formats only: AWS `AKIA…`, `ghp_`, `sk-`, `xox…`, PEM private keys, JWTs, `bnc_` |
| Names, addresses | **Non-goal** (needs named-entity recognition, an ML model) |

- **Overlaps:** collect all matches and resolve by priority:
  secret > card > national ID > email > phone > IP. Replace from the end of
  the string backwards so earlier positions don't shift.
- **Placeholders:** typed and numbered (`[EMAIL_1]`). The same value gets the
  same placeholder across all messages. The mapping exists only for that
  request and is never stored.
- **One-way:** real values are never put back into responses in v1.
- **What's scanned:** every text string in `messages` for all roles, including
  tool-call arguments. Image parts are rejected with a 400 (they can't be
  redacted).
- **Output isn't redacted:** the model never saw the real values.
- **ReDoS:** no nested quantifiers, bounded repetitions, and a pytest timing
  test with 100 KB adversarial input per pattern. Upgrade path: `google-re2`.
- **Recorded:** counts per category (`redaction_counts`), never the values.

---

## 4. Injection detection

**Threat model:** team apps are trusted. Attackers are end users of those apps,
or content the apps pass along (web pages, documents, tool output).

- **Scanned roles:** everything except `system`.
  - `assistant` messages are sent by the client on every request and can be forged.
  - `system` is written by the trusted developer and would cause false positives.
- **Normalization (applied to a detection copy only):** NFKC, strip invisible
  format characters, map look-alike characters, lowercase, collapse whitespace,
  decode long base64 blocks once.
- **Rules are data:** each has an ID, category, pattern, weight, description
  and its own tests.
  - Strong: fake chat-template markers, instruction override.
  - Medium: system-prompt extraction, jailbreak personas.
  - Weak: "pretend", "role-play".
- **Scoring:** weights are added up; block above a threshold. Strong rules
  cross the threshold alone.
- **`INJECTION_MODE=block|monitor`:** monitor mode logs "would have blocked"
  without blocking, for trying new rules safely.
- **Response:** 403 `prompt_blocked` with a generic message and the
  `request_id`. The matched rule is never revealed to the client.
- **Logged:** rule IDs and score, never the text.
- **Measurement:** injection set split 25 (for writing rules) / 25 (for
  reporting). The normal set includes hard cases ("how do I ignore errors in
  Python").
- **Stated limitations:** English-only rules. A heuristic layer, not a
  complete defense. The real defense is giving the model's tools minimal
  access and never trusting model output.

---

## 5. Upstream and streaming proxy

- **Routing:** `models.yaml` maps each public model name to an upstream URL,
  the upstream model name, and prices. It is also the **allowlist**: an
  unknown model returns 404 `model_not_found`. `/v1/models` is generated
  from it.
- **Field allowlist** (replaces `extra="allow"`): `model`, `messages`,
  `stream`, `stream_options`, `max_tokens`, `max_completion_tokens`,
  `temperature`, `top_p`, `stop`, `seed`, `presence_penalty`,
  `frequency_penalty`, `response_format`.
  - Unknown fields → 400. `n > 1` → 400.
  - Missing max tokens → set to `MAX_TOKENS_CAP`. Above the cap → 400.
- **Headers are built from scratch.** Client headers are never copied, so team
  keys can't leak upstream. The gateway adds its own provider key for hosted
  upstreams.
- **Streaming:**
  - Check the upstream status before starting the response, so upstream
    errors become real HTTP errors.
  - Errors after streaming starts are sent as a final SSE `error` event.
  - Upstream bytes are passed through unchanged; `data:` lines are only
    parsed to read token counts.
  - The gateway always asks for usage (`include_usage`). The usage chunk is
    removed if the client didn't ask for it. If usage is missing, estimate it
    and set `usage_estimated = true`.
  - Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (nginx
    ingress buffers responses otherwise).
  - Client disconnect: `finally` closes the upstream connection (stopping
    generation), counts partial tokens, releases the slot and logs
    `client_disconnected`.
- **Timeouts:**

  | Timeout | Value |
  |---|---|
  | Connect | 5 s |
  | First token | 60 s |
  | Gap between chunks | 30 s |
  | Total request | 300 s (must be shorter than the concurrency cleanup age) |

- **Mock upstream:** a separate OpenAI-compatible service with configurable
  time to first token and delay per token. It reports exact usage.

---

## 6. Usage logging

- **Rows are written** for every request that gets past the rate limit.
  401s and 429s go to Prometheus only, so database writes per key never
  exceed the key's rate limit.
- **Writer:** an in-memory `asyncio.Queue` with a size limit, plus a
  background worker doing batched inserts (every 1 s or 100 rows, whichever
  comes first).
  - When the queue is full: drop the row and increment `usage_log_dropped_total`.
  - The queue is flushed on shutdown.
  - Accepted: up to one batch lost on a crash. Budget enforcement doesn't
    depend on this log.
- **`cost_usd`** is calculated when the row is written, using that moment's price.
- **Future work:** a retention policy (monthly partitions or a 90-day cleanup job).

---

## 7. Admin API and dashboard

- **The dashboard is never publicly exposed** (localhost or
  `kubectl port-forward` only). It holds the admin token on its server and v1
  has no logins, so anyone who can reach it is the admin.
- **Three pages:** keys (create, revoke, expiry), usage, blocked requests.
- **Next.js, time-boxed to 2 days.** Fallback: server-rendered pages from FastAPI.
- **`last_used_at`:** updated at most once a minute per key, or skipped in v1.
- **Audit trail:** key creation and revocation are written to the structured log.

---

## 8. Observability

- **Prometheus is for system health; Postgres is for per-team accounting.**
  No per-key Prometheus labels, because every label value creates its own
  time series and the number of keys has no limit.
- **Metrics:**
  - `requests_total{status,model}`
  - duration, time-to-first-token and upstream-duration histograms
  - `tokens_total{direction,model}`
  - `blocked_total{rule}`
  - `redactions_total{category}`
  - `ratelimited_total`
  - `inflight_requests`
  - `usage_log_dropped_total`
  - Redis and Postgres error counters
- **`/healthz` (liveness)** checks only that the process is alive.
  **`/readyz` (readiness)** checks Postgres and Redis.
- **`/metrics`** is reachable inside the cluster only, never through the ingress.
- **Logs** are structured JSON with `request_id`. Prompt text is never logged.

---

## 9. Deployment and measurement

- **docker-compose:** gateway, Postgres, Redis, mock upstream. Ollama runs
  natively on the host at `host.docker.internal:11434`.
- **kind:** load tests run against the in-cluster mock. Ollama is reached
  through a Service pointing at the host's IP (to be verified in week 3).
- **RAM:** WSL capped at 8 GB, plus Ollama (~3 GB for a 3B model). Don't run
  k6 and the 3B model at the same time.
- **Overhead:** k6 against the mock directly vs. the gateway in front of the
  mock, with the same load. Report p50/p95 differences, with a warm-up, 3 runs,
  and the median. Note that k6 and the gateway share a CPU.
- **Throughput:** the highest requests per second while p95 overhead stays
  under 50 ms. Run once with high-limit test keys and once with realistic
  limits.
- **Before/after:** profile first. Candidates are the key cache and per-row
  vs. batched logging; the measurement decides.

---

## Data model

**api_keys**: id, team_name, key_prefix, key_hash (unique), rpm_limit,
max_concurrent, monthly_token_budget (nullable = unlimited), created_at,
expires_at (nullable), revoked_at (nullable)

**requests**: id (= `x-request-id`), key_id, model, upstream, prompt_tokens,
completion_tokens, usage_estimated, cost_usd, latency_ms, ttft_ms, status,
blocked_rules, injection_score, redaction_counts (JSON), created_at.
Index on (key_id, created_at). **No prompt text.**

**status values:** `ok`, `blocked_injection`, `budget_exceeded`,
`concurrency_limited`, `upstream_error`, `upstream_timeout`,
`client_disconnected`, `bad_request`

**Config:** `ADMIN_TOKEN`, `UPSTREAM_*`, `MAX_TOKENS_CAP`, `MAX_BODY_BYTES`,
`REDIS_TIMEOUT_MS`, `INJECTION_MODE`, `models.yaml`

---

## To verify while building
- [x] Does Ollama's OpenAI-compatible API send usage when streaming (`include_usage`)?
  **No, on Ollama 0.4.1**: the option is ignored and no usage chunk is sent. The
  estimate fallback (`usage_estimated = true`) is required. Re-test after upgrading Ollama.
- [x] Does a client disconnect stop the response and run the `finally` block?
  **Yes.** A unit test proves that closing the relay closes the upstream response.
  Checked by hand: when a client was cut off at 1.3 s, Ollama logged that request as
  ending after ~0.8 s with no `[DONE]` sent, so it stopped generating instead of finishing.
- [ ] Can kind pods reach Ollama on the host? (week 3)

---

## Self-check (interview prep)

Answer each in one sentence, in your own words:

1. Why SHA-256 and not bcrypt for API keys?
2. Why does every auth failure return the same 401?
3. Why do we need a concurrency limit as well as requests per minute?
4. Why must the rate limiter run as one Lua script?
5. How does headroom make the budget a hard limit, and what does it cost?
6. **Why must the concurrency slot be taken before the budget check?** (review)
7. Why is redaction "a seatbelt, not a vault"?
8. Why scan `assistant` messages but not `system` messages?
9. Why is `extra="allow"` dangerous for the budget?
10. **Why must the liveness check not depend on Redis?** (review)
11. Why are 401s and 429s not written to Postgres?
12. Why are there no per-key labels in Prometheus?
