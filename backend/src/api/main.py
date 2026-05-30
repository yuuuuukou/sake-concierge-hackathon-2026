"""Sake Concierge — FastAPI バックエンド.

エージェントは事前に backend/scripts/setup_agent.py で作成しておくこと。

環境変数:
    AZURE_AIPROJECT_ENDPOINT     ... Foundry プロジェクトエンドポイント
    AZURE_OPENAI_DEPLOYMENT_NAME ... モデルデプロイメント名（例: gpt-5-nano）
    AZURE_AGENT_NAME             ... 事前作成済みエージェント名
    AZURE_AGENT_VERSION          ... 事前作成済みエージェントバージョン

起動:
    cd backend
    uvicorn src.api.main:app --reload
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from openai.types.responses.response_input_param import FunctionCallOutput
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.api.a2a import build_agent_card, handle_a2a_rpc, require_a2a_api_key
from src.api.research_tools import (
    BRAND_RESEARCH_TOOL_NAME,
    execute_brand_research_tool,
)
from src.api.store_catalog import StoreNotFoundError, load_store_profile
from src.api.store_data import prepare_store_data

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_STATIC_ROOT = Path(__file__).resolve().parent / "static" / "react"
FRONTEND_ASSETS_ROOT = FRONTEND_STATIC_ROOT / "assets"

load_dotenv(BACKEND_ROOT.parent / ".env")
load_dotenv(BACKEND_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# ロギング・設定・状態
# ---------------------------------------------------------------------------
logger = logging.getLogger("sake_concierge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_telemetry_configured = False
_openai_instrumented = False

REQUIRED_ENV_VARS = (
    "AZURE_AIPROJECT_ENDPOINT",
    "AZURE_AGENT_NAME",
    "AZURE_AGENT_VERSION",
)

_state: dict[str, Any] = {}
_chat_rate_limit_lock = threading.Lock()
_chat_rate_limit_hits: dict[str, deque[float]] = {}
_metrics_lock = threading.Lock()
_metrics_state: dict[str, Any] = {
    "chat_requests": defaultdict(int),
    "feedback_total": defaultdict(int),
    "feedback_positive": defaultdict(int),
    "feedback_negative": defaultdict(int),
}

DEFAULT_CHAT_RATE_LIMIT_PER_MINUTE = 5
DEFAULT_CHAT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_EVENT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_EVENT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_MAX_FEEDBACK_RECORDS = 500
DEFAULT_MAX_TOOL_CALL_ROUNDS = 3
# Extraction/display safety cap only; this value is not sent to Agent instructions.
DEFAULT_MAX_RECOMMENDATION_CARDS = 10
DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS = (1.0, 2.0)
DEFAULT_BRAND_RESEARCH_TOOL_CHOICE_MODE = "intent"
DEFAULT_CHAT_INITIAL_DELTA = "お好みに合う候補を探してみますね。\n\n"
DEFAULT_FASTAPI_EXCLUDED_URLS = r".*/health(?:\?.*)?$"
GOLDEN_SET_TARGET_LABEL = "90%以上"
DEFAULT_CHAT_TEXT_CAPTURE_MODE = "feedback_only"
CHAT_TEXT_CAPTURE_MODES = {"off", "feedback_only", "all"}
SENSITIVE_PATTERNS = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b0\d{1,4}-?\d{1,4}-?\d{3,4}\b"), "[PHONE]"),
    (re.compile(r"\b\d{3}-\d{4}\b"), "[POSTAL_CODE]"),
    (re.compile(r"https?://\S+"), "[URL]"),
)
AZURE_MONITOR_INSTRUMENTATION_OPTIONS = {"fastapi": {"enabled": False}}
_feedback_records: deque[dict[str, Any]] = deque(maxlen=DEFAULT_MAX_FEEDBACK_RECORDS)
BRAND_RESEARCH_FALLBACK_PRODUCT_IDS = {
    "fukunotomo": [
        "ftm-fuyuki-fff-genshu",
        "ftm-fuyuki-fff-nama-genshu",
        "ftm-fuyuki-nama-genshu",
    ]
}
BRAND_RESEARCH_INTENT_MARKERS = (
    "好き",
    "すき",
    "普段",
    "いつも",
    "よく",
    "飲む",
    "飲ん",
    "どんなお酒",
    "どんな酒",
    "って",
    "について",
    "似て",
    "近い",
    "みたい",
)
STORE_BRAND_HINTS = (
    "サンプル店舗",
    "Fukunotomo",
    "DE Fukunotomo",
    "冬樹",
    "FFF",
    "秋田犬ラベル",
    "F1501",
    "F901",
    "神宮寺",
    "又右ェ門蔵",
    "又右エ門蔵",
    "60純米酒",
    "60純米",
    "純米原酒",
    "蔵内原酒",
    "馬から",
    "春うさぎ",
    "サワードッグ",
    "Wild card",
    "マル秘純米吟醸",
    "ヤママタクロラベル",
    "杉玉ラベル",
    "大吟醸 福",
)
GENERIC_SAKE_TERMS = (
    "日本酒",
    "お酒",
    "酒",
    "銘柄",
    "甘口",
    "辛口",
    "旨口",
    "淡麗",
    "濃醇",
    "香り",
    "酸味",
    "旨み",
    "純米",
    "吟醸",
    "大吟醸",
    "本醸造",
    "生酒",
    "原酒",
    "にごり",
    "食中酒",
    "冷酒",
    "燗",
    "おすすめ",
    "プレゼント",
    "ギフト",
)
BRAND_CANDIDATE_SPLIT_PATTERN = re.compile(r"[、,，/／\n]+")
BRAND_CANDIDATE_SUFFIX_PATTERN = re.compile(
    r"(?:が(?:好き|すき)|を?(?:普段|いつも|よく)?飲(?:む|ん|み)|って|とは|について|に(?:近い|似て)|みたい).*$"
)
BRAND_CANDIDATE_PREFIX_PATTERN = re.compile(
    r"^(?:普段は|普段|いつも|よく|最近|私は|わたしは|僕は|俺は|当店では|この中で|その中で)\s*"
)
BRAND_CANDIDATE_ALLOWED_PATTERN = re.compile(r"[一-龯々〆ヵヶぁ-んァ-ヴーA-Za-z0-9]")


# ---------------------------------------------------------------------------
# 型定義
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    """フロントエンドが /chat に渡す最小の会話リクエスト。"""

    message: str = Field(min_length=1, max_length=1200)
    conversation_id: str | None = Field(default=None, max_length=120)
    session_id: str | None = Field(default=None, max_length=120)
    store_id: str | None = Field(default=None, max_length=64)
    language: Literal["ja", "en", "zh"] | None = None


class AnalyticsEventRequest(BaseModel):
    """KPI 集計用の、本文を含めない軽量イベント。"""

    event_type: Literal[
        "message_sent",
        "recommendation_shown",
        "product_link_clicked",
        "feedback_submitted",
    ]
    store_id: str = Field(default="fukunotomo", max_length=64)
    session_id: str | None = Field(default=None, max_length=120)
    conversation_id: str | None = Field(default=None, max_length=120)
    message_id: str | None = Field(default=None, max_length=120)
    product_id: str | None = Field(default=None, max_length=120)
    product_ids: list[str] = Field(default_factory=list, max_length=20)
    recommendation_rank: int | None = Field(default=None, ge=1, le=50)
    official_url: str | None = Field(default=None, max_length=1000)
    page_path: str | None = Field(default=None, max_length=300)
    language: Literal["ja", "en", "zh"] | None = None
    rating: Literal["positive", "negative"] | None = None


class AnalyticsEventResponse(BaseModel):
    """analytics event を受け付けたことだけを UI に返す。"""

    status: Literal["ok"]


class ConversationResponse(BaseModel):
    """画面表示時点で先に払い出す会話 ID。"""

    conversation_id: str


class FeedbackRequest(BaseModel):
    """UI から受け取る、品質改善用の簡易フィードバック。"""

    store_id: str = Field(default="fukunotomo", max_length=64)
    session_id: str | None = Field(default=None, max_length=120)
    conversation_id: str | None = Field(default=None, max_length=120)
    message_id: str | None = Field(default=None, max_length=120)
    rating: Literal["positive", "negative"]
    comment: str | None = Field(default=None, max_length=280)
    user_message: str | None = Field(default=None, max_length=2000)
    assistant_message: str | None = Field(default=None, max_length=6000)
    language: Literal["ja", "en", "zh"] | None = None


class FeedbackResponse(BaseModel):
    """フィードバック保存後に UI が metrics を更新するための応答。"""

    status: Literal["ok"]
    metrics: dict[str, Any]


@dataclass(frozen=True)
class StreamWorker:
    """blocking stream を読む background thread と、その停止に必要な状態。"""

    thread: threading.Thread
    stop_event: threading.Event
    stream_holder: list[Any]


@dataclass(frozen=True)
class StreamQueueItem:
    """background thread から async SSE 側へ渡す、stream の状態変化。"""

    kind: str
    data: str = ""


@dataclass(frozen=True)
class AgentReference:
    """Responses API の extra_body に渡す Foundry Agent 参照。"""

    name: str
    version: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Foundry REST schema の AgentReference 形式に変換する。"""
        data = {
            "type": "agent_reference",
            "name": self.name,
        }
        if self.version:
            data["version"] = self.version
        return data


