# Prompt-injection detection results

How well Model Guard's two layers (rules, then a small classifier) catch prompt injection, measured on
four public datasets. Reproduce with `python -m evals.injection_benchmark` and
`python -m evals.ceiling_protectai` (both need `pip install -e ".[research]"`); the shipped model is trained by
`python -m evals.train_injection_classifier`. Data and licenses: [data/README.md](data/README.md).

## Rules

All test splits are held out and only scored. Every number uses a **1% false-positive budget**:
each model's threshold is picked from cross-validation on its own training data so that at most
1% of honest dev prompts are blocked. A request is blocked if the rules **or** the classifier fire.

## The shipped model

Rules + TF-IDF classifier (word and character n-grams, logistic regression) trained on the dev
splits of all four datasets (4,889 prompts). Gandalf is never trained on.

| Held-out test set | Rules only | **Shipped** |
|---|---|---|
| deepset (n=116, English + German) | 23% | **45%**, 0% false positives |
| jailbreak (n=262) | 58% | **97%**, 0% false positives |
| SPML chatbot (n=801) | 18% | **97%**, 0% false positives |
| Gandalf real attacks (n=1,000, never trained on) | 57% | **89%** |

Cost: about 1 ms for a typical prompt, 15.6 ms worst case (32 KB, the body limit; the classifier
reads only the first and last 4 KB of each message).

## How we got here

1. **Rules, measured on a test set written by the rule author: 80%.** On the public deepset test
   set: **8%**. The hand-written attacks were the ones the author expected.
2. Rules rewritten against public dev data: 23% on deepset, 0% false positives.
3. Small classifier trained on deepset only: 48% on deepset.
4. **Leave-one-dataset-out** (the honest "unseen data" check): trained on three datasets, scored
   on the fourth. The deepset-style classifier tuned to <=1% false positives on its training data
   blocked **68% of the honest prompts** of a dataset it hadn't seen (long role-play and creative
   requests). A false-positive budget does not transfer to unfamiliar honest text.
5. Retrained on all four datasets' dev splits: the table above. Breadth of honest training data
   was the fix.

## Alternatives measured

Mean recall across the four held-out sets, rules included, 1% false-positive budget:

| Detector | In distribution | Unseen data (leave one dataset out) | Worst unseen false-positive rate |
|---|---|---|---|
| Rules only | 39% | 39% | 0% |
| **Rules + TF-IDF (shipped)** | **82%** | 65% | 68% |
| Rules + multilingual embedding model (MiniLM-L12) | 73% | 54% | 31% |

The embedding model (`paraphrase-multilingual-MiniLM-L12-v2`, head and tail chunks embedded
separately) caught less in every setting and would add ~150 MB to the image and 10-30 ms per
request, so it was not shipped.

## Ceiling reference (not shipped)

ProtectAI `deberta-v3-base-prompt-injection-v2` (184M parameters), 0.5 threshold, ONNX on CPU:

| deepset | SPML | Gandalf | jailbreak |
|---|---|---|---|
| 37%, 0% FP | 99%, **10.9% FP** | 100% | excluded: in its training data |

Median 26 ms, **p95 252 ms** per prompt on CPU. Two takeaways: deepset is hard for every
detector (many of its "injections" are topic hijacks that read like normal requests), and even a
dedicated model blocks a noticeable share of unfamiliar honest prompts.

## Limits, stated plainly

- Train and test splits come from the same four datasets; real traffic will differ. Run the
  classifier in **monitor mode** (`MODELGUARD_INJECTION_CLASSIFIER_MODE=monitor`) on your own traffic
  first: it logs what it would block (rule `ml-classifier`) without blocking, so its false-positive
  rate can be checked before it is trusted. Rule-based blocking is unaffected.
- The unseen-data numbers show the real risk is false positives on unfamiliar honest text, not
  missed attacks.
- Rules cover English and German only; the classifier saw mostly English.
- An attack buried in the middle of a very long prompt is seen by the rules but not the classifier.
