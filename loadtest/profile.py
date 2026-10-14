"""Profile the gateway under load, to find what to optimize (DESIGN.md §9: profile first).

Drives the gateway with k6 at a fixed rate and, meanwhile, samples the gateway process inside its
container with py-spy (from a throwaway container sharing its process namespace). Prints where
the CPU time goes: per function including what it calls ("inclusive"), and own time ("self").

    python -m loadtest.profile --rate 90
"""

import argparse
import collections
import subprocess
import time

from loadtest.run import K6_IMAGE, NETWORK, RESULTS, ROOT, create_load_test_key

GATEWAY_CONTAINER = "modelguard-gateway-1"


def frame_name(frame: str) -> str:
    """'handle (app/auth.py:31)' -> 'app/auth.py:handle'. Keeps the file so names are unambiguous."""
    name, _, location = frame.partition(" (")
    path = location.split(":")[0]
    return f"{path}:{name}"


def summarize(raw_path, top: int) -> None:
    inclusive, own = collections.Counter(), collections.Counter()
    total = 0
    for line in raw_path.read_text().splitlines():
        stack, _, count = line.rpartition(" ")
        if not stack:
            continue
        samples = int(count)
        frames = [frame_name(f) for f in stack.split(";")]
        total += samples
        own[frames[-1]] += samples
        for name in set(frames):
            inclusive[name] += samples
    print(f"{total} samples while busy\n\nInclusive (function plus everything it calls):")
    for name, samples in inclusive.most_common(top):
        print(f"  {samples / total:6.1%}  {name}")
    print("\nSelf (time in the function's own code):")
    for name, samples in own.most_common(top):
        print(f"  {samples / total:6.1%}  {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rate", type=int, default=90)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    key = create_load_test_key()
    load = subprocess.Popen(
        [
            "docker", "run", "--rm", "--network", NETWORK,
            "-v", f"{ROOT / 'loadtest'}:/scripts:ro", "-v", f"{RESULTS}:/results",
            "-e", "TARGET=http://gateway:8000", "-e", f"API_KEY={key}", "-e", f"RATE={args.rate}",
            "-e", f"DURATION={args.seconds + 15}s", "-e", "OUT=/results/profile-load.json",
            K6_IMAGE, "run", "--quiet", "/scripts/chat.js",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )  # fmt: skip
    time.sleep(8)  # let the load reach a steady state before sampling
    subprocess.run(
        [
            "docker", "run", "--rm", f"--pid=container:{GATEWAY_CONTAINER}", "--cap-add", "SYS_PTRACE",
            "-v", f"{RESULTS}:/out", "python:3.13-slim", "sh", "-c",
            f"pip install -q py-spy==0.4.0 2>/dev/null && py-spy record --pid 1 --duration {args.seconds} "
            "--rate 200 --format raw --output /out/profile.txt",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    _, errors = load.communicate()
    if load.returncode != 0:
        raise SystemExit(f"k6 failed: {errors.decode()[-500:]}")
    (RESULTS / "profile-load.json").unlink(missing_ok=True)
    summarize(RESULTS / "profile.txt", args.top)


if __name__ == "__main__":
    main()