# ---------------------------------------------------------------------------
# テレメトリ
# ---------------------------------------------------------------------------
def configure_fastapi_excluded_urls() -> str:
    """FastAPI request telemetry から除外する URL を環境変数にも反映する。"""
    excluded_urls = (
        os.environ.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS")
        or os.environ.get("OTEL_PYTHON_EXCLUDED_URLS")
        or DEFAULT_FASTAPI_EXCLUDED_URLS
    )
    os.environ.setdefault("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", excluded_urls)
    os.environ.setdefault("OTEL_PYTHON_EXCLUDED_URLS", excluded_urls)
    return excluded_urls


def configure_application_insights() -> bool:
    """Application Insights connection string がある場合だけ Azure Monitor を有効化する。"""
    global _telemetry_configured

    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        logger.info("Application Insights telemetry disabled: connection string is not set")
        return False

    if _telemetry_configured:
        logger.info("Application Insights telemetry already configured")
        return True

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        logger.warning(
            "Application Insights telemetry disabled: azure-monitor-opentelemetry is unavailable"
        )
        logger.debug("Application Insights import error", exc_info=True)
        return False

    excluded_urls = configure_fastapi_excluded_urls()
    configure_azure_monitor(
        connection_string=connection_string,
        logger_name="sake_concierge",
        instrumentation_options=AZURE_MONITOR_INSTRUMENTATION_OPTIONS,
    )
    _telemetry_configured = True
    logger.info(
        "Application Insights telemetry configured",
        extra={"excluded_urls": excluded_urls},
    )
    return True


def instrument_fastapi_app(fastapi_app: FastAPI) -> bool:
    """FastAPI の request telemetry を Application Insights へ送る。"""
    if not _telemetry_configured:
        logger.info("FastAPI request telemetry disabled: Application Insights is not configured")
        return False

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning(
            "FastAPI request telemetry disabled: "
            "opentelemetry-instrumentation-fastapi is unavailable"
        )
        logger.debug("FastAPI instrumentation import error", exc_info=True)
        return False

    excluded_urls = configure_fastapi_excluded_urls()
    FastAPIInstrumentor.instrument_app(fastapi_app, excluded_urls=excluded_urls)
    logger.info(
        "FastAPI request telemetry configured",
        extra={"excluded_urls": excluded_urls},
    )
    return True


def instrument_openai_sdk() -> bool:
    """OpenAI SDK の自動計装を、使える環境だけで有効化する。"""
    global _openai_instrumented

    if not _telemetry_configured:
        logger.info("OpenAI SDK telemetry disabled: Application Insights is not configured")
        return False
    if _openai_instrumented:
        logger.info("OpenAI SDK telemetry already configured")
        return True

    try:
        from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
    except ImportError:
        logger.warning(
            "OpenAI SDK telemetry disabled: opentelemetry-instrumentation-openai-v2 is unavailable"
        )
        logger.debug("OpenAI instrumentation import error", exc_info=True)
        return False

    OpenAIInstrumentor().instrument()
    _openai_instrumented = True
    logger.info("OpenAI SDK telemetry configured")
    return True


