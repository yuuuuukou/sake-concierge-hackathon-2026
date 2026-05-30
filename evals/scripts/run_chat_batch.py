"""Run golden evaluation cases against the Sake Concierge /chat endpoint.

The script collects real app responses. A later step can feed the output JSONL
to Foundry Evaluation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from check_chat_results import build_simple_check

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "evals" / "golden" / "public_demo_golden.jsonl"
DEFAULT_BASE_URL = "http://127.0.0.1:8010"


@dataclass(frozen=True)
class SseEvent:
    """One parsed SSE event."""

    event: str
    data: str


@dataclass(frozen=True)
class ChatResult:
    """Result of one /chat execution."""

    answer: str
    conversation_id: str | None
    http_status: int
    latency_ms: int
    latency_meta_ms: int | None
    latency_first_delta_ms: int | None
    error_event: str | None


def main() -> int:
    args = parse_args()
    cases = load_jsonl(args.input_path)
    cases = filter_cases(cases, args.case_id)
    if args.limit is not None:
        cases = cases[: args.limit]

    output_path = args.output or default_output_path(args.input_path)
    run_metadata = {
        "run_id": args.run_id or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ"),
        "base_url": args.base_url.rstrip("/"),
        "agent_version": args.agent_version or os.environ.get("AZURE_AGENT_VERSION"),
        "model": args.model or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"),
        "data_version": args.data_version,
        "revision": args.revision or os.environ.get("CONTAINER_APP_REVISION"),
    }

    if args.dry_run:
        print(f"dry_run cases={len(cases)} input={args.input_path}")
        print(json.dumps(run_metadata, ensure_ascii=False))
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"input={args.input_path}")
    print(f"base_url={run_metadata['base_url']}")
    print(f"output={output_path}")
    print(f"cases={len(cases)} delay_sec={args.delay_sec}")

    shared_conversation_id: str | None = None
    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['id']} {case['category']}")
            started_at = datetime.now(UTC)
            result = call_chat(
                base_url=run_metadata["base_url"],
                message=case["query"],
                conversation_id=shared_conversation_id,
                timeout_sec=args.timeout_sec,
            )
            finished_at = datetime.now(UTC)
            if args.continue_conversation and result.conversation_id:
                shared_conversation_id = result.conversation_id

            record = build_result_record(
                case=case,
                result=result,
                run_metadata=run_metadata,
                started_at=started_at,
                finished_at=finished_at,
            )
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()

            if result.error_event:
                print(f"  error={result.error_event}")
            else:
                print(
                    "  conversation_id="
                    f"{result.conversation_id} first_delta_ms={result.latency_first_delta_ms} "
                    f"latency_ms={result.latency_ms}"
                )

            if index < len(cases) and args.delay_sec > 0:
                time.sleep(args.delay_sec)

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "--dataset", dest="input_path", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--base-url", default=os.environ.get("SAKE_CONCIERGE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--agent-version")
    parser.add_argument("--model")
    parser.add_argument("--data-version")
    parser.add_argument("--revision", "--app-revision", dest="revision")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--timeout-sec", type=int, default=90)
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=21.0,
        help="Default respects the app's 3 requests/minute demo rate limit.",
    )
    parser.add_argument(
        "--continue-conversation",
        action="store_true",
        help="Reuse conversation_id across cases. Default is one fresh conversation per case.",
    )
    parser.add_argument("--dry-run", action="store_true")
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


def filter_cases(cases: list[dict[str, Any]], case_ids: list[str] | None) -> list[dict[str, Any]]:
    if not case_ids:
        return cases
    wanted = set(case_ids)
    return [case for case in cases if case.get("id") in wanted]


def default_output_path(input_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "evals" / "results" / f"{input_path.stem}_{timestamp}.jsonl"


def build_result_record(
    *,
    case: dict[str, Any],
    result: ChatResult,
    run_metadata: dict[str, str | None],
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    status = "ok" if result.error_event is None and result.http_status < 400 else "error"
    record = {
        "run_id": run_metadata["run_id"],
        "case_id": case["id"],
        "store_id": case.get("store_id"),
        "dataset_version": case.get("dataset_version"),
        "category": case.get("category"),
        "language": case.get("language"),
        "query": case["query"],
        "answer": result.answer,
        "ground_truth": case.get("ground_truth"),
        "expected_products": case.get("expected_products", []),
        "must_include": case.get("must_include", []),
        "must_not_include": case.get("must_not_include", []),
        "evaluation_focus": case.get("evaluation_focus", []),
        "source_refs": case.get("source_refs", []),
        "conversation_id": result.conversation_id,
        "agent_version": run_metadata["agent_version"],
        "model": run_metadata["model"],
        "data_version": run_metadata["data_version"],
        "revision": run_metadata["revision"],
        "base_url": run_metadata["base_url"],
        "status": status,
        "latency_ms": result.latency_ms,
        "latency_meta_ms": result.latency_meta_ms,
        "latency_first_delta_ms": result.latency_first_delta_ms,
        "error_event": result.error_event,
        "http_status": result.http_status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "passed_transport": status == "ok",
    }
    record["simple_check"] = build_simple_check(record)
    return record


def call_chat(
    *,
    base_url: str,
    message: str,
    conversation_id: str | None,
    timeout_sec: int,
) -> ChatResult:
    started = time.perf_counter()
    url = f"{base_url.rstrip('/')}/chat"
    payload: dict[str, str] = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    answer_parts: list[str] = []
    response_conversation_id: str | None = conversation_id
    latency_meta_ms: int | None = None
    latency_first_delta_ms: int | None = None
    http_status = 0

    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            http_status = response.status
            for event in parse_sse_lines(iter_response_lines(response)):
                if event.event == "meta":
                    latency_meta_ms = latency_meta_ms or elapsed_ms(started)
                    response_conversation_id = (
                        parse_conversation_id(event.data) or response_conversation_id
                    )
                elif event.event == "delta":
                    latency_first_delta_ms = latency_first_delta_ms or elapsed_ms(started)
                    answer_parts.append(event.data)
                elif event.event == "error":
                    return ChatResult(
                        answer="".join(answer_parts),
                        conversation_id=response_conversation_id,
                        http_status=http_status,
                        latency_ms=elapsed_ms(started),
                        latency_meta_ms=latency_meta_ms,
                        latency_first_delta_ms=latency_first_delta_ms,
                        error_event=event.data or "SSE error event",
                    )
                elif event.event == "done":
                    break
    except urllib.error.HTTPError as e:
        http_status = e.code
        return ChatResult(
            answer="".join(answer_parts),
            conversation_id=response_conversation_id,
            http_status=http_status,
            latency_ms=elapsed_ms(started),
            latency_meta_ms=latency_meta_ms,
            latency_first_delta_ms=latency_first_delta_ms,
            error_event=e.read().decode("utf-8", errors="replace"),
        )
    except Exception as e:  # noqa: BLE001 - CLI report should capture transport errors.
        return ChatResult(
            answer="".join(answer_parts),
            conversation_id=response_conversation_id,
            http_status=http_status,
            latency_ms=elapsed_ms(started),
            latency_meta_ms=latency_meta_ms,
            latency_first_delta_ms=latency_first_delta_ms,
            error_event=str(e),
        )

    return ChatResult(
        answer="".join(answer_parts),
        conversation_id=response_conversation_id,
        http_status=http_status,
        latency_ms=elapsed_ms(started),
        latency_meta_ms=latency_meta_ms,
        latency_first_delta_ms=latency_first_delta_ms,
        error_event=None,
    )


def iter_response_lines(response: Any) -> Iterable[str]:
    for raw_line in response:
        yield raw_line.decode("utf-8", errors="replace").rstrip("\r\n")


def parse_sse_lines(lines: Iterable[str]) -> Iterable[SseEvent]:
    event_name = "message"
    data_lines: list[str] = []

    for line in lines:
        if line == "":
            if data_lines:
                yield SseEvent(event=event_name, data="\n".join(data_lines))
            event_name = "message"
            data_lines = []
            continue

        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
            continue
        if line.startswith("data:"):
            data_lines.append(strip_sse_data_prefix(line))

    if data_lines:
        yield SseEvent(event=event_name, data="\n".join(data_lines))


def strip_sse_data_prefix(line: str) -> str:
    data = line.removeprefix("data:")
    return data[1:] if data.startswith(" ") else data


def parse_conversation_id(data: str) -> str | None:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    conversation_id = payload.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) else None


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


if __name__ == "__main__":
    sys.exit(main())
