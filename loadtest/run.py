"""Measure the gateway's overhead and throughput with k6 (DESIGN.md §9).

For each request rate, the same load is sent to the mock upstream directly and to the gateway
in front of it; the difference is what the gateway costs. Each measurement is a warm-up run
followed by RUNS measured runs, and the median is reported.

Needs the Compose stack running (`docker compose --profile app up -d`) and MODELGUARD_ADMIN_TOKEN
in .env. k6 runs in Docker on the Compose network, so host networking doesn't add noise.

    python -m loadtest.run                      # default rates
    python -m loadtest.run --rates 50 100 200   # custom rates
    python -m loadtest.run --label after-cache  # tag a run (for before/after comparisons)
"""

import argparse
import json
import statistics
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "loadtest" / "results"
K6_IMAGE = "grafana/k6:0.54.0"
NETWORK = "modelguard_default"
TARGETS = {"direct": "http://mock-upstream:8001", "gateway": "http://gateway:8000"}
RUNS = 3


def admin_token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("MODELGUARD_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("MODELGUARD_ADMIN_TOKEN not found in .env")


def create_load_test_key() -> str:
    """A key with limits high enough that the test measures the gateway, not its rate limiter."""
    request = urllib.request.Request(
        "http://localhost:8000/admin/keys",
        data=json.dumps({"team_name": "load-test", "rpm_limit": 100_000, "max_concurrent": 1_000}).encode(),
        headers={"Authorization": f"Bearer {admin_token()}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)["key"]


def run_k6(target: str, rate: int, duration: str, key: str, stream: bool, out_name: str) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    command = [
        "docker", "run", "--rm", "--network", NETWORK,
        "-v", f"{ROOT / 'loadtest'}:/scripts:ro",
        "-v", f"{RESULTS}:/results",
        "-e", f"TARGET={target}", "-e", f"API_KEY={key}", "-e", f"RATE={rate}",
        "-e", f"DURATION={duration}", "-e", f"STREAM={'true' if stream else 'false'}",
        "-e", f"OUT=/results/{out_name}",
        K6_IMAGE, "run", "--quiet", "/scripts/chat.js",
    ]  # fmt: skip
    subprocess.run(command, check=True, capture_output=True)
    data = json.loads((RESULTS / out_name).read_text())
    (RESULTS / out_name).unlink()
    duration_ms = data["metrics"]["http_req_duration"]["values"]
    return {
        "p50": duration_ms["med"],
        "p95": duration_ms["p(95)"],
        "p99": duration_ms["p(99)"],
        "rps": data["metrics"]["http_reqs"]["values"]["rate"],
        "failed": data["metrics"]["http_req_failed"]["values"]["rate"],
    }


def measure(target: str, rate: int, key: str, stream: bool, duration: str) -> dict:
    run_k6(target, rate, "5s", key, stream, "warmup.json")  # warm caches and connection pools
    runs = [run_k6(target, rate, duration, key, stream, f"run{i}.json") for i in range(RUNS)]
    return {metric: statistics.median(r[metric] for r in runs) for metric in runs[0]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rates", type=int, nargs="+", default=[25, 50, 100, 200])
    parser.add_argument("--duration", default="15s")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()

    key = create_load_test_key()
    rows = []
    for rate in args.rates:
        direct = measure(TARGETS["direct"], rate, key, args.stream, args.duration)
        gateway = measure(TARGETS["gateway"], rate, key, args.stream, args.duration)
        row = {
            "rate": rate,
            "direct": direct,
            "gateway": gateway,
            "overhead_p50_ms": gateway["p50"] - direct["p50"],
            "overhead_p95_ms": gateway["p95"] - direct["p95"],
        }
        rows.append(row)
        print(
            f"{rate:>4} req/s | direct p50 {direct['p50']:7.1f} p95 {direct['p95']:7.1f} | "
            f"gateway p50 {gateway['p50']:7.1f} p95 {gateway['p95']:7.1f} | "
            f"overhead p50 {row['overhead_p50_ms']:+6.1f} p95 {row['overhead_p95_ms']:+6.1f} ms | "
            f"achieved {gateway['rps']:.0f} req/s, failed {gateway['failed']:.1%}",
            flush=True,
        )

    report = {
        "label": args.label,
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "stream": args.stream,
        "duration": args.duration,
        "runs_per_point": RUNS,
        "rows": rows,
    }
    path = RESULTS / f"{args.label}{'-stream' if args.stream else ''}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"saved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