# ---------------------------------------------------------------------------
# アプリ起動時の準備
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に Foundry Agent を確認し、設定ミスを request-time まで持ち越さない。"""
    del app
    logger.info("🚀 Sake Concierge 起動中...")

    missing = find_missing_env_vars()
    if missing:
        raise RuntimeError(f"必須環境変数が未設定: {', '.join(missing)}")

    data_root = prepare_store_data()
    logger.info(
        "store_data.ready source=%s root=%s",
        os.getenv("STORE_DATA_SOURCE", "local"),
        data_root,
    )

    project = create_project_client()
    openai_client = project.get_openai_client()
    agent_ref = load_agent_reference(project)
    brand_research_agent_ref = load_brand_research_agent_reference()

    _state["project"] = project
    _state["openai"] = openai_client
    _state["agent_ref"] = agent_ref
    _state["brand_research_agent_ref"] = brand_research_agent_ref

    logger.info("🍶 準備完了！")

    yield

    logger.info("👋 シャットダウン")


# ---------------------------------------------------------------------------
# FastAPI アプリ・リクエスト
# ---------------------------------------------------------------------------
# FastAPI の自動計測は app 作成前に有効化する必要がある。
configure_application_insights()

app = FastAPI(title="Sake Concierge", version="0.1.0", lifespan=lifespan)
app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_ASSETS_ROOT, check_dir=False),
    name="frontend-assets",
)
instrument_fastapi_app(app)
instrument_openai_sdk()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add conservative browser security headers to every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    """外部サービスへ触らず、コンテナの生存確認だけを返す。"""
    return {"status": "ok"}


@app.get("/api/stores/{store_id}")
async def store_profile(store_id: str) -> dict[str, Any]:
    """店舗別 UI が使う表示設定・商品カード用カタログを返す。"""
    try:
        return load_store_profile(store_id)
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail="店舗データが見つかりません") from exc


@app.get("/api/stores/{store_id}/metrics")
async def store_metrics(store_id: str) -> dict[str, Any]:
    """提出デモで見せる最小の品質メトリクスを返す。"""
    try:
        load_store_profile(store_id)
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail="店舗データが見つかりません") from exc

    return build_metrics_summary(store_id)


@app.post("/api/feedback")
async def submit_feedback(req: FeedbackRequest, request: Request) -> FeedbackResponse:
    """チャット回答への👍/👎を、品質改善用に記録する。"""
    check_event_rate_limit(request, scope="feedback")
    try:
        load_store_profile(req.store_id)
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail="店舗データが見つかりません") from exc

    record_feedback(req=req)
    return FeedbackResponse(status="ok", metrics=build_metrics_summary(req.store_id))


@app.post("/api/analytics/events")
async def submit_analytics_event(
    req: AnalyticsEventRequest,
    request: Request,
) -> AnalyticsEventResponse:
    """本文を含まない UI/KPI イベントを構造化ログへ記録する。"""
    check_event_rate_limit(request, scope="analytics")
    store_id = normalize_store_id(req.store_id)
    try:
        load_store_profile(store_id)
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail="店舗データが見つかりません") from exc

    record_analytics_event(req=req, store_id=store_id)
    return AnalyticsEventResponse(status="ok")


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/.well-known/agent-card.json")
async def a2a_agent_card(request: Request) -> dict[str, Any]:
    """A2A discovery endpoint for the sake brand research agent."""
    return build_agent_card(request)


@app.post("/a2a")
async def a2a_rpc(request: Request) -> dict[str, Any]:
    """A2A JSON-RPC endpoint called by Foundry A2A preview."""
    require_a2a_api_key(request)
    check_chat_rate_limit(request, scope="a2a")
    openai_client, _ = get_chat_runtime()
    brand_research_agent_ref = get_brand_research_agent_reference()
    return await handle_a2a_rpc(
        request=request,
        openai_client=openai_client,
        brand_research_agent_ref=brand_research_agent_ref,
    )


@app.get("/", include_in_schema=False)
async def frontend_index() -> FileResponse:
    """Vite build 済みの React アプリを返す。"""
    return get_frontend_index_response()


@app.post("/chat/conversation")
async def create_chat_conversation(request: Request) -> ConversationResponse:
    """初回メッセージ送信前に Foundry 側の conversation だけを作る。"""
    check_chat_rate_limit(request, scope="chat-conversation")
    openai_client, _ = get_chat_runtime()
    return ConversationResponse(conversation_id=create_conversation_id(openai_client))


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> EventSourceResponse:
    """UI からの会話を、作成済み Foundry Agent へ SSE で中継する。"""
    check_chat_rate_limit(request, scope="chat")

    store_id = normalize_store_id(req.store_id)
    try:
        load_store_profile(store_id)
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail="店舗データが見つかりません") from exc

    openai_client, agent_ref = get_chat_runtime()
    brand_research_agent_ref = get_brand_research_agent_reference()
    conversation_id = get_or_create_conversation_id(openai_client, req.conversation_id)
    record_chat_request(store_id)
    record_analytics_event(
        req=AnalyticsEventRequest(
            event_type="message_sent",
            store_id=store_id,
            session_id=req.session_id,
            conversation_id=conversation_id,
            language=req.language,
        ),
        store_id=store_id,
    )

    return EventSourceResponse(
        stream_chat_events(
            openai_client=openai_client,
            agent_ref=agent_ref,
            brand_research_agent_ref=brand_research_agent_ref,
            conversation_id=conversation_id,
            store_id=store_id,
            user_message=req.message,
            agent_message=build_agent_input_message(req.message, req.language),
            session_id=req.session_id,
            language=req.language,
        )
    )


@app.get("/{full_path:path}", include_in_schema=False)
async def frontend_spa_fallback(full_path: str) -> FileResponse:
    """SPA の deep link を React 側に戻す。"""
    if is_reserved_backend_path(full_path):
        raise HTTPException(status_code=404, detail="Not Found")
    return get_frontend_index_response()


def is_reserved_backend_path(full_path: str) -> bool:
    """Avoid returning React HTML for backend/API paths that should be real 404s."""
    first_segment = full_path.split("/", 1)[0]
    return first_segment in {"api", "chat", "assets", "a2a", ".well-known"}


# ---------------------------------------------------------------------------
# 起動時の補助関数
# ---------------------------------------------------------------------------
def find_missing_env_vars() -> list[str]:
    """必要な設定漏れをまとめて表示し、実機確認の手戻りを減らす。"""
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def create_project_client() -> AIProjectClient:
    """Foundry 接続の作成を起動時に寄せ、/chat では SDK 初期化を行わない。"""
    return AIProjectClient(
        endpoint=os.environ["AZURE_AIPROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )


def load_agent_reference(project: AIProjectClient) -> AgentReference:
    """事前作成済み Agent の存在を起動時に確認し、/chat は会話中継だけにする。"""
    agent_name = os.environ["AZURE_AGENT_NAME"]
    agent_version = os.environ["AZURE_AGENT_VERSION"]
    logger.info(f"🤖 エージェント確認中: {agent_name} v{agent_version}")

    try:
        agent = project.agents.get_version(agent_name, agent_version)
        logger.info(f"  ✅ エージェント確認済: {agent.id}")
    except AzureError as e:
        raise RuntimeError(
            f"エージェント取得失敗 ({agent_name} v{agent_version}): {e}\n"
            "backend/scripts/setup_agent.py を実行してエージェントを作成してください。"
        ) from e

    return AgentReference(name=agent.name, version=agent.version)


def load_brand_research_agent_reference() -> AgentReference | None:
    """Optional Foundry Agent used behind the brand research function tool."""
    agent_name = os.environ.get("AZURE_BRAND_RESEARCH_AGENT_NAME")
    if not agent_name:
        logger.info("brand_research.agent.disabled")
        return None

    agent_version = os.environ.get("AZURE_BRAND_RESEARCH_AGENT_VERSION")
    logger.info(
        "brand_research.agent.configured agent_name=%s agent_version=%s",
        agent_name,
        agent_version or "latest",
    )
    return AgentReference(name=agent_name, version=agent_version)


# ---------------------------------------------------------------------------
# フロントエンド配信の補助関数
# ---------------------------------------------------------------------------
def get_frontend_index_response() -> FileResponse:
    """React build が存在する場合だけ index.html を返す。"""
    index_file = FRONTEND_STATIC_ROOT / "index.html"
    if not index_file.exists():
        raise HTTPException(
            status_code=404,
            detail="フロントエンドが未ビルドです。frontend で npm run build を実行してください。",
        )

    return FileResponse(index_file)


# ---------------------------------------------------------------------------
# エンドポイントの補助関数
# ---------------------------------------------------------------------------
def get_chat_runtime() -> tuple[Any, AgentReference]:
    """/chat が Retriever や SDK 初期化に触らず、起動済み状態だけを使うようにする。"""
    openai_client = _state.get("openai")
    agent_ref = _state.get("agent_ref")
    if not openai_client or not agent_ref:
        raise HTTPException(status_code=503, detail="サービスが初期化されていません")

    return openai_client, agent_ref


def get_brand_research_agent_reference() -> AgentReference | None:
    """Return the optional sub-agent reference used by the brand research tool."""
    return _state.get("brand_research_agent_ref")


def check_chat_rate_limit(request: Request, *, scope: str = "chat") -> None:
    """公開デモでのモデル呼び出しを、IP ごとに短時間だけ絞る。"""
    check_rate_limit(
        request,
        scope=scope,
        limit=get_chat_rate_limit_per_minute(),
        window_seconds=get_chat_rate_limit_window_seconds(),
    )


def check_event_rate_limit(request: Request, *, scope: str) -> None:
    """KPI/feedback 系の軽量 POST を、通常利用を妨げない範囲で絞る。"""
    check_rate_limit(
        request,
        scope=scope,
        limit=get_event_rate_limit_per_minute(),
        window_seconds=get_event_rate_limit_window_seconds(),
    )


def check_rate_limit(
    request: Request,
    *,
    scope: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Apply an in-memory rate limit for one public endpoint scope."""
    if limit <= 0:
        return

    client_ip = get_client_ip(request)
    key = f"{scope}:{client_ip}"
    now = time.monotonic()

    with _chat_rate_limit_lock:
        hits = _chat_rate_limit_hits.setdefault(key, deque())
        while hits and now - hits[0] >= window_seconds:
            hits.popleft()

        if len(hits) >= limit:
            retry_after = max(1, int(window_seconds - (now - hits[0])) + 1)
            logger.warning(
                "rate_limit.exceeded scope=%s client_ip=%s retry_after=%d",
                scope,
                client_ip,
                retry_after,
            )
            raise HTTPException(
                status_code=429,
                detail="短時間にアクセスが集中しています。少し待ってからもう一度お試しください。",
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)


def get_chat_rate_limit_per_minute() -> int:
    """1 IP あたりの /chat 許可回数。0 以下なら無効化する。"""
    return get_int_env("CHAT_RATE_LIMIT_PER_MINUTE", DEFAULT_CHAT_RATE_LIMIT_PER_MINUTE)


def get_chat_rate_limit_window_seconds() -> int:
    """rate limit の観測窓。壊れた値なら既定値へ戻す。"""
    value = get_int_env(
        "CHAT_RATE_LIMIT_WINDOW_SECONDS",
        DEFAULT_CHAT_RATE_LIMIT_WINDOW_SECONDS,
    )
    return value if value > 0 else DEFAULT_CHAT_RATE_LIMIT_WINDOW_SECONDS


def get_event_rate_limit_per_minute() -> int:
    """1 IP あたりの feedback / analytics 許可回数。0 以下なら無効化する。"""
    return get_int_env("EVENT_RATE_LIMIT_PER_MINUTE", DEFAULT_EVENT_RATE_LIMIT_PER_MINUTE)


def get_event_rate_limit_window_seconds() -> int:
    """event rate limit の観測窓。壊れた値なら既定値へ戻す。"""
    value = get_int_env(
        "EVENT_RATE_LIMIT_WINDOW_SECONDS",
        DEFAULT_EVENT_RATE_LIMIT_WINDOW_SECONDS,
    )
    return value if value > 0 else DEFAULT_EVENT_RATE_LIMIT_WINDOW_SECONDS


def get_int_env(name: str, default: int) -> int:
    """環境変数の整数値を読み、未設定や不正値なら default を返す。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer environment variable %s=%r", name, raw)
        return default


def get_bool_env(name: str, default: bool = False) -> bool:
    """Read common boolean env spellings."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_client_ip(request: Request) -> str:
    """Return the rate-limit client key without trusting spoofable proxy headers by default."""
    if get_bool_env("TRUST_FORWARDED_HEADERS"):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            first_ip = forwarded_for.split(",", maxsplit=1)[0].strip()
            if first_ip:
                return first_ip

        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


