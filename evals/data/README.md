# Evaluation data

All files: one JSON object per line, `{"split": "dev" | "test", "label": "injection" | "benign",
"text": ...}`. "dev" rows may be used for training and choosing thresholds; "test" rows are only
ever scored. Re-create everything except the deepset file with `python -m evals.fetch_datasets`.
Texts are unchanged from the sources.

| File | Source | License | How it was split |
|---|---|---|---|
| `deepset_prompt_injections.jsonl` | [deepset/prompt-injections](https://huggingface.co/datasets/deepset/prompt-injections) (deepset GmbH) | apache-2.0 (card metadata) / cc-by-4.0 (dataset info) | train → dev, test → test. English and German. |
| `jailbreak_classification.jsonl` | [jackhhao/jailbreak-classification](https://huggingface.co/datasets/jackhhao/jailbreak-classification) | apache-2.0 | train → dev, test → test. `jailbreak` → injection. |
| `spml.jsonl` | [reshabhs/SPML_Chatbot_Prompt_Injection](https://huggingface.co/datasets/reshabhs/SPML_Chatbot_Prompt_Injection) | mit | One upstream split of 16,012 rows: a stratified random sample of 4,000 (seed 7), split 80/20 into dev/test. Only the user prompt is kept (the gateway scans user messages; system prompts are trusted). |
| `gandalf.jsonl` | [Lakera/gandalf_ignore_instructions](https://huggingface.co/datasets/Lakera/gandalf_ignore_instructions) (Lakera AI) | mit | All 1,000 rows are attacks, all "test": real attacks that no model is trained on. |

Note: ProtectAI's `deberta-v3-base-prompt-injection-v2` (used only as a ceiling reference, not
shipped) lists `jackhhao/jailbreak-classification` in its training data, so its jailbreak score is
contaminated and excluded from comparisons.
