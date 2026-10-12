# Load test results

What the gateway costs on top of the model, how much it can take, and what profiling found.

## Method

- **Target:** the mock upstream (`mock_upstream/`), which answers every chat completion in
  ~292 ms (100 ms to first token, 20 tokens at 10 ms). A real CPU model would be the bottleneck
  and hide the gateway's own cost, so a fixed-timing mock isolates it.
- **Overhead** = the same load sent to the mock directly and through the gateway; the difference
  in p50 and p95 is the gateway.
- **Load:** k6 0.54, constant arrival rate (requests keep arriving on schedule even when
  responses slow down, like real traffic), non-streaming requests.
- **Each point:** 5 s warm-up, then 3 runs of 15 s, median reported (`loadtest/run.py`).
- **Key:** limits set very high, so the test measures the gateway, not its rate limiter.
- **Setup:** Docker Compose on one Windows 11 laptop (16 logical cores, Docker Desktop on WSL2).
  k6, gateway, mock, Postgres and Redis share the same CPU, and the gateway is **one Python
  process**. OneDrive syncs the repo folder in the background (~3% CPU when measured).

## Baseline

| Rate | Overhead p50 | Overhead p95 | Achieved |
|---|---|---|---|
| 25/s | +11.7 ms | +14.8 ms | 26/s |
| 50/s | +13.7 ms | +25.0 ms | 53/s |
| 100/s | +95 ms | +498 ms | 102/s |
| 200/s | +3.1 s | +12.6 s | 105/s |
| 400/s | +9.5 s | +19.9 s | 104/s |

Below its limit the gateway adds **~12–14 ms at p50**. The single process saturated at ~105 req/s
in this run. (See "variance" below: the same code reached ~180 req/s in a later session.)

## Profile: where the CPU goes

`loadtest/profile.py` drives 90 req/s and samples the gateway with py-spy, attached from a
throwaway container that shares the gateway container's process namespace.

- 3,960 of 4,000 possible samples were busy: the process was **CPU-bound**.
- The suspected costs barely registered: injection rule matching was 0.9%.
- **41%** of CPU was inside the HTTP call to the upstream, in httpcore (httpx's transport):
  - **18.3%** connection-pool bookkeeping (`_assign_requests_to_connections`), which scans the
    pool on every request and every response close.
  - **7.1%** `current_async_library()`, which does `import sniffio` on every lock it creates.
    `sniffio` wasn't installed, and Python never caches a *failed* import, so it searched the
    filesystem on every request.

The design doc had guessed the per-request Postgres key lookup would be the bottleneck. It wasn't.

## Before/after: installing sniffio

The fix is one dependency (`sniffio`). To compare fairly, both sides ran **back to back on the
same image** on a quiet machine: A removes `sniffio` inside the running container and restarts
it; B recreates the container from the image. Each side checks the package is actually absent or
present before measuring. (A first attempt overlapped a 1.46 GB download and install on the same
machine and was discarded.)

| Rate | A: without, overhead p50 / p95 | B: with, overhead p50 / p95 | Achieved A → B |
|---|---|---|---|
| 25/s | +12.5 / +16.0 ms | +10.4 / +14.0 ms | 25 → 25 |
| 50/s | +13.5 / +17.2 ms | +12.7 / +17.6 ms | 49 → 49 |
| 100/s | +10.6 / **+135 ms** | +13.2 / **+81 ms** | 102 → 106 |
| 200/s | +331 ms / +2.9 s | +365 ms / +4.7 s | 186 → 176 |
| 400/s | +5.3 s / +15.6 s | +8.3 s / +19.9 s | 162 → 115 |

**Reading it honestly:**
- Below the limit, B is slightly better: about 2 ms at p50 at 25/s, and **54 ms lower p95 at
  100/s**, where the process is close to saturation and wasted CPU turns into queueing.
- It did **not** raise capacity: the limit stays around 160–190 req/s.
- Beyond the limit (200–400/s) results swing in both directions between runs. Overload is
  dominated by queues and timeouts, so those points measure chaos, not the fix.
- **Variance between sessions is large:** the baseline run (same code as A) capped at ~105 req/s,
  while A reached ~186 req/s. Only back-to-back runs on the same image are comparable.

So the profiler found real waste, the measurement showed it wasn't the bottleneck, and the fix
stays (it is free and helps p95 near the limit).

## Kubernetes (kind): 2 gateway replicas

A quick look rather than the full method: k6 ran as a pod inside the kind cluster against the
in-cluster mock, directly and through the gateway service (2 replicas), one 15 s run per point.

| Rate | Mock direct p95 | Gateway p50 / p95 (overhead) | Achieved |
|---|---|---|---|
| 100/s | 293 ms | 309 / 328 ms (+17 / +34 ms) | 98/s |
| 200/s | 293 ms | 1.8 s / 7.2 s | 144/s |
| 400/s | 292 ms | 5.8 s / 14.9 s | 150/s |

- At 100 req/s, two replicas clearly beat one process: +34 ms p95 overhead against +81 ms.
- Capacity did **not** double (~150 req/s). Both pods, k6, Postgres, Redis and the mock all run
  inside one kind node on one laptop, so replicas spread the work without adding CPU. Finding
  the shared limit (node CPU, Postgres, Redis) is the next investigation.

Also verified in the cluster: pods reach Ollama on the Windows host (`host.docker.internal`),
streaming through the nginx ingress is not buffered (first chunk at 0.48 s of a 0.94 s
generation), the admin API is not reachable through the ingress (404), two replicas enforce one
shared rate limit (exactly 5 of 10 requests allowed across both pods with a 5/min key), and
Prometheus scrapes each pod separately.

## What's next

- The remaining ceiling is httpcore's pool bookkeeping plus a single Python process. Options:
  tune or replace the upstream HTTP client, and run several gateway processes (Kubernetes runs 2
  replicas; their limits are shared through Redis).
- Measure through Kubernetes (2 replicas) against the in-cluster mock.

## Raw data

`results/baseline.json`, `results/ab-without-sniffio.json`, `results/ab-with-sniffio.json`, and
the py-spy samples in `results/profile.txt`.