def reset_chat_rate_limit_state() -> None:
    """テストや運用確認で、インメモリ rate limit 状態を初期化する。"""
    with _chat_rate_limit_lock:
        _chat_rate_limit_hits.clear()


def reset_feedback_metrics_state() -> None:
    """テストやデモ前確認で、インメモリ feedback / metrics を初期化する。"""
    with _metrics_lock:
        _feedback_records.clear()
        for bucket in _metrics_state.values():
            bucket.clear()


def normalize_store_id(store_id: str | None) -> str:
    """未指定時は現在のデモ店舗へ寄せる。"""
    return (store_id or "fukunotomo").strip() or "fukunotomo"


def build_agent_input_message(message: str, language: str | None) -> str:
    """UI の言語切替を Agent へ明示する。日本語既定なら入力を汚さない。"""
    if language == "en":
        return f"Please answer in English.\n\nUser request:\n{message}"
    if language == "zh":
        return f"请用中文回答。\n\n用户咨询:\n{message}"
    return message


def record_chat_request(store_id: str) -> None:
    """会話本文を保存せず、デモ指標に必要な件数だけ数える。"""
    with _metrics_lock:
        _metrics_state["chat_requests"][store_id] += 1


def record_analytics_event(*, req: AnalyticsEventRequest, store_id: str | None = None) -> None:
    """KQL 集計用の軽量イベントを、本文なしで trace に残す。"""
    normalized_store_id = normalize_store_id(store_id or req.store_id)
    record = {
        "event": "analytics_event",
        "schema_version": 1,
        "event_type": req.event_type,
        "store_id": normalized_store_id,
        "session_id_hash": hash_session_id(req.session_id),
        "conversation_id": normalize_optional_text(req.conversation_id, max_length=120),
        "message_id": normalize_optional_text(req.message_id, max_length=120),
        "product_id": normalize_optional_text(req.product_id, max_length=120),
        "product_ids": [str(product_id)[:120] for product_id in req.product_ids[:20]],
        "recommendation_rank": req.recommendation_rank,
        "official_url": normalize_optional_text(req.official_url, max_length=1000),
        "page_path": normalize_optional_text(req.page_path, max_length=300),
        "language": req.language,
        "rating": req.rating,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    logger.info("analytics_event %s", json.dumps(record, ensure_ascii=False, sort_keys=True))


def record_feedback(*, req: FeedbackRequest) -> None:
    """回答単位の評価と会話抜粋を品質改善用に記録する。"""
    capture_text = should_capture_feedback_text()
    comment = redact_sensitive_text((req.comment or "").strip()) if capture_text else ""
    user_message = redact_sensitive_text((req.user_message or "").strip()) if capture_text else ""
    assistant_message = (
        redact_sensitive_text((req.assistant_message or "").strip()) if capture_text else ""
    )
    record = {
        "store_id": req.store_id,
        "session_id_hash": hash_session_id(req.session_id),
        "conversation_id": req.conversation_id,
        "message_id": req.message_id,
        "rating": req.rating,
        "comment": comment[:280],
        "user_message": user_message,
        "assistant_message": assistant_message,
        "comment_present": bool(req.comment),
        "user_message_present": bool(req.user_message),
        "assistant_message_present": bool(req.assistant_message),
        "text_capture_mode": get_chat_text_capture_mode(),
        "language": req.language,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    with _metrics_lock:
        _feedback_records.append(record)
        _metrics_state["feedback_total"][req.store_id] += 1
        if req.rating == "positive":
            _metrics_state["feedback_positive"][req.store_id] += 1
        else:
            _metrics_state["feedback_negative"][req.store_id] += 1

    logger.info(
        "feedback.received store_id=%s rating=%s comment_present=%s "
        "user_message_present=%s assistant_message_present=%s language=%s",
        req.store_id,
        req.rating,
        bool(comment),
        bool(user_message),
        bool(assistant_message),
        req.language or "unset",
    )
    record_analytics_event(
        req=AnalyticsEventRequest(
            event_type="feedback_submitted",
            store_id=req.store_id,
            session_id=req.session_id,
            conversation_id=req.conversation_id,
            message_id=req.message_id,
            language=req.language,
            rating=req.rating,
        ),
        store_id=req.store_id,
    )
    log_feedback_trace(record)


def build_metrics_summary(store_id: str) -> dict[str, Any]:
    """UI と提出資料に使う、会話本文を含まない最小メトリクス。"""
    with _metrics_lock:
        chat_requests = int(_metrics_state["chat_requests"][store_id])
        feedback_total = int(_metrics_state["feedback_total"][store_id])
        feedback_positive = int(_metrics_state["feedback_positive"][store_id])
        feedback_negative = int(_metrics_state["feedback_negative"][store_id])

    positive_ratio = round(feedback_positive / feedback_total, 3) if feedback_total else None
    return {
        "store_id": store_id,
        "chat_requests": chat_requests,
        "feedback": {
            "total": feedback_total,
            "positive": feedback_positive,
            "negative": feedback_negative,
            "positive_ratio": positive_ratio,
        },
        "quality_targets": {
            "golden_set_pass_rate": GOLDEN_SET_TARGET_LABEL,
            "warm_first_delta": "3秒以内",
            "cold_first_delta": "8秒以内",
        },
        "privacy_note": (
            "評価送信時は、この回答と直前の相談内容を品質改善のため記録します。"
            "個人情報、連絡先、住所、健康状態などは入力しないでください。"
        ),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def log_chat_trace(
    *,
    store_id: str,
    conversation_id: str,
    session_id: str | None,
    user_message: str,
    assistant_message: str,
    product_ids: list[str],
    language: str | None,
    response_status: str,
    latency_ms: int,
    error_type: str | None = None,
) -> None:
    """品質改善用に、本文なしの会話 trace を短期保持ログへ残す。"""
    include_text = should_log_full_chat_text()
    redacted_user_message = redact_sensitive_text(trim_for_log(user_message, max_length=2000))
    redacted_assistant_message = redact_sensitive_text(
        trim_for_log(assistant_message, max_length=6000)
    )
    record = {
        "event": "chat_trace",
        "schema_version": 1,
        "store_id": store_id,
        "conversation_id": conversation_id,
        "session_id_hash": hash_session_id(session_id),
        "user_message": redacted_user_message if include_text else "",
        "assistant_message": redacted_assistant_message if include_text else "",
        "user_message_present": bool(user_message),
        "assistant_message_present": bool(assistant_message),
        "recommendation_product_ids": product_ids[:20],
        "language": language,
        "response_status": response_status,
        "latency_ms": latency_ms,
        "error_type": error_type,
        "text_capture_mode": get_chat_text_capture_mode(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    logger.info("chat_trace %s", json.dumps(record, ensure_ascii=False, sort_keys=True))


def log_feedback_trace(record: dict[str, Any]) -> None:
    """Emit feedback details as a structured trace for Log Analytics reports."""
    trace_record = {
        "event": "feedback.received",
        "schema_version": 1,
        **record,
    }
    logger.info("feedback_trace %s", json.dumps(trace_record, ensure_ascii=False, sort_keys=True))


def normalize_optional_text(value: str | None, *, max_length: int) -> str | None:
    """ログの任意文字列を、空文字なし・長さ上限ありに整える。"""
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:max_length] if normalized else None


def trim_for_log(value: str, *, max_length: int) -> str:
    """本文ログが過剰に大きくならないよう、品質確認に必要な範囲へ絞る。"""
    return value.strip()[:max_length]


def get_chat_text_capture_mode() -> str:
    """Return the configured chat text capture mode for quality logs."""
    mode = os.environ.get("CHAT_TEXT_CAPTURE_MODE", DEFAULT_CHAT_TEXT_CAPTURE_MODE)
    normalized = mode.strip().lower()
    if normalized not in CHAT_TEXT_CAPTURE_MODES:
        logger.warning(
            "Invalid CHAT_TEXT_CAPTURE_MODE=%r; using %s",
            mode,
            DEFAULT_CHAT_TEXT_CAPTURE_MODE,
        )
        return DEFAULT_CHAT_TEXT_CAPTURE_MODE
    return normalized


def should_log_full_chat_text() -> bool:
    """Only explicit all-mode stores normal chat user/assistant text."""
    return get_chat_text_capture_mode() == "all"


def should_capture_feedback_text() -> bool:
    """Feedback mode stores text only when users explicitly submit feedback."""
    return get_chat_text_capture_mode() in {"feedback_only", "all"}


def redact_sensitive_text(text: str) -> str:
    """Best-effort redaction for common contact details before quality logging."""
    redacted = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def hash_session_id(session_id: str | None) -> str | None:
    """Hash browser session IDs before writing structured logs."""
    normalized = normalize_optional_text(session_id, max_length=120)
    if not normalized:
        return None
    salt = os.environ.get("SESSION_HASH_SALT", "")
    return sha256(f"{salt}:{normalized}".encode("utf-8")).hexdigest()


def get_or_create_conversation_id(openai_client: Any, conversation_id: str | None) -> str:
    """継続会話なら既存 ID を使い、初回だけ Foundry 側で会話を作る。"""
    if conversation_id:
        logger.info("chat.conversation.reuse conversation_id=%s", conversation_id)
        return conversation_id

    return create_conversation_id(openai_client)


def create_conversation_id(openai_client: Any) -> str:
    """Foundry 側で新規 conversation を作り、所要時間を記録する。"""
    started_at = time.perf_counter()
    logger.info("chat.conversation.create.start")
    try:
        conversation = openai_client.conversations.create()
    except (AzureError, OpenAIError) as e:
        logger.error(
            "chat.conversation.create.error elapsed_ms=%d",
            elapsed_ms(started_at),
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail="会話の作成に失敗しました。少し時間を置いて再試行してください。",
        ) from e

    logger.info(
        "chat.conversation.create.complete conversation_id=%s elapsed_ms=%d",
        conversation.id,
        elapsed_ms(started_at),
    )
    return conversation.id


async def stream_chat_events(
    *,
    openai_client: Any,
    agent_ref: AgentReference,
    brand_research_agent_ref: AgentReference | None,
    conversation_id: str,
    store_id: str,
    user_message: str,
    agent_message: str,
    session_id: str | None,
    language: str | None,
) -> AsyncIterator[dict[str, str]]:
    """同期 SDK のストリームを worker thread で受け、FastAPI の async SSE へ橋渡しする。

    /chat
      ↓
    stream_chat_events()
      ↓ meta を先に返す
      ↓
    start_stream_worker()
      ↓ background thread が SDK stream を読む
      ↓
    asyncio.Queue
      ↓ delta / error / done を受け渡す
      ↓
    queue_to_sse_events()
      ↓ SSE event に変換
      ↓
    UI に返す
      ↓
    stop_stream_worker()
      ↓ thread と SDK stream を閉じる
    """
    queue: asyncio.Queue[StreamQueueItem] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    force_tool_choice = should_force_brand_research_tool(user_message)
    initial_assistant_text = build_initial_assistant_text(
        user_message,
        force_tool_choice=force_tool_choice,
    )
    worker = start_stream_worker(
        openai_client=openai_client,
        agent_ref=agent_ref,
        brand_research_agent_ref=brand_research_agent_ref,
        conversation_id=conversation_id,
        store_id=store_id,
        user_message=user_message,
        agent_message=agent_message,
        session_id=session_id,
        language=language,
        initial_assistant_text=initial_assistant_text,
        force_tool_choice=force_tool_choice,
        queue=queue,
        loop=loop,
    )

    try:
        yield {"event": "meta", "data": json.dumps({"conversation_id": conversation_id})}
        if initial_assistant_text:
            logger.info(
                "chat.stream.initial_delta conversation_id=%s force_tool_choice=%s",
                conversation_id,
                force_tool_choice,
            )
            yield {"event": "delta", "data": initial_assistant_text}
        if force_tool_choice:
            yield {"event": "status", "data": "調べています"}

        async for event in queue_to_sse_events(queue):
            yield event

    finally:
        stop_stream_worker(worker)


def start_stream_worker(
    *,
    openai_client: Any,
    agent_ref: AgentReference,
    brand_research_agent_ref: AgentReference | None,
    conversation_id: str,
    store_id: str,
    user_message: str,
    agent_message: str,
    session_id: str | None,
    language: str | None,
    initial_assistant_text: str,
    force_tool_choice: bool,
    queue: asyncio.Queue[StreamQueueItem],
    loop: asyncio.AbstractEventLoop,
) -> StreamWorker:
    """blocking stream を background thread で読み、結果を async queue に積む。"""
    stop_event = threading.Event()
    stream_holder: list[Any] = [None]

    def run_stream() -> None:
        """SDK stream の delta/error/done を、SSE 側が読める item に変換する。"""
        started_at = time.perf_counter()
        active_conversation_id = conversation_id
        first_delta_logged = force_tool_choice
        recreated_after_tool_output_error = False
        stream_completed = False
        used_fallback = False
        product_ids: list[str] = []
        assistant_text_parts: list[str] = [initial_assistant_text] if initial_assistant_text else []
        try:
            next_input: str | list[FunctionCallOutput] = agent_message
            previous_response_id: str | None = None

            for tool_round in range(DEFAULT_MAX_TOOL_CALL_ROUNDS + 1):
                stream_attempt = 0
                while True:
                    logger.info(
                        "chat.responses.create.start conversation_id=%s "
                        "tool_round=%d stream_attempt=%d",
                        active_conversation_id,
                        tool_round,
                        stream_attempt,
                    )
                    try:
                        stream = create_agent_response_stream(
                            openai_client=openai_client,
                            agent_ref=agent_ref,
                            conversation_id=active_conversation_id,
                            input_data=next_input,
                            previous_response_id=previous_response_id,
                            force_tool_choice=force_tool_choice and tool_round == 0,
                        )
                    except Exception as exc:
                        if (
                            previous_response_id is None
                            and tool_round == 0
                            and stream_attempt == 0
                            and not recreated_after_tool_output_error
                            and is_unresolved_tool_output_error(exc)
                        ):
                            old_conversation_id = active_conversation_id
                            active_conversation_id = create_conversation_id(openai_client)
                            recreated_after_tool_output_error = True
                            logger.warning(
                                "chat.conversation.recreated_after_unresolved_tool_output "
                                "old_conversation_id=%s new_conversation_id=%s",
                                old_conversation_id,
                                active_conversation_id,
                            )
                            put_stream_item(
                                loop,
                                queue,
                                StreamQueueItem(
                                    kind="meta",
                                    data=json.dumps({"conversation_id": active_conversation_id}),
                                ),
                            )
                            stream = create_agent_response_stream(
                                openai_client=openai_client,
                                agent_ref=agent_ref,
                                conversation_id=active_conversation_id,
                                input_data=next_input,
                                previous_response_id=previous_response_id,
                                force_tool_choice=force_tool_choice and tool_round == 0,
                            )
                        else:
                            raise
                    stream_holder[0] = stream
                    logger.info(
                        "chat.responses.create.stream_ready conversation_id=%s elapsed_ms=%d",
                        active_conversation_id,
                        elapsed_ms(started_at),
                    )
                    delta_emitted = False
                    try:
                        (
                            response_id,
                            tool_outputs,
                            first_delta_logged,
                            delta_emitted,
                        ) = read_agent_stream(
                            stream=stream,
                            openai_client=openai_client,
                            brand_research_agent_ref=brand_research_agent_ref,
                            queue=queue,
                            loop=loop,
                            stop_event=stop_event,
                            conversation_id=active_conversation_id,
                            started_at=started_at,
                            first_delta_logged=first_delta_logged,
                            assistant_text_parts=assistant_text_parts,
                        )
                        break
                    except (AzureError, OpenAIError) as exc:
                        if stop_event.is_set() or not is_retryable_upstream_error(exc):
                            raise
                        if stream_attempt >= len(DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS):
                            fallback = build_brand_research_fallback_response(
                                store_id=store_id,
                                message=user_message,
                                tool_outputs=next_input if isinstance(next_input, list) else [],
                            )
                            if fallback:
                                used_fallback = True
                                logger.warning(
                                    "chat.stream.read.fallback conversation_id=%s "
                                    "tool_round=%d error=%s",
                                    active_conversation_id,
                                    tool_round,
                                    exc,
                                )
                                put_stream_item(
                                    loop,
                                    queue,
                                    StreamQueueItem(kind="delta", data=fallback),
                                )
                                assistant_text_parts.append(fallback)
                                response_id = None
                                tool_outputs = []
                                break
                            raise
                        if delta_emitted and tool_round == 0:
                            raise
                        delay_seconds = DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS[stream_attempt]
                        stream_attempt += 1
                        logger.warning(
                            "chat.stream.read.retry conversation_id=%s tool_round=%d "
                            "stream_attempt=%d delay_seconds=%s error=%s",
                            active_conversation_id,
                            tool_round,
                            stream_attempt,
                            delay_seconds,
                            exc,
                        )
                        close_stream_safely(stream_holder[0])
                        stream_holder[0] = None
                        time.sleep(delay_seconds)

                if stop_event.is_set():
                    break
                if not tool_outputs:
                    product_ids = select_recommendation_product_ids(
                        store_id=store_id,
                        content="".join(assistant_text_parts),
                    )
                    if product_ids:
                        put_stream_item(
                            loop,
                            queue,
                            StreamQueueItem(
                                kind="recommendations",
                                data=json.dumps({"product_ids": product_ids}, ensure_ascii=False),
                            ),
                        )
                    stream_completed = True
                    break
                if tool_round >= DEFAULT_MAX_TOOL_CALL_ROUNDS:
                    raise RuntimeError("tool call の再試行回数が上限に達しました")
                if not response_id:
                    raise RuntimeError(
                        "tool call の継続に必要な response_id を取得できませんでした"
                    )

                previous_response_id = response_id
                next_input = tool_outputs
        except Exception as e:
            if not stop_event.is_set():
                logger.error(
                    "chat.stream.error conversation_id=%s elapsed_ms=%d",
                    active_conversation_id,
                    elapsed_ms(started_at),
                    exc_info=True,
                )
                put_stream_item(
                    loop,
                    queue,
                    StreamQueueItem(kind="error", data=to_user_facing_stream_error(e)),
                )
                log_chat_trace(
                    store_id=store_id,
                    conversation_id=active_conversation_id,
                    session_id=session_id,
                    user_message=user_message,
                    assistant_message="".join(assistant_text_parts),
                    product_ids=product_ids,
                    language=language,
                    response_status="error",
                    latency_ms=elapsed_ms(started_at),
                    error_type=e.__class__.__name__,
                )
        finally:
            if stream_completed:
                latency_ms = elapsed_ms(started_at)
                logger.info(
                    "chat.stream.done conversation_id=%s elapsed_ms=%d",
                    active_conversation_id,
                    latency_ms,
                )
                log_chat_trace(
                    store_id=store_id,
                    conversation_id=active_conversation_id,
                    session_id=session_id,
                    user_message=user_message,
                    assistant_message="".join(assistant_text_parts),
                    product_ids=product_ids,
                    language=language,
                    response_status="fallback" if used_fallback else "success",
                    latency_ms=latency_ms,
                )
            put_stream_item(loop, queue, StreamQueueItem(kind="done"))

    thread = threading.Thread(target=run_stream, daemon=True)
    thread.start()

    return StreamWorker(thread=thread, stop_event=stop_event, stream_holder=stream_holder)


def create_agent_response_stream(
    *,
    openai_client: Any,
    agent_ref: AgentReference,
    conversation_id: str,
    input_data: str | list[FunctionCallOutput],
    previous_response_id: str | None,
    force_tool_choice: bool,
) -> Any:
    """Create a streaming response, optionally continuing after a tool call."""
    kwargs: dict[str, Any] = {
        "input": input_data,
        "extra_body": {"agent_reference": agent_ref.as_dict()},
        "stream": True,
    }
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    else:
        kwargs["conversation"] = conversation_id
    if force_tool_choice:
        kwargs["tool_choice"] = "required"

    last_error: Exception | None = None
    for attempt in range(len(DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS) + 1):
        try:
            return openai_client.responses.create(**kwargs)
        except (AzureError, OpenAIError) as exc:
            last_error = exc
            if not is_retryable_upstream_error(exc):
                raise
            if attempt >= len(DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS):
                raise
            delay_seconds = DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS[attempt]
            logger.warning(
                "chat.responses.create.retry attempt=%d delay_seconds=%s error=%s",
                attempt + 1,
                delay_seconds,
                exc,
            )
            time.sleep(delay_seconds)

    raise RuntimeError("Responses API stream を開始できませんでした") from last_error


def is_retryable_upstream_error(exc: Exception) -> bool:
    """Return true for transient upstream throttling errors worth retrying."""
    message = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or "too many requests" in message or "rate limit" in message


def is_unresolved_tool_output_error(exc: Exception) -> bool:
    """Return true when a reused conversation is blocked by a previous tool call."""
    message = str(exc).lower()
    return "no tool output found for function call" in message


def to_user_facing_stream_error(exc: Exception) -> str:
    """Convert upstream errors into a message suitable for the chat UI."""
    if is_retryable_upstream_error(exc):
        return (
            "ただいま酒あわせAIが混み合っています。"
            "少し時間を置いて、もう一度相談してください。"
        )
    if is_unresolved_tool_output_error(exc):
        return "前回の会話状態が途中で止まっていたため、もう一度相談してください。"
    return "応答の取得中にエラーが発生しました。時間を置いてもう一度お試しください。"


def build_brand_research_fallback_response(
    *,
    store_id: str,
    message: str,
    tool_outputs: list[FunctionCallOutput],
) -> str:
    """Build a deterministic answer when research succeeded but final generation is throttled."""
    research = extract_successful_brand_research(tool_outputs)
    if not research:
        return ""

    products = select_brand_research_fallback_products(
        store_id=store_id,
        message=message,
        limit=1 if re.search(r"1\s*本|一本|ひとつ|一つ", message) else 3,
    )
    if not products:
        return ""

    brand_name = research.get("brand_name") or "その銘柄"
    lead = (
        f"{brand_name}がお好きなら、香りの華やかさ、爽やかな酸、"
        "軽やかな飲み口を軸に近い候補を見ます。\n\n"
    )
    lines = [lead, "店舗ラインナップでは、次の候補から確認するのがよさそうです。\n"]
    for index, product in enumerate(products, start=1):
        lines.append(
            f"{index}. {product['name']}\n"
            f"   - 味わい: {product.get('taste_type') or '公式商品ページで確認'}\n"
            f"   - 合わせ方: {format_product_tags(product.get('pairing_tags', []))}\n"
            f"   - 理由: {product.get('summary') or '店舗の取扱い酒データに基づく候補です。'}\n"
        )
    lines.append(
        "\n正確な価格・在庫は公式オンラインストアで確認してください。"
    )
    return "".join(lines)


def extract_successful_brand_research(
    tool_outputs: list[FunctionCallOutput],
) -> dict[str, Any] | None:
    """Return the first successful brand research payload from tool outputs."""
    for output in tool_outputs:
        output_text = str(get_value(output, "output", ""))
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError:
            continue
        if payload.get("status") == "ok" and payload.get("brand_name"):
            return payload
    return None


def select_brand_research_fallback_products(
    *,
    store_id: str,
    message: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Pick store products that are safe to mention in degraded research responses."""
    profile = load_store_profile(store_id)
    products_by_id = {product["id"]: product for product in profile["products"]}
    preferred_ids = BRAND_RESEARCH_FALLBACK_PRODUCT_IDS.get(store_id, [])
    selected = [
        products_by_id[product_id]
        for product_id in preferred_ids
        if product_id in products_by_id
    ]
    if re.search(r"辛口|ドライ|dry", message, re.IGNORECASE):
        dry_ids = ["ftm-fuyuki-genshu", "ftm-akita-inu-akijungin", "ftm-jinguuji"]
        dry_products = [
            products_by_id[product_id]
            for product_id in dry_ids
            if product_id in products_by_id
        ]
        selected = [*dry_products, *selected]

    deduped = list({product["id"]: product for product in selected}.values())
    return deduped[: max(1, min(limit, 3))]


def format_product_tags(tags: Any) -> str:
    """Return compact display text for a product tag list."""
    if not isinstance(tags, list):
        return "公式商品ページで確認"
    filtered = [str(tag) for tag in tags if str(tag).strip()]
    return "、".join(filtered[:3]) if filtered else "公式商品ページで確認"


def select_recommendation_product_ids(
    *,
    store_id: str,
    content: str,
    max_cards: int = DEFAULT_MAX_RECOMMENDATION_CARDS,
) -> list[str]:
    """Select product card IDs from the final assistant text on the backend."""
    if not content.strip():
        return []

    normalized_content = normalize_recommendation_text(content)
    products = load_store_profile(store_id)["products"]
    alias_counts = build_precise_alias_counts(products)
    matches: list[tuple[int, int, str]] = []
    for product in products:
        match = match_recommendation_product(product, normalized_content, alias_counts)
        if match:
            first_index, score = match
            matches.append((first_index, -score, product["id"]))

    product_ids: list[str] = []
    for _, _, product_id in sorted(matches):
        if product_id in product_ids:
            continue
        product_ids.append(product_id)
        if len(product_ids) >= max_cards:
            break
    return product_ids


def match_recommendation_product(
    product: dict[str, Any],
    normalized_content: str,
    alias_counts: dict[str, int],
) -> tuple[int, int] | None:
    """Return the best precise alias match for one product."""
    best: tuple[int, int] | None = None
    for alias in iter_precise_product_aliases(product):
        normalized_alias = normalize_recommendation_text(alias)
        search_start = 0
        while True:
            first_index = normalized_content.find(normalized_alias, search_start)
            if first_index < 0:
                break
            search_start = first_index + 1
            if (
                not has_alias_boundary(normalized_content, normalized_alias, first_index)
                or is_negated_alias_context(normalized_content, first_index)
                or (
                    alias_counts.get(normalized_alias, 0) > 1
                    and not has_disambiguating_product_context(
                        product,
                        normalized_content,
                        first_index,
                    )
                )
            ):
                continue
            score = len(normalized_alias)
            if best is None or first_index < best[0] or (
                first_index == best[0] and score > best[1]
            ):
                best = (first_index, score)
    return best


def has_disambiguating_product_context(
    product: dict[str, Any],
    normalized_content: str,
    first_index: int,
) -> bool:
    """Allow shared aliases only when the nearby text identifies the exact style."""
    line_start = normalized_content.rfind("\n", 0, first_index) + 1
    line_end = normalized_content.find("\n", first_index)
    if line_end < 0:
        line_end = len(normalized_content)
    window_start = max(line_start, first_index - 40)
    window_end = min(line_end, first_index + 80)
    window = normalized_content[window_start:window_end]
    for field_name in ("style_class", "name"):
        value = normalize_recommendation_text(str(product.get(field_name, "")))
        if value and value in window:
            return True
    return False


def has_alias_boundary(
    normalized_content: str,
    normalized_alias: str,
    first_index: int,
) -> bool:
    """Avoid matching a shorter product name inside a longer product name."""
    end_index = first_index + len(normalized_alias)
    next_char = normalized_content[end_index : end_index + 1]
    if next_char and re.match(r"[a-z0-9]", next_char):
        if re.match(r"\d+[.)．、]", normalized_content[end_index : end_index + 5]):
            return True
        return False
    if next_char and re.match(r"[一-龯々〆ヵヶぁ-んァ-ヴー]", next_char):
        return next_char in "がをはにでとへもやのか"
    return True


def build_precise_alias_counts(products: list[dict[str, Any]]) -> dict[str, int]:
    """Count usable aliases so ambiguous shared aliases can be ignored."""
    counts: dict[str, int] = defaultdict(int)
    for product in products:
        product_aliases: set[str] = set()
        for alias in iter_precise_product_aliases(product):
            product_aliases.add(normalize_recommendation_text(alias))
        for alias in product_aliases:
            counts[alias] += 1
    return counts


def iter_precise_product_aliases(product: dict[str, Any]) -> list[str]:
    """Return aliases precise enough to drive product cards."""
    raw_aliases = [
        product.get("name", ""),
        *product.get("aliases", []),
    ]
    series = str(product.get("series", "")).strip()
    aliases: list[str] = []
    for alias in raw_aliases:
        alias_text = str(alias).strip()
        normalized = normalize_recommendation_text(alias_text)
        if not normalized or alias_text == series:
            continue
        if len(normalized) < 4 and not re.search(r"[A-Za-z0-9]{3,}", normalized):
            continue
        aliases.append(alias_text)
    return list(dict.fromkeys(aliases))


def is_negated_alias_context(normalized_content: str, first_index: int) -> bool:
    """Avoid cards for names that only appear in a negative/comparison exclusion."""
    window = normalized_content[first_index : first_index + 28]
    return any(marker in window for marker in ("ではなく", "じゃなく", "除外", "対象外", "不要"))


def normalize_recommendation_text(value: str) -> str:
    """Normalize Japanese/English mixed product names for matching."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").lower()
    return re.sub(r"[^\S\n]+", "", normalized)


def should_force_brand_research_tool(message: str) -> bool:
    """Nudge tool use for brand-like preference questions without a fixed brand list."""
    mode = get_brand_research_tool_choice_mode()
    if mode == "auto":
        return False
    if mode == "required":
        return True
    return bool(extract_brand_research_candidates(message))


def get_brand_research_tool_choice_mode() -> str:
    """Return how aggressively the BFF nudges agent tool use for preview reliability."""
    mode = os.getenv(
        "BRAND_RESEARCH_TOOL_CHOICE_MODE",
        DEFAULT_BRAND_RESEARCH_TOOL_CHOICE_MODE,
    ).strip().lower()
    if mode not in {"auto", "intent", "required"}:
        return DEFAULT_BRAND_RESEARCH_TOOL_CHOICE_MODE
    return mode


def build_brand_research_intro(message: str) -> str:
    """Build the immediate assistant line shown before a slower research call."""
    candidates = extract_brand_research_candidates(message)
    if not candidates:
        return "そのお酒についての情報を確認しますね。\n\n"
    return f"{'・'.join(candidates[:4])}についての情報を確認しますね。\n\n"


def build_initial_assistant_text(message: str, *, force_tool_choice: bool) -> str:
    """Return the immediate user-visible line before the slower Foundry stream."""
    if force_tool_choice:
        return build_brand_research_intro(message)
    return DEFAULT_CHAT_INITIAL_DELTA


def extract_brand_research_candidates(message: str) -> list[str]:
    """Extract likely external brand names from preference/research phrasing."""
    if not has_brand_research_intent(message):
        return []

    candidates: list[str] = []
    for part in BRAND_CANDIDATE_SPLIT_PATTERN.split(message):
        candidate = normalize_brand_candidate(part)
        if candidate and is_external_brand_candidate(candidate):
            candidates.append(candidate)

    return list(dict.fromkeys(candidates))


def has_brand_research_intent(message: str) -> bool:
    """Detect wording that means the user is referring to a sake brand preference."""
    return any(marker in message for marker in BRAND_RESEARCH_INTENT_MARKERS)


def normalize_brand_candidate(text: str) -> str:
    """Clean a message fragment into a possible brand name."""
    candidate = text.strip(" \t\r\n。！？?「」『』（）()[]【】")
    candidate = BRAND_CANDIDATE_PREFIX_PATTERN.sub("", candidate)
    candidate = BRAND_CANDIDATE_SUFFIX_PATTERN.sub("", candidate)
    candidate = re.sub(r"(?:どんなお酒|どんな酒|教えて|知りたい|です|ます).*$", "", candidate)
    candidate = candidate.strip(" \t\r\n。！？?「」『』（）()[]【】はをがの")
    return re.sub(r"\s+", " ", candidate)


def is_external_brand_candidate(candidate: str) -> bool:
    """Return true for a likely non-store sake brand reference."""
    if not candidate or len(candidate) > 32:
        return False
    if not BRAND_CANDIDATE_ALLOWED_PATTERN.search(candidate):
        return False
    if any(hint in candidate for hint in STORE_BRAND_HINTS):
        return False
    return not any(candidate == term for term in GENERIC_SAKE_TERMS)


def read_agent_stream(
    *,
    stream: Any,
    openai_client: Any,
    brand_research_agent_ref: AgentReference | None,
    queue: asyncio.Queue[StreamQueueItem],
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
    conversation_id: str,
    started_at: float,
    first_delta_logged: bool,
    assistant_text_parts: list[str],
) -> tuple[str | None, list[FunctionCallOutput], bool, bool]:
    """Read one Foundry response stream and resolve any requested function calls."""
    response_id: str | None = None
    function_calls: dict[int, dict[str, str]] = {}
    research_status_sent = False
    delta_emitted = False

    for event in stream:
        if stop_event.is_set():
            break

        response_id = extract_response_id(event, fallback=response_id)
        event_type = get_value(event, "type")
        if not research_status_sent and is_research_tool_stream_event(event):
            put_stream_item(
                loop,
                queue,
                StreamQueueItem(kind="status", data="調べています"),
            )
            research_status_sent = True
        if event_type == "response.output_text.delta":
            delta = get_value(event, "delta", "")
            if not first_delta_logged:
                logger.info(
                    "chat.stream.first_delta conversation_id=%s elapsed_ms=%d",
                    conversation_id,
                    elapsed_ms(started_at),
                )
                first_delta_logged = True
            put_stream_item(
                loop,
                queue,
                StreamQueueItem(kind="delta", data=delta),
            )
            assistant_text_parts.append(delta)
            delta_emitted = True
            continue

        update_function_call_state(function_calls, event)

    if function_calls and not first_delta_logged:
        intro = build_brand_research_intro_from_function_calls(function_calls)
        if intro:
            put_stream_item(loop, queue, StreamQueueItem(kind="delta", data=intro))
            assistant_text_parts.append(intro)
            first_delta_logged = True

    tool_outputs = build_tool_outputs(
        function_calls=function_calls,
        openai_client=openai_client,
        brand_research_agent_ref=brand_research_agent_ref,
        conversation_id=conversation_id,
        response_id=response_id,
    )
    if function_calls and len(tool_outputs) < len(function_calls):
        missing_count = len(function_calls) - len(tool_outputs)
        logger.error(
            "chat.tool_outputs.missing conversation_id=%s response_id=%s "
            "function_call_count=%d missing_count=%d",
            conversation_id,
            response_id or "unknown",
            len(function_calls),
            missing_count,
        )
        raise RuntimeError(
            "銘柄リサーチの継続に必要な tool call 情報を取得できませんでした。"
            "もう一度送信してください。"
        )
    if tool_outputs:
        logger.info(
            "chat.tool_outputs.created conversation_id=%s count=%d",
            conversation_id,
            len(tool_outputs),
        )

    return response_id, tool_outputs, first_delta_logged, delta_emitted


def build_brand_research_intro_from_function_calls(
    function_calls: dict[int, dict[str, str]],
) -> str:
    """Build a user-visible intro after the agent autonomously selected research."""
    brand_names: list[str] = []
    for call in function_calls.values():
        if call.get("name") != BRAND_RESEARCH_TOOL_NAME:
            continue
        try:
            arguments = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        brand_name = str(arguments.get("brand_name", "")).strip()
        if brand_name:
            brand_names.append(brand_name)

    if not brand_names:
        return ""
    return f"{'・'.join(list(dict.fromkeys(brand_names))[:4])}についての情報を確認しますね。\n\n"


def is_research_tool_stream_event(event: Any) -> bool:
    """Detect tool/A2A events early enough to update the UI while research runs."""
    event_type = str(get_value(event, "type", ""))
    if event_type.startswith("response.function_call_arguments."):
        return True
    if "a2a" in event_type and "call" in event_type:
        return True

    item = get_value(event, "item")
    item_type = str(get_value(item, "type", ""))
    return item_type == "function_call" or ("a2a" in item_type and "call" in item_type)


def update_function_call_state(
    function_calls: dict[int, dict[str, str]],
    event: Any,
) -> None:
    """Accumulate function-call streaming events from the Responses API."""
    event_type = get_value(event, "type")
    output_index = int(get_value(event, "output_index", len(function_calls)))

    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = get_value(event, "item")
        if get_value(item, "type") != "function_call":
            return
        state = function_calls.setdefault(output_index, {"arguments": ""})
        state["name"] = get_value(item, "name", state.get("name", ""))
        state["call_id"] = get_value(item, "call_id", state.get("call_id", ""))
        state["arguments"] = get_value(item, "arguments", state.get("arguments", ""))
        return

    if event_type == "response.function_call_arguments.delta":
        state = function_calls.setdefault(output_index, {"arguments": ""})
        state["name"] = get_value(event, "name", state.get("name", ""))
        state["call_id"] = get_value(event, "call_id", state.get("call_id", ""))
        state["arguments"] = state.get("arguments", "") + get_value(event, "delta", "")
        return

    if event_type == "response.function_call_arguments.done":
        state = function_calls.setdefault(output_index, {"arguments": ""})
        item = get_value(event, "item")
        if item is not None:
            state["name"] = get_value(item, "name", state.get("name", ""))
            state["call_id"] = get_value(item, "call_id", state.get("call_id", ""))
            state["arguments"] = get_value(item, "arguments", state.get("arguments", ""))
        else:
            state["name"] = get_value(event, "name", state.get("name", ""))
            state["call_id"] = get_value(event, "call_id", state.get("call_id", ""))
            state["arguments"] = get_value(event, "arguments", state.get("arguments", ""))


def build_tool_outputs(
    *,
    function_calls: dict[int, dict[str, str]],
    openai_client: Any,
    brand_research_agent_ref: AgentReference | None,
    conversation_id: str,
    response_id: str | None,
) -> list[FunctionCallOutput]:
    """Execute supported function calls and format outputs for the next response."""
    outputs: list[FunctionCallOutput] = []
    for call in function_calls.values():
        call_id = call.get("call_id")
        if not call_id:
            continue

        output = execute_function_call(
            name=call.get("name", ""),
            arguments=call.get("arguments", ""),
            openai_client=openai_client,
            brand_research_agent_ref=brand_research_agent_ref,
            trace_context={
                "conversation_id": conversation_id,
                "response_id": response_id,
                "tool_call_id": call_id,
                "tool_name": call.get("name", ""),
            },
        )
        outputs.append(
            FunctionCallOutput(
                type="function_call_output",
                call_id=call_id,
                output=output,
            )
        )

    return outputs


def execute_function_call(
    *,
    name: str,
    arguments: str,
    openai_client: Any,
    brand_research_agent_ref: AgentReference | None,
    trace_context: dict[str, Any] | None = None,
) -> str:
    """Dispatch Foundry function calls to local executors."""
    if name == BRAND_RESEARCH_TOOL_NAME:
        try:
            return execute_brand_research_tool(
                openai_client=openai_client,
                brand_research_agent_ref=brand_research_agent_ref,
                arguments=arguments,
                trace_context=trace_context,
            )
        except Exception as exc:
            logger.exception(
                "brand_research.tool_execution_error trace_context=%s",
                json.dumps(trace_context or {}, ensure_ascii=False, sort_keys=True),
            )
            return json.dumps(
                {
                    "status": "tool_execution_error",
                    "summary": "外部銘柄リサーチ中に一時的なエラーが発生しました。",
                    "error_type": exc.__class__.__name__,
                },
                ensure_ascii=False,
            )

    return json.dumps(
        {
            "status": "unsupported_tool",
            "summary": f"未対応の tool が要求されました: {name}",
        },
        ensure_ascii=False,
    )


def extract_response_id(event: Any, *, fallback: str | None) -> str | None:
    """Get the response ID from common Responses streaming event shapes."""
    response_id = get_value(event, "response_id")
    if response_id:
        return response_id

    response = get_value(event, "response")
    response_id = get_value(response, "id")
    return response_id or fallback


def get_value(source: Any, key: str, default: Any = None) -> Any:
    """Read an attribute from SDK models or dict-like test doubles."""
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def put_stream_item(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[StreamQueueItem],
    item: StreamQueueItem,
) -> None:
    """別 thread から async queue を安全に更新する。"""
    loop.call_soon_threadsafe(queue.put_nowait, item)


def elapsed_ms(started_at: float) -> int:
    """perf_counter の開始時刻から経過ミリ秒を整数で返す。"""
    return int((time.perf_counter() - started_at) * 1000)


async def queue_to_sse_events(
    queue: asyncio.Queue[StreamQueueItem],
) -> AsyncIterator[dict[str, str]]:
    """worker が積んだ stream item を、FastAPI が返す SSE event に変換する。"""
    while True:
        item = await queue.get()
        if item.kind == "done":
            yield {"event": "done", "data": ""}
            return
        if item.kind == "error":
            logger.error(f"Stream error: {item.data}")
            yield {"event": "error", "data": item.data}
            return
        if item.kind == "meta":
            yield {"event": "meta", "data": item.data}
            continue
        if item.kind == "status":
            yield {"event": "status", "data": item.data}
            continue
        if item.kind == "recommendations":
            yield {"event": "recommendations", "data": item.data}
            continue

        yield {"event": "delta", "data": item.data}


def stop_stream_worker(worker: StreamWorker) -> None:
    """クライアント切断・正常終了のどちらでも、SDK stream と thread を閉じる。"""
    worker.stop_event.set()
    close_stream_safely(worker.stream_holder[0])
    worker.thread.join(timeout=2)


def close_stream_safely(stream: Any) -> None:
    """Best-effort close for SDK stream objects."""
    if stream is None:
        return
    try:
        stream.close()
    except Exception as exc:
        logger.debug("chat.stream.close_ignored error=%s", exc)



