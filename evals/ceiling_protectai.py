"""Score a public pretrained injection detector on the same held-out test sets, as a ceiling.

    python -m evals.ceiling_protectai

ProtectAI's deberta-v3-base-prompt-injection-v2 (Apache-2.0), run through ONNX Runtime. Not shipped
in the gateway: it is a reference for how much room is left. Its model card lists
jackhhao/jailbreak-classification among its training data, so that column is contaminated and
must not be compared; deepset, SPML and Gandalf are not in its training list.
"""

import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

from evals.injection_data import SCORED, load_all

REPO = "protectai/deberta-v3-base-prompt-injection-v2"
CONTAMINATED = {"jailbreak"}  # in the model's own training data


def main() -> None:
    model_path = hf_hub_download(REPO, "onnx/model.onnx")
    tokenizer = Tokenizer.from_file(hf_hub_download(REPO, "onnx/tokenizer.json"))
    tokenizer.enable_truncation(512)
    config = json.loads(Path(hf_hub_download(REPO, "onnx/config.json")).read_text())
    injection_index = next(int(i) for i, label in config["id2label"].items() if label.upper() == "INJECTION")
    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    input_names = {i.name for i in session.get_inputs()}

    def probability(text: str) -> float:
        encoding = tokenizer.encode(text)
        feeds = {"input_ids": np.array([encoding.ids], dtype=np.int64)}
        if "attention_mask" in input_names:
            feeds["attention_mask"] = np.array([encoding.attention_mask], dtype=np.int64)
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.array([encoding.type_ids], dtype=np.int64)
        logits = session.run(None, feeds)[0][0]
        exp = np.exp(logits - logits.max())
        return float(exp[injection_index] / exp.sum())

    data = load_all()
    timings, results = [], {}
    for target in [*SCORED, "gandalf"]:
        cases = [c for c in data[target] if c.split == "test"]
        flagged, labels = [], []
        for case in cases:
            start = time.perf_counter()
            flagged.append(probability(case.text) >= 0.5)
            timings.append((time.perf_counter() - start) * 1000)
            labels.append(case.label)
        flagged, labels = np.array(flagged), np.array(labels)
        injections, benign = (labels == 1).sum(), (labels == 0).sum()
        results[target] = {
            "recall": float((flagged & (labels == 1)).sum() / injections),
            "fp_rate": float((flagged & (labels == 0)).sum() / benign) if benign else None,
            "contaminated": target in CONTAMINATED,
        }
        fp = "" if results[target]["fp_rate"] is None else f" / FP {results[target]['fp_rate']:.1%}"
        note = "  (CONTAMINATED: in the model's training data)" if target in CONTAMINATED else ""
        print(f"  {target:<10} recall {results[target]['recall']:.0%}{fp}{note}", flush=True)
    print(f"  latency per prompt: median {np.median(timings):.0f} ms, p95 {np.percentile(timings, 95):.0f} ms (CPU)")
    out = Path(__file__).parent / "results" / "ceiling_protectai.json"
    out.write_text(json.dumps({"model": REPO, "results": results, "median_ms": float(np.median(timings))}, indent=2))


if __name__ == "__main__":
    main()
