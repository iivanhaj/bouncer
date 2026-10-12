"""Prometheus metrics (DESIGN.md §8).

Prometheus is for system health; per-team accounting lives in Postgres (/admin/usage).

Two rules keep label cardinality bounded, so a client can't blow up Prometheus's memory:
- No per-key labels: the number of keys has no limit.
- Labels never contain client-supplied text. Models are labelled by their models.yaml name,
  or "unlisted"; statuses and reasons come from fixed sets defined by the gateway.

Metrics are served on their own port (METRICS_PORT), never on the API port, so nothing
published for API traffic can expose them.
"""

from prometheus_client import Counter, Gauge, Histogram

# Request lifetimes on a CPU model run from milliseconds (blocked, mock) to minutes.
_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
_TTFT_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)

REQUESTS = Counter(
    "bouncer_requests_total",
    "Requests that got past the rate limit, by outcome.",
    ["status", "model"],
)
REJECTED = Counter(
    "bouncer_rejected_total",
    "Requests turned away before the pipeline (never logged to Postgres).",
    ["reason"],  # unauthorized | rate_limited | body_too_large
)
REQUEST_DURATION = Histogram(
    "bouncer_request_duration_seconds",
    "Whole request, gateway in to last byte out.",
    ["model", "stream"],
    buckets=_LATENCY_BUCKETS,
)
TIME_TO_FIRST_TOKEN = Histogram(
    "bouncer_time_to_first_token_seconds",
    "Streamed requests: gateway in to first content token out.",
    ["model"],
    buckets=_TTFT_BUCKETS,
)
TOKENS = Counter("bouncer_tokens_total", "Tokens through the gateway.", ["direction", "model"])
BLOCKED = Counter("bouncer_blocked_total", "Injection rule matches on blocked requests.", ["rule"])
REDACTIONS = Counter("bouncer_redactions_total", "Values redacted from prompts.", ["category"])
IN_FLIGHT = Gauge("bouncer_inflight_requests", "Requests currently holding a concurrency slot or streaming.")
USAGE_LOG_DROPPED = Counter("bouncer_usage_log_dropped_total", "Usage rows dropped instead of written.")
DEPENDENCY_ERRORS = Counter(
    "bouncer_dependency_errors_total",
    "Failures talking to Redis or Postgres.",
    ["dependency"],
)


def model_label(model: str, known: bool) -> str:
    """Only models.yaml names become label values; anything a client made up is 'unlisted'."""
    return model if known else "unlisted"
