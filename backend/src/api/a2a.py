"""Minimal A2A endpoint for sake brand research."""

from __future__ import annotations

import json
import os
import uuid
from hmac import compare_digest
from typing import Any

from fastapi import HTTPException, Request

from src.api.research_tools import execute_brand_research_tool

A2A_PROTOCOL_VERSION = "0.3.0"
A2A_RPC_PATH = "/a2a"


def build_agent_card(request: Request) -> dict[str, Any]:
    """Return an A2A Agent Card for the brand research endpoint."""
    base_url = get_external_base_url(request)
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "Sake Brand Research Agent",
        "description": (
            "日本酒の他銘柄について、甘辛・酸・香り・旨み・飲み口の比較材料を返す"
            "Sake Concierge 用の軽量リサーチエージェント。"
        ),
        "version": "0.1.0",
        "url": f"{base_url}{A2A_RPC_PATH}",
        "preferredTransport": "JSONRPC",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "research-sake-brand",
                "name": "Research Sake Brand",
                "description": (
                    "ユーザーが普段飲む日本酒銘柄の味わい傾向を調べ、"
                    "取扱い酒へ翻訳するための比較軸を返す。"
                ),
                "tags": ["sake", "brand-research", "comparison"],
                "examples": ["新政 No.6 が好き", "獺祭の甘みと近い酒を探したい"],
            }
        ],
    }


async def handle_a2a_rpc(
    *,
    request: Request,
    openai_client: Any,
    brand_research_agent_ref: Any | None,
) -> dict[str, Any]:
    """Handle the JSON-RPC subset needed by Foundry A2A preview."""
    require_a2a_api_key(request)

    payload = await request.json()
    request_id = payload.get("id")
    method = payload.get("method")

    if method == "message/send":
        return build_jsonrpc_result(
            request_id,
            build_research_message_result(
                openai_client=openai_client,
                brand_research_agent_ref=brand_research_agent_ref,
                params=payload.get("params") or {},
            ),
        )

    return build_jsonrpc_error(
        request_id,
        code=-32601,
        message=f"Unsupported A2A method: {method}",
    )


def build_research_message_result(
    *,
    openai_client: Any,
    brand_research_agent_ref: Any | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Build a synchronous A2A message result for brand research."""
    message = params.get("message") or {}
    text = extract_text_from_message(message)
    context_id = message.get("contextId") or str(uuid.uuid4())
    tool_output = execute_brand_research_tool(
        openai_client=openai_client,
        brand_research_agent_ref=brand_research_agent_ref,
        arguments=json.dumps({"brand_name": text, "user_context": text}, ensure_ascii=False),
        trace_context={
            "conversation_id": context_id,
            "response_id": None,
            "tool_call_id": message.get("messageId"),
            "tool_name": "a2a_message_send",
            "transport": "a2a",
        },
    )
    output = json.loads(tool_output)
    response_text = format_research_output(output)

    return {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "contextId": context_id,
        "parts": [
            {
                "kind": "text",
                "text": response_text,
            }
        ],
        "metadata": {
            "source": "sake-concierge-a2a",
            "research_status": output.get("status", "unknown"),
            "research_trace_id": (output.get("trace") or {}).get("trace_id"),
        },
    }


def extract_text_from_message(message: dict[str, Any]) -> str:
    """Extract text parts from an A2A message."""
    texts: list[str] = []
    for part in message.get("parts") or []:
        if part.get("kind") == "text":
            text = str(part.get("text", "")).strip()
            if text:
                texts.append(text)
    return "\n".join(texts).strip() or "銘柄名未指定"


def format_research_output(output: dict[str, Any]) -> str:
    """Format tool JSON into a compact A2A response."""
    brand_name = output.get("brand_name")
    summary = output.get("summary", "")
    usage_policy = output.get("usage_policy", "")
    if brand_name:
        return f"対象銘柄: {brand_name}\n{summary}\n\n利用方針: {usage_policy}".strip()
    return str(summary).strip()


def build_jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def build_jsonrpc_error(request_id: Any, *, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def require_a2a_api_key(request: Request) -> None:
    """Require an x-api-key header before the A2A RPC endpoint can run."""
    expected = os.environ.get("A2A_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    provided = request.headers.get("x-api-key", "")
    if not compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid A2A API key")


def get_external_base_url(request: Request) -> str:
    """Resolve the public base URL without trusting inbound Host headers by default."""
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if public_base_url:
        return public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")
