# Evaluation Fixtures

This folder contains lightweight public evaluation helpers.

- `scripts/run_chat_batch.py` can send JSONL cases to a local or deployed `/chat` endpoint.
- `scripts/check_chat_results.py` adds simple keyword and transport checks.
- `scripts/run_ai_evaluation.py` can run Azure AI Evaluation when judge credentials are configured locally.

Keep paid or production evaluation results in `evals/results/`; the folder is ignored by Git.
