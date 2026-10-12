"""Download the public prompt-injection datasets into evals/data/ in one format.

    python -m evals.fetch_datasets

Every row becomes {"split": "dev" | "test", "label": "injection" | "benign", "text": ...}.
"dev" rows may be used for training and choosing thresholds; "test" rows are only ever scored.
See evals/data/README.md for sources, licenses and how each dataset was split.
"""

import io
import json
import random
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

DATA = Path(__file__).parent / "data"
PARQUET_INDEX = "https://datasets-server.huggingface.co/parquet?dataset={dataset}"
SEED = 7
SPML_SAMPLE = 4_000  # of 16,012 rows, stratified, to keep the repo small


def fetch(dataset: str, split: str, total: int) -> list[dict]:
    """Download a split as Parquet: one file from the Hub, instead of paging the rows API.

    (The rows API is a preview service; paging 16k rows through it hit HTTP 429 rate limits and
    502 gateway errors.)
    """
    with urllib.request.urlopen(PARQUET_INDEX.format(dataset=dataset), timeout=60) as response:
        files = [f["url"] for f in json.load(response)["parquet_files"] if f["split"] == split]
    rows = []
    for url in files:
        with urllib.request.urlopen(url, timeout=300) as response:
            rows += pq.read_table(io.BytesIO(response.read())).to_pylist()
    assert len(rows) == total, (dataset, split, len(rows), total)
    return rows


def write(name: str, rows: list[dict]) -> None:
    path = DATA / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8", newline="\n")
    injections = sum(r["label"] == "injection" for r in rows)
    by_split = {s: sum(r["split"] == s for r in rows) for s in ("dev", "test")}
    print(f"{name}: {len(rows)} rows ({injections} injections), {by_split}")


def jailbreak_classification() -> None:
    rows = []
    for split, ours, total in (("train", "dev", 1044), ("test", "test", 262)):
        for r in fetch("jackhhao/jailbreak-classification", split, total):
            label = {"jailbreak": "injection", "benign": "benign"}[r["type"]]
            rows.append({"split": ours, "label": label, "text": r["prompt"]})
    write("jailbreak_classification", rows)


def spml() -> None:
    """One 16k-row split upstream: take a stratified sample, then split it 80/20 ourselves."""
    source = fetch("reshabhs/SPML_Chatbot_Prompt_Injection", "train", 16_012)
    rng = random.Random(SEED)
    rows = []
    for label_value in (0, 1):
        group = [r for r in source if r["Prompt injection"] == label_value and r["User Prompt"]]
        share = round(SPML_SAMPLE * len(group) / len(source))
        sample = rng.sample(group, share)
        cut = int(len(sample) * 0.8)
        for i, r in enumerate(sample):
            rows.append(
                {
                    "split": "dev" if i < cut else "test",
                    "label": "injection" if label_value else "benign",
                    # Only the user prompt: that's what the gateway scans (system prompts are trusted).
                    "text": r["User Prompt"],
                }
            )
    rng.shuffle(rows)
    write("spml", rows)


def gandalf() -> None:
    """Real attacks from Lakera's Gandalf game. Attacks only, so it measures catch rate, and it is
    never trained on: all of it is "test", an attack set no model has seen."""
    rows = []
    for split, total in (("train", 777), ("validation", 111), ("test", 112)):
        rows += [
            {"split": "test", "label": "injection", "text": r["text"]}
            for r in fetch("Lakera/gandalf_ignore_instructions", split, total)
        ]
    write("gandalf", rows)


def main() -> None:
    DATA.mkdir(exist_ok=True)
    jailbreak_classification()
    spml()
    gandalf()


if __name__ == "__main__":
    main()
