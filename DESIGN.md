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

### Changes from the original plan
- Injection check runs **before** redaction (the first draft had the reverse).
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
- Rejected settings are never echoed in startup errors (`hide_input_in_errors`).
  Found while testing: Pydantic printed a too-short admin token in the error message.
- The auth lookup uses its own short database session, not one held for the whole
  request. A streaming response can last minutes, and holding a pooled connection
  that long would let a few slow streams exhaust the pool.
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
headroom        = max_concurrent × (MAX_BODY_BYTES + MAX_TOKENS_CAP + BUDGET_TOKEN_MARGIN)
effective_limit = monthly_token_budget − headroom
```
- A request is allowed if the tokens spent this month are below `effective_limit`.
- In-progress streams are allowed to finish. Their overshoot fits inside the
  headroom, so the **real budget is never exceeded**.
- At key creation, reject a budget smaller than `10 × headroom` (keeps headroom ≤ 10%).
- The dashboard and error messages show both the usable and the full budget.
- `monthly_token_budget = null` means unlimited.
- 429 with `code: "insufficient_quota"`.

- **Why `MAX_BODY_BYTES` bounds prompt tokens:** a token is at least one byte, and the
  JSON framing around each message (`{"role":"user","content":""}`) is larger than the
  chat template's extra tokens. The body size limit (pipeline step 1) enforces it.
- **`BUDGET_TOKEN_MARGIN` (16):** upstreams can report a token or so past `max_tokens`
  (Ollama 0.4.1 reported 16 for `max_tokens: 15`).

**Counter:** Redis key `budget:{key_id}:{YYYY-MM}`, increased when a request finishes.
Expires after ~40 days. A new month means a new key, so no reset job is needed. The
month comes from when the request *started*, for both the check and the count, so a
request running across midnight at month end is charged to one month only.

**Counting only adds to an existing counter** (a Lua `EXISTS` + `INCRBY`). If Redis lost
the counter between the check and the count, `INCRBY` would recreate it near zero and
hide the month's spending. Leaving it missing makes the next check rebuild it from Postgres.

**Proven by tests:** the counter is updated *before* the slot is released (swapping the
order makes `test_tokens_are_counted_before_the_slot_is_released` fail with `[0] == [42]`).

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
- **Context, not just checksums:** card and Aadhaar numbers must not be glued to a letter,
  digit or dash. A checksum alone passes ~10% of random numbers; found by a test where the
  last 12 digits of a UUID passed the Aadhaar checksum.
- **Performance, found by testing the whole path on 100 KB of hostile input:**
  - The PEM "BEGIN … END" regex was quadratic with many BEGINs and no END (0.5 s on 80 KB).
    Replaced by a linear search that stops at the first BEGIN with no END after it.
  - Overlap resolution compared each match with every chosen match: 5.3 s on `"1." * 50_000`
    (~12,500 IP-like matches). Now a per-character "taken" map: linear.
  - Replacement rebuilt the string once per match; now built in one pass.
  - The 32 KB body limit also caps redaction work per request.
- **Measured** (`python -m evals.redaction_eval`, 99 hand-written prompts): catch rate 92.5%,
  precision 98.7%, false-positive prompts 1/39 (2.6%). The misses are deliberate: obfuscation
  ("john at gmail dot com"), UK local phone format, bare 10-digit numbers, IPv6, dashless
  SSNs, free-form passwords. The one false positive is "1.2.3.4" used as a ratio.

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
- **Matched rules are logged even below the threshold** (`blocked_rules`, `injection_score`):
  that is the data for tuning weights later. Monitor mode logs would-be blocks with the
  request's normal status.
- **Error responses carry `x-request-id`**, so the reference in a 403 matches the log row.
- **Measured** (`python -m evals.injection_eval`). The headline number is the held-out
  test split of the public `deepset/prompt-injections` dataset (English and German), run once
  after the rules were written against dev data only:

  | Data | Catch rate | False positives |
  |---|---|---|
  | **Public test (held out)** | **14/60 (23%)** | **0/56 (0%)** |
  | Public dev (deepset's train split) | 73/203 (36%) | 0/343 (0%) |
  | Hand-written "test" | 20/25 (80%) | 1/25 (4%) |
  | Hand-written "dev" | 25/25 (100%) | 0/25 |

- **What the public dataset revealed:** my own test set reported 80%; the same rules caught
  **8%** of the public test set. The hand-written attacks were written by the person writing
  the rules, so they were the attacks the rules expected. Rewriting against the public dev
  split (adding German rules, "forget everything before that", dropping previous tasks,
  praise-then-pivot, prompt-text extraction, ignore-the-documents for retrieval apps) took
  the held-out number from 8% to 23% with no false positives. (A first version reached 25%,
  but one of its German rules matched any mention of "prompt text" and blocked an honest
  message in the test suite; narrowing it cost one held-out catch. 23% is the number to quote.)
- **Deliberately not chased:** "I want you to act as a Linux terminal / debater" (the dataset
  labels popular persona prompts as injections; blocking them would break normal use), and
  pure topic hijacks ("Write why newspaper X is the best"), which no text pattern can tell
  apart from a real request without knowing what the app is for.
- **Second layer: a small classifier** (`app/injection_model.py`). TF-IDF features (word 1-2
  grams and character 3-5 grams) into a logistic regression, trained on the dev splits of four
  public datasets (deepset, jackhhao jailbreak, SPML, hand-written; 4,889 prompts). Threshold from
  5-fold cross-validation at <=1% dev false positives. Blocks if the rules or the classifier fire;
  logs as rule `ml-classifier`. Full results: `evals/RESULTS.md`.

  | Held-out test set | Rules only | Rules + classifier |
  |---|---|---|
  | deepset | 23% | 45% (0% FP) |
  | jailbreak | 58% | 97% (0% FP) |
  | SPML | 18% | 97% (0% FP) |
  | Gandalf (never trained on) | 57% | 89% |

- **Why four datasets:** leave-one-dataset-out showed a classifier tuned to <=1% false positives
  on one kind of honest text blocked 68% of a kind it had never seen. Diverse honest training
  data was the fix. A multilingual embedding model and a 184M-parameter pretrained detector were
  measured and not shipped (`evals/RESULTS.md`).
- **Classifier monitor mode** (`injection_classifier_mode=monitor`): classifier-only hits are
  logged, not blocked, so its false-positive rate can be checked on real traffic first.
- **Stored as JSON, not pickle:** loading a pickle can execute code; the model file is just
  vocabularies, IDF weights, coefficients and the threshold.
- **Capped input, found by measuring:** classifier cost grows with input size, 43 ms for a
  32 KB prompt, so maximum-size prompts would have been a cheap way to burn the single
  process's CPU (~23 such requests a second saturate it). The classifier reads only the first
  and last 4 KB of each message (worst case now ~9 ms); the rules still scan everything. Known
  gap: an attack buried in the middle of a long prompt reaches only the rules.
- **The honest framing:** rules are a cheap first layer with zero false positives that stops
  the obvious attacks. The next layer would be a small classifier model; the real defense
  remains architectural (least privilege for tools, never trusting model output).
- **Known false positive:** quoting the attack while asking how to defend against it.
- **YAML trap found while building:** a rule field named `on` was read as the boolean `True`
  (YAML 1.1). Renamed to `form`.
- **Stated limitations:** rules cover English and German only. A heuristic layer, not a
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

- **Rows are written** for every request that gets past the rate limit and the request
  schema check. (Schema errors are rejected by FastAPI before the route runs, so they get a
  400 but no row; they will be counted in Prometheus.)
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
- **Token estimates** (when the upstream reports no usage, like Ollama 0.4.1 when streaming)
  lean high: prompt = content bytes / 3 + 10 per message + 30; completion = one token per
  streamed chunk. Rows built from estimates have `usage_estimated = true`. Measured: a real
  31-token prompt was estimated at 42.
- **Every request past the rate limit ends in exactly one `finish()`**, which (in order)
  adds tokens to the budget counter, releases the concurrency slot, and queues the row.
- **JSONB columns use `none_as_null`**, so a missing value is SQL `NULL`, not JSON `null`
  (found by a test: `WHERE redaction_counts IS NULL` would have missed those rows).
- **Future work:** a retention policy (monthly partitions or a 90-day cleanup job).

---

## 7. Admin API and dashboard

- **The dashboard is never publicly exposed** (localhost or
  `kubectl port-forward` only). It holds the admin token on its server and v1
  has no logins, so anyone who can reach it is the admin.
- **Three pages:** keys (create, revoke, expiry), usage, blocked requests.
- **Redesigned in week 2** (Next.js 16, shadcn/ui from its `dashboard-01` block, following the
  `frontend-design` skill). The first version was the generic default: five identical rounded
  stat cards with gradient washes and the stock neutral palette. The redesign spends its
  boldness on one element, a door tally splitting requests into served / blocked / failed, and
  keeps everything else quiet (hairline rules, no shadows). Colours carry meaning everywhere:
  `served` teal, `blocked` velvet-rope burgundy (also the brand), `failed` amber. One typeface,
  Archivo, using its width axis for the wordmark and headline numbers.
- **Moving to Next 16 also fixed two `postcss` advisories** (one high) reported by `npm audit`
  against Next 15.
- **Next.js (App Router).** Pages are server components that call the admin API; create and
  revoke are server actions. The API client imports `server-only`, so the build fails if it is
  ever pulled into browser code. Checked: the built browser bundles contain neither the token
  nor the variable name. Charts are plain SVG, no chart library.
- **Published on `127.0.0.1:3000` only** in Docker Compose, as a non-root user.
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
- **Metrics are served on their own port (9100), not the API port.** Nothing published for API
  traffic can expose them; in Docker Compose port 9100 is not published at all, and Prometheus
  scrapes it over the internal network. (Stronger than "remember to exclude `/metrics` from the
  ingress".) Tested: `/metrics` on the API port is a 404.
- **Labels never contain client-supplied text.** A `model` label taken from the request would
  let any client create unlimited time series; models are labelled by their `models.yaml` name
  or `unlisted`. Tested with an attacker-chosen model name.
- **Rejections before the pipeline** (401, 429, 413) have their own counter,
  `bouncer_rejected_total{reason}`, since they never reach the usage log.
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
  vs. batched logging; the measurement decides. **Done in week 3:** see `loadtest/RESULTS.md`.
  The profiler pointed somewhere else entirely (httpcore: a failing per-request import and
  connection-pool bookkeeping), and the fix helped p95 near the limit without raising capacity.

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
  **No on 0.4.1** (the option was ignored, so streamed rows were estimates). **Yes on 0.40.3**
  (re-tested in week 3: 37 prompt / 25 completion tokens reported). The estimate fallback stays
  for upstreams that don't report usage.
- [x] Does a client disconnect stop the response and run the `finally` block?
  **Yes.** A unit test proves that closing the relay closes the upstream response.
  Checked by hand: when a client was cut off at 1.3 s, Ollama logged that request as
  ending after ~0.8 s with no `[DONE]` sent, so it stopped generating instead of finishing.
- [x] Can kind pods reach Ollama on the host? **Yes**, via `host.docker.internal` on Docker Desktop.
  Docker Compose containers can: `host.docker.internal:11434` works from Docker Desktop.
- [ ] **For the budget (week 2):** Ollama 0.4.1 reported `completion_tokens: 16` for
  `max_tokens: 15`. Upstreams can exceed the cap by at least one token (likely the
  end-of-sequence token), so the headroom formula needs a small per-request margin.

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

**Added in week 2:**

13. Why does the usage log drop rows when its queue is full instead of waiting?
14. Why is the budget counter updated *before* the concurrency slot is released? (A test proves it.)
15. Why does `settle()` refuse to recreate a missing budget counter?
16. Why is the request body size limit part of the budget's guarantee?
17. Why does redaction check context (`(?<![\w-])`) as well as checksums?
18. Name two quadratic-time problems found in the redaction code, and how they were fixed.
19. Why did the rules score 80% on my own test set but 8% on a public one, and what does 23% with 0% false positives mean?
20. Why does the 403 for a blocked prompt not say which rule matched?
21. How does the dashboard keep the admin token out of the browser?
22. Why does YAML read a key named `on` as `True`, and where did it bite this project?
