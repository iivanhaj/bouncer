# Evaluation data

## deepset_prompt_injections.jsonl

From the [`deepset/prompt-injections`](https://huggingface.co/datasets/deepset/prompt-injections)
dataset on Hugging Face (deepset GmbH). The dataset card lists the license as `apache-2.0`
(card metadata) and `cc-by-4.0` (dataset info); both allow redistribution with attribution.

Converted to this repo's format: `label` 1 → `"injection"`, 0 → `"benign"`; the `train` split
is renamed `dev` (rules may be tuned against it) and `test` stays `test` (held out: reported,
never tuned against). The texts are unchanged. Part of the data is in German.
