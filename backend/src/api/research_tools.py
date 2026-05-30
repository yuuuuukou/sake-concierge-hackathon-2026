"""Foundry function tools used by the Sake Concierge agent."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import nullcontext
from hashlib import sha256
from typing import Any

from azure.ai.projects.models import FunctionTool
from azure.core.exceptions import AzureError
from openai import OpenAIError

try:
    from opentelemetry import trace as otel_trace
except ImportError:  # pragma: no cover - optional runtime instrumentation.
    otel_trace = None

logger = logging.getLogger("sake_concierge")

BRAND_RESEARCH_TOOL_NAME = "research_sake_brand"
MAX_BRAND_NAME_LENGTH = 80
MAX_USER_CONTEXT_LENGTH = 500
BRAND_RESEARCH_RETRY_DELAYS_SECONDS = (1.0, 2.0)
DEFAULT_TRACE_CONTENT_MAX_CHARS = 500
URL_PATTERN = re.compile(r"https?://[^\s)）\]】>\"']+")


BRAND_RESEARCH_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "brand_name": {
            "type": "string",
            "description": "ユーザーが普段飲む、または比較したい日本酒の銘柄名。",
        },
        "user_context": {
            "type": "string",
            "description": "銘柄名の前後にある、好み・飲用シーン・料理などの補足。",
        },
    },
    "required": ["brand_name", "user_context"],
    "additionalProperties": False,
}


def build_brand_research_tool() -> FunctionTool:
    """Create the function tool schema registered on the main Foundry Agent."""
    return FunctionTool(
        name=BRAND_RESEARCH_TOOL_NAME,
        description=(
            "店舗の取扱い酒リストにない日本酒銘柄・蔵元・ブランドを、"
            "ユーザーが普段飲む/好き/比較したい/どんな酒か知りたい文脈で挙げた時に必ず使う。"
            "有名銘柄だけでなく、飛鸞、産土、あべ、花陽浴のような未知または表記ゆれのある"
            "銘柄も対象。味わい傾向、香り、甘辛、酸、旨み、飲用シーンを確認し、"
            "店舗内商品の推薦へ変換するための比較材料にする。"
        ),
        parameters=BRAND_RESEARCH_TOOL_PARAMETERS,
        strict=True,
    )


def execute_brand_research_tool(
    *,
    openai_client: Any,
    brand_research_agent_ref: Any | None,
    arguments: str,
    trace_context: dict[str, Any] | None = None,
) -> str:
    """Execute `research_sake_brand` and return a JSON string for Foundry."""
    trace_context = trace_context or {}
    trace_id = str(trace_context.get("trace_id") or uuid.uuid4())
    parsed = parse_brand_research_arguments(arguments)
    if parsed["status"] != "ok":
        log_brand_research_trace(
            "brand_research.trace.invalid_arguments",
            trace_id=trace_id,
            status=parsed["status"],
            context=trace_context,
        )
        return json.dumps(parsed, ensure_ascii=False)

    brand_name = parsed["brand_name"]
    user_context = parsed.get("user_context")
    intended_search_query = build_brand_research_query(brand_name)
    base_trace = {
        "trace_id": trace_id,
        "brand_name": brand_name,
        "intended_search_query": intended_search_query,
        "actual_web_search_query_visible": False,
        "context": trace_context,
    }
    log_brand_research_trace("brand_research.trace.start", **base_trace)

    if brand_research_agent_ref is None:
        log_brand_research_trace(
            "brand_research.trace.agent_not_configured",
            **base_trace,
            status="research_agent_not_configured",
        )
        return json.dumps(
            {
                "status": "research_agent_not_configured",
                "brand_name": brand_name,
                "trace": build_trace_payload(
                    trace_id=trace_id,
                    intended_search_query=intended_search_query,
                    actual_web_search_query_visible=False,
                    status="research_agent_not_configured",
                ),
                "summary": (
                    "外部銘柄リサーチ用の Foundry Agent はまだ設定されていません。"
                    "この銘柄について断定せず、ユーザーに甘み・酸・香り・飲み口など"
                    "好きな点を確認してください。"
                ),
                "usage_policy": (
                    "他銘柄は好み推定の材料に限定し、推薦は取扱い酒リスト内から行うこと。"
                ),
            },
            ensure_ascii=False,
        )

    prompt = build_brand_research_prompt(brand_name=brand_name, user_context=user_context)
    prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()
    started_at = time.perf_counter()
    try:
        with start_optional_span(
            "sake_concierge.brand_research",
            {
                "sake.brand_research.trace_id": trace_id,
                "sake.brand_research.brand_name": brand_name,
                "sake.brand_research.intended_search_query": intended_search_query,
                "sake.brand_research.prompt_sha256": prompt_hash,
            },
        ) as span:
            response = create_brand_research_response_with_retry(
                openai_client=openai_client,
                prompt=prompt,
                extra_body={"agent_reference": brand_research_agent_ref.as_dict()},
            )
            response_trace = extract_response_trace(response)
            set_span_attributes(
                span,
                {
                    "sake.brand_research.response_id": response_trace.get("response_id"),
                    "sake.brand_research.source_count": len(response_trace["source_urls"]),
                    "sake.brand_research.tool_types": ",".join(response_trace["tool_types"]),
                },
            )
    except (AzureError, OpenAIError) as exc:
        log_brand_research_trace(
            "brand_research.trace.agent_error",
            **base_trace,
            status="research_agent_error",
            elapsed_ms=elapsed_ms(started_at),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        logger.warning("brand_research.agent_call.failed brand_name=%s error=%s", brand_name, exc)
        return json.dumps(
            {
                "status": "research_agent_error",
                "brand_name": brand_name,
                "trace": build_trace_payload(
                    trace_id=trace_id,
                    intended_search_query=intended_search_query,
                    actual_web_search_query_visible=False,
                    status="research_agent_error",
                ),
                "summary": (
                    "外部銘柄リサーチに失敗しました。銘柄情報を断定せず、"
                    "ユーザーの好みを追加で確認してください。"
                ),
                "usage_policy": (
                    "他銘柄は好み推定の材料に限定し、推薦は取扱い酒リスト内から行うこと。"
                ),
            },
            ensure_ascii=False,
        )

    summary = extract_response_text(response)
    log_brand_research_trace(
        "brand_research.trace.complete",
        **base_trace,
        status="ok",
        elapsed_ms=elapsed_ms(started_at),
        prompt_sha256=prompt_hash,
        response=response_trace,
        summary_preview=truncate_text(summary, get_trace_content_max_chars()),
    )

    return json.dumps(
        {
            "status": "ok",
            "brand_name": brand_name,
            "trace": build_trace_payload(
                trace_id=trace_id,
                intended_search_query=intended_search_query,
                actual_web_search_query_visible=False,
                status="ok",
                response_trace=response_trace,
                elapsed_ms=elapsed_ms(started_at),
            ),
            "sources": response_trace["source_urls"],
            "summary": summary,
            "usage_policy": (
                "リサーチ結果は比較・ヒアリングの材料に限定し、推薦銘柄は"
                "取扱い酒リスト内から選ぶこと。"
            ),
        },
        ensure_ascii=False,
    )


def build_trace_payload(
    *,
    trace_id: str,
    intended_search_query: str,
    actual_web_search_query_visible: bool,
    status: str,
    response_trace: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
) -> dict[str, Any]:
    """Build compact diagnostic metadata returned to the main agent."""
    response_trace = response_trace or {}
    source = response_trace.get("source")
    if source is None:
        source = "not_configured" if status == "research_agent_not_configured" else "model_unknown"
    return {
        "trace_id": trace_id,
        "status": status,
        "intended_search_query": intended_search_query,
        "actual_web_search_query_visible": actual_web_search_query_visible,
        "actual_web_search_query_note": (
            "Foundry WebSearchTool preview does not expose the generated search query "
            "through this app-level response."
        ),
        "response_id": response_trace.get("response_id"),
        "conversation_id": response_trace.get("conversation_id"),
        "tool_types": response_trace.get("tool_types", []),
        "source_urls": response_trace.get("source_urls", []),
        "source": source,
        "elapsed_ms": elapsed_ms,
    }


def build_brand_research_query(brand_name: str) -> str:
    """Return the app-side preferred query used for diagnostics and prompt steering."""
    return f"{brand_name} 日本酒 味わい 甘辛 酸 香り 旨み 飲み口"


def create_brand_research_response_with_retry(
    *,
    openai_client: Any,
    prompt: str,
    extra_body: dict[str, Any],
) -> Any:
    """Call the research agent with short retries for preview throttling."""
    for attempt in range(len(BRAND_RESEARCH_RETRY_DELAYS_SECONDS) + 1):
        try:
            return openai_client.responses.create(input=prompt, extra_body=extra_body)
        except (AzureError, OpenAIError) as exc:
            if not is_retryable_upstream_error(exc):
                raise
            if attempt >= len(BRAND_RESEARCH_RETRY_DELAYS_SECONDS):
                raise
            delay_seconds = BRAND_RESEARCH_RETRY_DELAYS_SECONDS[attempt]
            logger.warning(
                "brand_research.agent_call.retry attempt=%d delay_seconds=%s error=%s",
                attempt + 1,
                delay_seconds,
                exc,
            )
            time.sleep(delay_seconds)

    raise RuntimeError("brand research agent call failed")


def extract_response_trace(response: Any) -> dict[str, Any]:
    """Extract app-visible diagnostic fields from a Responses API object."""
    output_text = extract_response_text(response)
    annotations = extract_annotations(response)
    annotation_urls = [
        annotation["url"]
        for annotation in annotations
        if isinstance(annotation.get("url"), str) and annotation["url"]
    ]
    source_urls = list(dict.fromkeys([*annotation_urls, *extract_urls(output_text)]))

    return {
        "response_id": get_value(response, "id"),
        "conversation_id": get_value(get_value(response, "conversation"), "id"),
        "model": get_value(response, "model"),
        "x_request_id": get_value(response, "x_request_id"),
        "tool_types": extract_tool_types(response),
        "annotations": annotations,
        "source_urls": source_urls,
        "source": "web_search" if source_urls else "model_unknown",
        "output_text_chars": len(output_text),
        "output_preview": truncate_text(output_text, get_trace_content_max_chars()),
    }


def extract_tool_types(response: Any) -> list[str]:
    """Collect tool types visible on the response object."""
    tools = get_value(response, "tools", []) or []
    tool_types: list[str] = []
    for tool in tools:
        tool_type = get_value(tool, "type")
        if tool_type:
            tool_types.append(str(tool_type))
    return list(dict.fromkeys(tool_types))


def extract_annotations(response: Any) -> list[dict[str, Any]]:
    """Collect URL/file annotations exposed by output_text content parts."""
    annotations: list[dict[str, Any]] = []
    for item in get_value(response, "output", []) or []:
        for part in get_value(item, "content", []) or []:
            for annotation in get_value(part, "annotations", []) or []:
                annotation_record = annotation_to_trace_dict(annotation)
                if annotation_record:
                    annotations.append(annotation_record)
    return annotations


def annotation_to_trace_dict(annotation: Any) -> dict[str, Any]:
    """Convert SDK annotation models or dicts into compact trace fields."""
    annotation_type = get_value(annotation, "type")
    url = get_value(annotation, "url") or get_value(annotation, "uri")
    title = get_value(annotation, "title")
    file_id = get_value(annotation, "file_id")
    record = {
        "type": annotation_type,
        "url": url,
        "title": title,
        "file_id": file_id,
    }
    return {key: value for key, value in record.items() if value}


def extract_urls(text: str) -> list[str]:
    """Extract URL-like strings from model text as a fallback source list."""
    return list(dict.fromkeys(URL_PATTERN.findall(text or "")))


def get_value(source: Any, key: str, default: Any = None) -> Any:
    """Read an attribute from SDK models or dict-like values."""
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def is_retryable_upstream_error(exc: Exception) -> bool:
    """Return true for transient upstream throttling errors worth retrying."""
    message = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or "too many requests" in message or "rate limit" in message


def parse_brand_research_arguments(arguments: str) -> dict[str, str]:
    """Parse and validate the model-supplied function arguments."""
    try:
        data = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {
            "status": "invalid_arguments",
            "summary": "銘柄リサーチの引数が JSON として解釈できませんでした。",
        }

    brand_name = str(data.get("brand_name", "")).strip()
    if not brand_name:
        return {
            "status": "invalid_arguments",
            "summary": "銘柄名が指定されていません。",
        }

    user_context = str(data.get("user_context", "")).strip()
    return {
        "status": "ok",
        "brand_name": brand_name[:MAX_BRAND_NAME_LENGTH],
        "user_context": user_context[:MAX_USER_CONTEXT_LENGTH],
    }


def build_brand_research_prompt(*, brand_name: str, user_context: str | None = None) -> str:
    """Build the prompt sent to the optional brand research sub-agent."""
    context_line = f"\nユーザー文脈: {user_context}" if user_context else ""
    intended_search_query = build_brand_research_query(brand_name)
    return f"""\
