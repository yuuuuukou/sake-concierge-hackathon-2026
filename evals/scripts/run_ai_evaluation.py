"""Run AI-assisted evaluation for collected Sake Concierge chat results.

The input is the JSONL produced by ``run_chat_batch.py``. This script converts
each successful chat result into the simple agent-data shape expected by
``azure.ai.evaluation.evaluate``:

    query, response, context, ground_truth

Use ``--dry-run`` first. A non-dry run calls the judge model and consumes tokens.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTEXT_DIR = REPO_ROOT / "backend" / "src" / "data" / "stores" / "fukunotomo"
DEFAULT_RESULTS_DIR = REPO_ROOT / "evals" / "results"
DEFAULT_EVALUATORS = ("relevance", "coherence", "fluency", "groundedness")
CONTEXT_EXTENSIONS = {".md", ".csv", ".json", ".jsonl", ".txt"}


def main() -> int:
    args = parse_args()
    source_records = load_jsonl(args.input_path)
    source_records = filter_records(source_records, args.case_id)
    if args.passed_transport_only:
        source_records = [
            record for record in source_records if record.get("passed_transport", True)
        ]
    if args.limit is not None:
        source_records = source_records[: args.limit]

    shared_context = load_context(args.context_path, max_chars=args.context_max_chars)
    eval_records = build_evaluation_records(source_records, shared_context=shared_context)
    eval_data_path = args.eval_data_output or default_eval_data_path(args.input_path)
    output_path = args.output or default_output_path(args.input_path)
    evaluation_name = args.evaluation_name or default_evaluation_name(args.input_path)

    print(f"input={args.input_path}")
    print(f"cases={len(eval_records)}")
    print(f"evaluators={', '.join(args.evaluators)}")
    print(f"eval_data={eval_data_path}")
    print(f"output={output_path}")
    if args.dry_run:
        preview = {
            "evaluation_name": evaluation_name,
            "upload_to_foundry": args.upload,
            "context_chars": len(shared_context),
            "first_case": summarize_preview_record(eval_records[0]) if eval_records else None,
        }
        print(json.dumps(preview, ensure_ascii=True, indent=2))
        return 0

    if not eval_records:
        raise ValueError("No evaluation records. Check --limit, --case-id, or input status.")

    eval_data_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(eval_data_path, eval_records)

    model_config = build_model_config(
        project_endpoint=args.project_endpoint or os.environ.get("AZURE_AIPROJECT_ENDPOINT"),
        azure_openai_endpoint=(
            args.azure_openai_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        ),
        deployment_name=args.deployment_name or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"),
        api_key=args.api_key or os.environ.get("AZURE_OPENAI_API_KEY"),
    )
    evaluators = create_evaluators(args.evaluators, model_config=model_config)
    azure_ai_project = build_azure_ai_project(args) if args.upload else None

    print(f"judge_model={model_config['azure_deployment']} @ {model_config['azure_endpoint']}")
    if azure_ai_project:
        print(f"foundry_project={azure_ai_project}")
    else:
        print("foundry_project=(not uploaded; pass --upload to send results to Foundry)")

    result = run_evaluate(
        eval_data_path=eval_data_path,
        evaluators=evaluators,
        evaluation_name=evaluation_name,
        output_path=output_path,
        azure_ai_project=azure_ai_project,
    )
    print_summary(result, output_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_path", type=Path, help="JSONL produced by run_chat_batch.py")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--eval-data-output", type=Path)
    parser.add_argument("--evaluation-name")
    parser.add_argument("--context-path", type=Path, default=DEFAULT_CONTEXT_DIR)
    parser.add_argument("--context-max-chars", type=int, default=80_000)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append")
    parser.add_argument(
        "--evaluators",
        nargs="+",
        choices=DEFAULT_EVALUATORS,
        default=list(DEFAULT_EVALUATORS),
    )
    parser.add_argument(
        "--include-transport-errors",
        dest="passed_transport_only",
        action="store_false",
        help="Also include records whose /chat transport failed. Default excludes them.",
    )
    parser.add_argument("--project-endpoint")
    parser.add_argument("--azure-openai-endpoint")
    parser.add_argument("--deployment-name")
    parser.add_argument("--api-key")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload results to Foundry Evaluation.",
    )
    parser.add_argument("--subscription-id", default=os.environ.get("AZURE_SUBSCRIPTION_ID"))
    parser.add_argument(
        "--resource-group",
        default=(
            os.environ.get("AZURE_RESOURCE_GROUP_NAME")
            or os.environ.get("AZURE_RESOURCE_GROUP")
        ),
    )
    parser.add_argument("--project-name", default=os.environ.get("AZURE_AI_PROJECT_NAME"))
    parser.add_argument(
        "--upload-legacy-workspace-scope",
        action="store_true",
        help=(
            "Upload using the legacy AML workspace triad instead of the Foundry "
            "project endpoint string."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.set_defaults(passed_transport_only=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {e}") from e
    return records


def filter_records(
    records: list[dict[str, Any]],
    case_ids: list[str] | None,
) -> list[dict[str, Any]]:
    if not case_ids:
        return records
    wanted = set(case_ids)
    return [
        record
        for record in records
        if record.get("case_id") in wanted or record.get("id") in wanted
    ]


def load_context(path: Path, *, max_chars: int) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Context path not found: {path}")
    if path.is_file():
        return trim_text(path.read_text(encoding="utf-8"), max_chars)

    parts: list[str] = []
    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix.lower() in CONTEXT_EXTENSIONS:
            text = child.read_text(encoding="utf-8")
            rel = child.relative_to(REPO_ROOT) if child.is_relative_to(REPO_ROOT) else child.name
            parts.append(f"# Source: {rel}\n\n{text}")
    return trim_text("\n\n".join(parts), max_chars)


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n\n[context truncated for evaluation]\n"
    return text[: max(0, max_chars - len(marker))] + marker


def build_evaluation_records(
    source_records: Iterable[dict[str, Any]],
    *,
    shared_context: str,
) -> list[dict[str, Any]]:
    eval_records: list[dict[str, Any]] = []
    for record in source_records:
        response = str(record.get("answer") or record.get("response") or "")
        eval_records.append({
            "case_id": record.get("case_id") or record.get("id"),
            "category": record.get("category"),
            "query": record["query"],
            "response": response,
            "context": build_case_context(record, shared_context=shared_context),
            "ground_truth": record.get("ground_truth") or "",
            "expected_products": record.get("expected_products", []),
            "evaluation_focus": record.get("evaluation_focus", []),
            "source_refs": record.get("source_refs", []),
            "run_id": record.get("run_id"),
            "agent_version": record.get("agent_version"),
            "model": record.get("model"),
            "revision": record.get("revision"),
        })
    return eval_records


def build_case_context(record: dict[str, Any], *, shared_context: str) -> str:
    expectation = {
        "ground_truth": record.get("ground_truth"),
        "expected_products": record.get("expected_products", []),
        "must_include": record.get("must_include", []),
        "must_not_include": record.get("must_not_include", []),
        "evaluation_focus": record.get("evaluation_focus", []),
        "source_refs": record.get("source_refs", []),
    }
    return (
        "## Store context\n"
        f"{shared_context}\n\n"
        "## Case expectation\n"
        f"{json.dumps(expectation, ensure_ascii=False, indent=2)}"
    )


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def summarize_preview_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": record.get("case_id"),
        "category": record.get("category"),
        "query": record.get("query"),
        "response_preview": str(record.get("response") or "")[:160],
        "context_chars": len(str(record.get("context") or "")),
        "ground_truth_preview": str(record.get("ground_truth") or "")[:160],
        "expected_products": record.get("expected_products", []),
    }


def build_model_config(
    *,
    project_endpoint: str | None,
    azure_openai_endpoint: str | None = None,
    deployment_name: str | None = None,
    api_key: str | None = None,
) -> dict[str, str]:
    if not project_endpoint and not azure_openai_endpoint:
        raise ValueError(
            "AZURE_AIPROJECT_ENDPOINT/--project-endpoint or "
            "AZURE_OPENAI_ENDPOINT/--azure-openai-endpoint is required."
        )
    if not deployment_name:
        raise ValueError("AZURE_OPENAI_DEPLOYMENT_NAME or --deployment-name is required.")
    endpoint = (
        azure_openai_endpoint.rstrip("/")
        if azure_openai_endpoint
        else project_endpoint.split("/api/projects/")[0].rstrip("/")
    )
    config = {
        "azure_endpoint": endpoint,
        "azure_deployment": deployment_name,
    }
    if api_key:
        config["api_key"] = api_key
    return config


def create_evaluators(
    evaluator_names: Iterable[str],
    *,
    model_config: dict[str, str],
) -> dict[str, Any]:
    from azure.ai.evaluation import (  # type: ignore[import-not-found]
        CoherenceEvaluator,
        FluencyEvaluator,
        GroundednessEvaluator,
        RelevanceEvaluator,
    )

    factories = {
        "relevance": RelevanceEvaluator,
        "coherence": CoherenceEvaluator,
        "fluency": FluencyEvaluator,
        "groundedness": GroundednessEvaluator,
    }
    return {
        name: factories[name](model_config, is_reasoning_model=True)
        for name in evaluator_names
    }


def build_azure_ai_project(args: argparse.Namespace) -> str | dict[str, str]:
    project_endpoint = args.project_endpoint or os.environ.get("AZURE_AIPROJECT_ENDPOINT")
    if project_endpoint and not args.upload_legacy_workspace_scope:
        return project_endpoint

    project_name = args.project_name or parse_project_name(project_endpoint)
    missing = [
        name
        for name, value in {
            "subscription_id": args.subscription_id,
            "resource_group": args.resource_group,
            "project_name": project_name,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(
            "--upload requires subscription/resource group/project. Missing: "
            + ", ".join(missing)
        )
    return {
        "subscription_id": args.subscription_id,
        "resource_group_name": args.resource_group,
        "project_name": project_name,
    }


def parse_project_name(project_endpoint: str | None) -> str | None:
    if not project_endpoint or "/api/projects/" not in project_endpoint:
        return None
    return project_endpoint.rstrip("/").split("/api/projects/")[-1] or None


def run_evaluate(
    *,
    eval_data_path: Path,
    evaluators: dict[str, Any],
    evaluation_name: str,
    output_path: Path,
    azure_ai_project: str | dict[str, str] | None,
) -> dict[str, Any]:
    from azure.ai.evaluation import evaluate  # type: ignore[import-not-found]

    kwargs: dict[str, Any] = {
        "data": str(eval_data_path),
        "evaluators": evaluators,
        "evaluation_name": evaluation_name,
        "output_path": str(output_path),
    }
    if azure_ai_project:
        kwargs["azure_ai_project"] = azure_ai_project
    return evaluate(**kwargs)


def print_summary(result: dict[str, Any], output_path: Path) -> None:
    print("\nAI evaluation summary")
    metrics = result.get("metrics", {})
    if metrics:
        for name, score in sorted(metrics.items()):
            print(f"- {name}: {score}")
    else:
        print("- metrics: (none returned)")
    if result.get("studio_url"):
        print(f"- Foundry: {result['studio_url']}")
    print(f"- output: {output_path}")


def default_eval_data_path(input_path: Path) -> Path:
    return DEFAULT_RESULTS_DIR / f"{input_path.stem}_ai_eval_input.jsonl"


def default_output_path(input_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_RESULTS_DIR / f"{input_path.stem}_ai_eval_{timestamp}.json"


def default_evaluation_name(input_path: Path) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"sake-concierge-{input_path.stem}-{timestamp}"


if __name__ == "__main__":
    raise SystemExit(main())