あなたは日本酒銘柄の比較調査を行うサブエージェントです。

対象銘柄: {brand_name}{context_line}
確認時の優先検索語（アプリ側トレース用）: {intended_search_query}

次の観点だけを簡潔に整理してください。
- 甘辛、酸、香り、旨み、飲み口の傾向
- 似た味わいを探す時に見るべき比較軸
- 情報の確度。不明な点は不明と明記する
- 根拠URLまたはページ名が確認できた場合は、末尾に「根拠: タイトル - URL」を最大3件列挙する
- 検索していない、または根拠URLが確認できない場合は「根拠: 不明」と明記する

注意:
- 価格、在庫、販売可否は断定しない
- 健康効果や飲酒促進につながる表現は避ける
- 最終推薦はメインエージェントの取扱い酒リスト内で行うため、他銘柄の購入は勧めない
"""


def extract_response_text(response: Any) -> str:
    """Extract text from OpenAI Responses-compatible objects."""
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    return str(response)


def start_optional_span(name: str, attributes: dict[str, Any]):
    """Start an OpenTelemetry span when tracing is configured; otherwise no-op."""
    if otel_trace is None:
        return nullcontext(None)
    span_cm = otel_trace.get_tracer("sake_concierge").start_as_current_span(name)
    return SpanAttributeContext(span_cm, attributes)


class SpanAttributeContext:
    """Small context manager that sets span attributes without coupling to OTel SDK."""

    def __init__(self, span_cm: Any, attributes: dict[str, Any]) -> None:
        self.span_cm = span_cm
        self.attributes = attributes
        self.span: Any | None = None

    def __enter__(self) -> Any:
        self.span = self.span_cm.__enter__()
        set_span_attributes(self.span, self.attributes)
        return self.span

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None:
        return self.span_cm.__exit__(exc_type, exc, traceback)


def set_span_attributes(span: Any | None, attributes: dict[str, Any]) -> None:
    """Set primitive span attributes, skipping unset values."""
    if span is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str | bool | int | float):
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, json.dumps(value, ensure_ascii=False))


def log_brand_research_trace(event_name: str, **payload: Any) -> None:
    """Emit a structured trace log that Application Insights can ingest."""
    if not get_bool_env("BRAND_RESEARCH_TRACE_ENABLED", True):
        return
    record = {
        "event": event_name,
        "schema_version": 1,
        **payload,
    }
    logger.info(
        "brand_research_trace %s",
        json.dumps(record, ensure_ascii=False, sort_keys=True),
    )


def get_bool_env(name: str, default: bool) -> bool:
    """Parse a boolean environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_trace_content_max_chars() -> int:
    """Limit logged model text previews to avoid oversized telemetry."""
    raw = os.environ.get("BRAND_RESEARCH_TRACE_CONTENT_MAX_CHARS")
    if raw is None:
        return DEFAULT_TRACE_CONTENT_MAX_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TRACE_CONTENT_MAX_CHARS


def truncate_text(text: str, max_chars: int) -> str:
    """Return a bounded preview suitable for diagnostics."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def elapsed_ms(started_at: float) -> int:
    """Return elapsed milliseconds from a perf_counter timestamp."""
    return int((time.perf_counter() - started_at) * 1000)

