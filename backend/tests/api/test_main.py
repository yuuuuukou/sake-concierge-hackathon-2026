import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core.exceptions import AzureError
from fastapi.testclient import TestClient

from src.api import main, store_catalog


@pytest.fixture(autouse=True)
def _clear_app_state(monkeypatch):
    """テスト間で起動済み状態が混ざらないよう、共有 state を毎回空にする。"""
    fixture_data_root = Path(__file__).resolve().parents[1] / "fixtures" / "stores"
    monkeypatch.setenv("STORE_DATA_SOURCE", "local")
    monkeypatch.setenv("STORE_DATA_ROOT", str(fixture_data_root))
    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TRUST_FORWARDED_HEADERS", raising=False)
    monkeypatch.delenv("EVENT_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("EVENT_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("CHAT_TEXT_CAPTURE_MODE", raising=False)
    monkeypatch.delenv("SESSION_HASH_SALT", raising=False)
    main._state.clear()
    main._telemetry_configured = False
    main._openai_instrumented = False
    main.reset_chat_rate_limit_state()
    main.reset_feedback_metrics_state()
    yield
    main._state.clear()
    main._telemetry_configured = False
    main._openai_instrumented = False
    main.reset_chat_rate_limit_state()
    main.reset_feedback_metrics_state()


class _FakeAgentRef:
    """Foundry AgentReference の as_dict だけを持つ代替。"""

    def as_dict(self) -> dict[str, str]:
        """responses.create に渡す agent 指定を固定値で返す。"""
        return {"type": "agent_reference", "name": "sake-concierge", "version": "1"}


class _FakeConversations:
    """Foundry 側の conversation 作成を外部通信なしで検証する代替。"""

    def __init__(self, error: Exception | None = None) -> None:
        """正常系と異常系を、同じ Fake で切り替えられるようにする。"""
        self.error = error
        self.create_calls = 0

    def create(self) -> SimpleNamespace:
        """新規会話 ID を固定値で返す。"""
        self.create_calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(id="conv_test")


class _FakeStream:
    """OpenAI SDK の streaming response っぽく振る舞う最小の代替。"""

    def __init__(self, events: list[SimpleNamespace] | None = None) -> None:
        """delta イベント2件だけを返す stream として準備する。"""
        self.closed = False
        self.events = events or [
            SimpleNamespace(type="response.output_text.delta", delta="辛口なら"),
            SimpleNamespace(type="response.output_text.delta", delta="刈穂です"),
        ]

    def __iter__(self):
        """for event in stream で同期的に読めるようにする。"""
        return iter(self.events)

    def close(self) -> None:
        """本物の stream と同じく、終了時に close できるようにする。"""
        self.closed = True


class _FailingStream(_FakeStream):
    """Iteration raises an upstream error after responses.create succeeded."""

    def __init__(self, error: Exception) -> None:
        """Prepare a stream that fails while being consumed."""
        super().__init__(events=[])
        self.error = error

    def __iter__(self):
        """Simulate a streaming read error."""
        raise self.error


class _FakeResponses:
    """Foundry Agent への responses.create 呼び出しだけを記録する代替。"""

    def __init__(
        self,
        error: Exception | None = None,
        errors: list[Exception | None] | None = None,
    ) -> None:
        """正常 stream と stream 開始失敗を切り替えられるようにする。"""
        self.error = error
        self.errors = errors or []
        self.calls: list[dict] = []
        self.stream = _FakeStream()
        self.streams: list[_FakeStream] = []

    def create(self, **kwargs) -> _FakeStream:
        """外部サービスには出さず、固定の stream を返す。"""
        self.calls.append(kwargs)
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        if self.error:
            raise self.error
        if self.streams:
            self.stream = self.streams.pop(0)
            return self.stream
        return self.stream


class _FakeOpenAI:
    """main.py が使う conversations / responses だけを持つ最小 OpenAI client。"""

    def __init__(
        self,
        conversation_error: Exception | None = None,
        response_error: Exception | None = None,
        response_errors: list[Exception | None] | None = None,
        response_streams: list[_FakeStream] | None = None,
    ) -> None:
        """会話作成の正常系と異常系を差し替えられるようにする。"""
        self.conversations = _FakeConversations(error=conversation_error)
        self.responses = _FakeResponses(error=response_error, errors=response_errors)
        self.responses.streams = response_streams or []


class _FakeProjectAgents:
    """Foundry SDK の Agent version 確認だけを検証する代替。"""

    def __init__(self, error: AzureError | None = None) -> None:
        """Agent version 取得の正常系と異常系を切り替えられるようにする。"""
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def get_version(self, agent_name: str, agent_version: str) -> SimpleNamespace:
        """事前作成済み Agent 風の値を返す。"""
        self.calls.append((agent_name, agent_version))
        if self.error:
            raise self.error
        return SimpleNamespace(id="agent_123", name=agent_name, version=agent_version)


class _FakeProject:
    """main.py が起動時に使う project.agents だけを持つ最小 Project。"""

    def __init__(self, agent_error: AzureError | None = None) -> None:
        """Agent version 確認の結果だけを差し替えられるようにする。"""
        self.agents = _FakeProjectAgents(error=agent_error)


def extract_structured_log(caplog_text: str, prefix: str) -> dict:
    """logger の `prefix {json}` 形式から、最初の JSON payload を取り出す。"""
    marker = f"{prefix} "
    for line in caplog_text.splitlines():
        if marker not in line:
            continue
        return json.loads(line.split(marker, 1)[1])
    raise AssertionError(f"{prefix} log was not found")


def assert_chat_trace_text_suppressed(chat_trace: dict) -> None:
    """feedback_only では通常 chat_trace に本文を残さない。"""
    assert chat_trace["user_message"] == ""
    assert chat_trace["assistant_message"] == ""
    assert chat_trace["user_message_present"] is True
    assert chat_trace["assistant_message_present"] is True
    assert chat_trace["text_capture_mode"] == "feedback_only"


def test_health_returns_ok_without_foundry_state() -> None:
    """ヘルスチェックは、Foundry 接続前でもコンテナ生存確認に使える。"""
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_store_profile_returns_fukunotomo_catalog_cards() -> None:
    """店舗別 UI 用に、CSV から商品カード情報を返す。"""
    client = TestClient(main.app)

    response = client.get("/api/stores/fukunotomo")

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == "fukunotomo"
    assert body["display_name"] == "サンプル店舗"
    assert body["product_count"] > 0
    assert body["quick_prompts"]["ja"]
    product = next(
        product for product in body["products"] if product["id"] == "ftm-fuyuki-fff-genshu"
    )
    assert product["official_url"] == ""
    assert product["price_label"] == "価格は公式確認"
    assert product["stock_label"] == "在庫は公式商品ページで確認ください"


def test_official_url_sanitizer_allows_only_configured_https_urls() -> None:
    """商品カードリンクは、CSV由来でも公式 HTTPS URL だけを表示する。"""
    assert (
        store_catalog.sanitize_official_url("https://example.com/products/sample-00639")
        == "https://example.com/products/sample-00639"
    )
    assert store_catalog.sanitize_official_url("javascript:alert(1)") == ""
    assert store_catalog.sanitize_official_url("http://example.com/products/sample-00639") == ""
    assert store_catalog.sanitize_official_url("https://evil.example/products/sample-00639") == ""


def test_store_profile_returns_404_for_unknown_store() -> None:
    """未設定 store slug は SPA fallback ではなく API として 404 にする。"""
    client = TestClient(main.app)

    response = client.get("/api/stores/unknown-store")

    assert response.status_code == 404
    assert response.json()["detail"] == "店舗データが見つかりません"


def test_unknown_backend_path_does_not_return_spa_html() -> None:
    """未知の backend/API パスでは React HTML ではなく 404 を返す。"""
    client = TestClient(main.app)

    response = client.get("/api/not-real")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "Not Found"


def test_security_headers_are_added_to_responses() -> None:
    """公開レスポンスには基本的なブラウザ防御ヘッダーを付ける。"""
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_feedback_updates_metrics_with_chat_content(caplog) -> None:
    """feedback は会話抜粋と評価を品質改善用 trace に残す。"""
    client = TestClient(main.app)

    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/api/feedback",
            json={
                "store_id": "fukunotomo",
                "session_id": "session_test",
                "conversation_id": "conv_test",
                "message_id": "assistant-1",
                "rating": "positive",
                "comment": "商品リンクが助かる。連絡先 test@example.com",
                "user_message": "甘口で飲みやすいものは？ 090-1234-5678",
                "assistant_message": "冬樹FFFがおすすめです。https://example.test/ 123-4567",
                "language": "ja",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["metrics"]["feedback"]["total"] == 1
    assert body["metrics"]["feedback"]["positive_ratio"] == 1
    assert main._feedback_records[0]["comment_present"] is True
    assert main._feedback_records[0]["session_id_hash"] == main.hash_session_id("session_test")
    assert main._feedback_records[0]["comment"] == "商品リンクが助かる。連絡先 [EMAIL]"
    assert main._feedback_records[0]["user_message"] == "甘口で飲みやすいものは？ [PHONE]"
    assert main._feedback_records[0]["assistant_message"] == (
        "冬樹FFFがおすすめです。[URL] [POSTAL_CODE]"
    )
    assert main._feedback_records[0]["text_capture_mode"] == "feedback_only"
    assert "feedback_trace" in caplog.text
    assert "analytics_event" in caplog.text
    assert "甘口で飲みやすいものは？ [PHONE]" in caplog.text
    assert "090-1234-5678" not in caplog.text
    assert "test@example.com" not in caplog.text


def test_store_metrics_counts_chat_requests() -> None:
    """チャット回数は本文を保存せず store 単位で数える。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()
    client = TestClient(main.app)

    response = client.post(
        "/chat",
        json={"message": "辛口のおすすめは？", "store_id": "fukunotomo"},
    )
    metrics_response = client.get("/api/stores/fukunotomo/metrics")

    assert response.status_code == 200
    assert metrics_response.status_code == 200
    assert metrics_response.json()["chat_requests"] == 1
    assert (
        metrics_response.json()["privacy_note"]
        == "評価送信時は、この回答と直前の相談内容を品質改善のため記録します。"
        "個人情報、連絡先、住所、健康状態などは入力しないでください。"
    )


def test_analytics_event_logs_bodyless_kpi_payload(caplog) -> None:
    """KPI イベントは本文を含めず、KQL で parse しやすい JSON trace にする。"""
    client = TestClient(main.app)

    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/api/analytics/events",
            json={
                "event_type": "product_link_clicked",
                "store_id": "fukunotomo",
                "session_id": "session_test",
                "conversation_id": "conv_test",
                "message_id": "assistant-1",
                "product_id": "ftm-fuyuki-fff-genshu",
                "recommendation_rank": 1,
                "official_url": "https://example.com/products/sample-00639",
                "page_path": "/s/fukunotomo",
                "language": "ja",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    record = extract_structured_log(caplog.text, "analytics_event")
    assert record["event"] == "analytics_event"
    assert record["event_type"] == "product_link_clicked"
    assert record["store_id"] == "fukunotomo"
    assert record["session_id_hash"] == main.hash_session_id("session_test")
    assert "session_id" not in record
    assert record["product_id"] == "ftm-fuyuki-fff-genshu"
    assert "甘口" not in caplog.text


def test_a2a_agent_card_returns_public_rpc_url(monkeypatch) -> None:
    """A2A connection が discovery できる Agent Card を返す。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    client = TestClient(main.app)

    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    body = response.json()
    assert body["protocolVersion"] == "0.3.0"
    assert body["preferredTransport"] == "JSONRPC"
    assert body["url"] == "https://example.test/a2a"
    assert body["skills"][0]["id"] == "research-sake-brand"


def test_a2a_message_send_requires_api_key() -> None:
    """A2A_API_KEY 未設定の公開環境では JSON-RPC endpoint を隠す。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "req-1", "method": "message/send"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"
    assert fake_openai.conversations.create_calls == 0


def test_a2a_message_send_returns_brand_research_message(monkeypatch) -> None:
    """Foundry A2A preview から呼ばれる message/send に同期応答する。"""
    monkeypatch.setenv("A2A_API_KEY", "test-a2a-key")
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg-1",
                    "parts": [{"kind": "text", "text": "新政 No.6 が好き"}],
                }
            },
        },
        headers={"x-api-key": "test-a2a-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "req-1"
    assert body["result"]["kind"] == "message"
    assert body["result"]["role"] == "agent"
    assert "新政 No.6" in body["result"]["parts"][0]["text"]
    assert "research_agent_not_configured" == body["result"]["metadata"]["research_status"]


def test_find_missing_env_vars_reports_unset_required_env(monkeypatch) -> None:
    """起動時に必要な環境変数の不足をまとめて見せる。"""
    for name in main.REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_AGENT_NAME", "sake-concierge")

    assert main.find_missing_env_vars() == [
        "AZURE_AIPROJECT_ENDPOINT",
        "AZURE_AGENT_VERSION",
    ]


def test_configure_application_insights_skips_when_connection_string_is_unset(
    monkeypatch,
    caplog,
) -> None:
    """ローカル未設定時は App Insights を有効化せず、起動を妨げない。"""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        configured = main.configure_application_insights()

    assert configured is False
    assert main._telemetry_configured is False
    assert "Application Insights telemetry disabled" in caplog.text


def test_configure_application_insights_uses_connection_string_and_logger_name(
    monkeypatch,
) -> None:
    """接続文字列がある場合、logger と request 除外設定を telemetry 対象にする。"""
    calls: list[dict] = []
    fake_monitor = SimpleNamespace(
        configure_azure_monitor=lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_monitor)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=test")
    monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)
    monkeypatch.delenv("OTEL_PYTHON_EXCLUDED_URLS", raising=False)

    configured = main.configure_application_insights()

    assert configured is True
    assert main._telemetry_configured is True
    assert calls == [
        {
            "connection_string": "InstrumentationKey=test",
            "logger_name": "sake_concierge",
            "instrumentation_options": main.AZURE_MONITOR_INSTRUMENTATION_OPTIONS,
        }
    ]
    assert os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"] == main.DEFAULT_FASTAPI_EXCLUDED_URLS
    assert os.environ["OTEL_PYTHON_EXCLUDED_URLS"] == main.DEFAULT_FASTAPI_EXCLUDED_URLS


def test_configure_fastapi_excluded_urls_respects_explicit_env(monkeypatch) -> None:
    """運用側で除外 URL を広げた場合は、明示値を優先する。"""
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "/health,/ready")
    monkeypatch.delenv("OTEL_PYTHON_EXCLUDED_URLS", raising=False)

    excluded_urls = main.configure_fastapi_excluded_urls()

    assert excluded_urls == "/health,/ready"
    assert os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"] == "/health,/ready"
    assert os.environ["OTEL_PYTHON_EXCLUDED_URLS"] == "/health,/ready"


def test_instrument_fastapi_app_skips_when_telemetry_is_unconfigured(caplog) -> None:
    """App Insights 未設定時は request telemetry も無効のままにする。"""
    main._telemetry_configured = False

    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        configured = main.instrument_fastapi_app(main.app)

    assert configured is False
    assert "FastAPI request telemetry disabled" in caplog.text


def test_instrument_fastapi_app_uses_fastapi_instrumentor(monkeypatch) -> None:
    """App Insights 設定済みなら FastAPI app を明示的に instrument する。"""
    calls: list[dict] = []

    class _FakeFastAPIInstrumentor:
        @staticmethod
        def instrument_app(app, **kwargs) -> None:
            calls.append({"app": app, **kwargs})

    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.instrumentation.fastapi",
        SimpleNamespace(FastAPIInstrumentor=_FakeFastAPIInstrumentor),
    )
    main._telemetry_configured = True
    monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)

    configured = main.instrument_fastapi_app(main.app)

    assert configured is True
    assert calls == [
        {
            "app": main.app,
            "excluded_urls": main.DEFAULT_FASTAPI_EXCLUDED_URLS,
        }
    ]


def test_instrument_openai_sdk_skips_when_telemetry_is_unconfigured(caplog) -> None:
    """App Insights 未設定時は OpenAI SDK 自動計装も無効のままにする。"""
    main._telemetry_configured = False

    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        configured = main.instrument_openai_sdk()

    assert configured is False
    assert main._openai_instrumented is False
    assert "OpenAI SDK telemetry disabled" in caplog.text


def test_instrument_openai_sdk_uses_openai_instrumentor(monkeypatch) -> None:
    """App Insights 設定済みなら OpenAI SDK の自動計装も有効化する。"""
    calls: list[str] = []

    class _FakeOpenAIInstrumentor:
        def instrument(self) -> None:
            calls.append("instrument")

    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.instrumentation.openai_v2",
        SimpleNamespace(OpenAIInstrumentor=_FakeOpenAIInstrumentor),
    )
    main._telemetry_configured = True

    configured = main.instrument_openai_sdk()

    assert configured is True
    assert main._openai_instrumented is True
    assert calls == ["instrument"]


def test_load_agent_reference_returns_foundry_agent_reference(monkeypatch) -> None:
    """起動時に、事前作成済み Agent の name/version を AgentReference にする。"""
    monkeypatch.setenv("AZURE_AGENT_NAME", "sake-concierge")
    monkeypatch.setenv("AZURE_AGENT_VERSION", "7")
    fake_project = _FakeProject()

    agent_ref = main.load_agent_reference(fake_project)

    assert agent_ref.name == "sake-concierge"
    assert agent_ref.version == "7"
    assert fake_project.agents.calls == [("sake-concierge", "7")]


def test_load_agent_reference_raises_runtime_error_when_agent_is_missing(monkeypatch) -> None:
    """Agent が存在しない場合、起動時に setup_agent.py の実行を促す。"""
    monkeypatch.setenv("AZURE_AGENT_NAME", "sake-concierge")
    monkeypatch.setenv("AZURE_AGENT_VERSION", "7")
    fake_project = _FakeProject(agent_error=AzureError("missing"))

    with pytest.raises(RuntimeError, match="backend/scripts/setup_agent.py"):
        main.load_agent_reference(fake_project)


def test_chat_requires_initialized_state() -> None:
    """/chat は起動時に準備した Agent 状態がない場合、会話処理へ進まない。"""
    client = TestClient(main.app)

    response = client.post("/chat", json={"message": "辛口のおすすめは？"})

    assert response.status_code == 503
    assert response.json()["detail"] == "サービスが初期化されていません"


def test_create_chat_conversation_returns_conversation_id_without_streaming(caplog) -> None:
    """画面表示時点で、メッセージ送信前に conversation_id だけを作れる。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post("/chat/conversation")

    assert response.status_code == 200
    assert response.json() == {"conversation_id": "conv_test"}
    assert fake_openai.conversations.create_calls == 1
    assert fake_openai.responses.calls == []
    assert "chat.conversation.create.complete conversation_id=conv_test elapsed_ms=" in caplog.text


def test_chat_returns_502_when_conversation_creation_fails() -> None:
    """新規会話を作れない場合は、SSE 開始前に 502 として返す。"""
    fake_openai = _FakeOpenAI(conversation_error=AzureError("boom"))
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post("/chat", json={"message": "辛口のおすすめは？"})

    assert response.status_code == 502
    assert "会話の作成に失敗しました" in response.json()["detail"]
    assert "boom" not in response.json()["detail"]


def test_chat_rejects_overlong_message_before_model_call() -> None:
    """巨大なチャット本文は、Foundry へ渡す前に 422 で止める。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post("/chat", json={"message": "あ" * 1201})

    assert response.status_code == 422
    assert fake_openai.conversations.create_calls == 0
    assert fake_openai.responses.calls == []


def test_chat_streams_meta_delta_done_without_retriever(caplog) -> None:
    """/chat は request-time に Retriever を呼ばず、作成済み Agent へ中継する。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post("/chat", json={"message": "辛口のおすすめは？"})

    assert response.status_code == 200
    assert 'event: meta\r\ndata: {"conversation_id": "conv_test"}' in response.text
    assert f"event: delta\r\ndata: {main.DEFAULT_CHAT_INITIAL_DELTA.rstrip()}" in response.text
    assert "event: delta\r\ndata: 辛口なら" in response.text
    assert "event: delta\r\ndata: 刈穂です" in response.text
    assert "event: done" in response.text
    assert response.text.index(main.DEFAULT_CHAT_INITIAL_DELTA.rstrip()) < response.text.index(
        "辛口なら"
    )

    assert fake_openai.conversations.create_calls == 1
    assert fake_openai.responses.calls == [
        {
            "conversation": "conv_test",
            "input": "辛口のおすすめは？",
            "extra_body": {
                "agent_reference": {
                    "type": "agent_reference",
                    "name": "sake-concierge",
                    "version": "1",
                }
            },
            "stream": True,
        }
    ]
    assert fake_openai.responses.stream.closed is True
    assert "chat.conversation.create.start" in caplog.text
    assert "chat.conversation.create.complete conversation_id=conv_test elapsed_ms=" in caplog.text
    assert (
        "chat.stream.initial_delta conversation_id=conv_test force_tool_choice=False"
        in caplog.text
    )
    assert "chat.responses.create.start conversation_id=conv_test" in caplog.text
    assert "chat.responses.create.stream_ready conversation_id=conv_test elapsed_ms=" in caplog.text
    assert "chat.stream.first_delta conversation_id=conv_test elapsed_ms=" in caplog.text
    assert "chat.stream.done conversation_id=conv_test elapsed_ms=" in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert_chat_trace_text_suppressed(chat_trace)
    assert chat_trace["response_status"] == "success"


def test_chat_trace_can_capture_full_text_in_all_mode(monkeypatch, caplog) -> None:
    """開発検証向け all mode だけ通常 chat_trace に本文を残せる。"""
    monkeypatch.setenv("CHAT_TEXT_CAPTURE_MODE", "all")
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()
    client = TestClient(main.app)

    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post("/chat", json={"message": "連絡先は test@example.com です"})

    assert response.status_code == 200
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert chat_trace["user_message"] == "連絡先は [EMAIL] です"
    assert chat_trace["assistant_message"] == (
        f"{main.DEFAULT_CHAT_INITIAL_DELTA}辛口なら刈穂です"
    )
    assert chat_trace["text_capture_mode"] == "all"
    assert "test@example.com" not in caplog.text


def test_chat_streams_error_event_when_agent_stream_fails(caplog) -> None:
    """Agent stream 開始後の失敗は、SSE の error event として返す。"""
    fake_openai = _FakeOpenAI(response_error=RuntimeError("stream boom"))
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post("/chat", json={"message": "辛口のおすすめは？"})

    assert response.status_code == 200
    assert 'event: meta\r\ndata: {"conversation_id": "conv_test"}' in response.text
    assert "event: error" in response.text
    assert "応答の取得中にエラーが発生しました" in response.text
    assert "stream boom" not in response.text
    assert "event: done" not in response.text
    assert "chat.stream.error conversation_id=conv_test elapsed_ms=" in caplog.text
    assert "chat.stream.done conversation_id=conv_test elapsed_ms=" not in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert_chat_trace_text_suppressed(chat_trace)
    assert chat_trace["response_status"] == "error"
    assert chat_trace["error_type"] == "RuntimeError"


def test_chat_recreates_conversation_when_reused_conversation_has_pending_tool_call(
    caplog,
) -> None:
    """前回の tool call が未完了な conversation は、新規 conversation で同じ相談を救済する。"""
    stale_tool_error = AzureError(
        "Error code: 400 - {'error': {'message': "
        "'No tool output found for function call call_stale.'}}"
    )
    fake_openai = _FakeOpenAI(response_errors=[stale_tool_error, None])
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/chat",
            json={"message": "新政が好きです", "conversation_id": "conv_stale"},
        )

    assert response.status_code == 200
    assert 'event: meta\r\ndata: {"conversation_id": "conv_stale"}' in response.text
    assert 'event: meta\r\ndata: {"conversation_id": "conv_test"}' in response.text
    assert "event: delta\r\ndata: 辛口なら" in response.text
    assert "event: done" in response.text
    assert "No tool output found" not in response.text
    assert fake_openai.conversations.create_calls == 1
    assert len(fake_openai.responses.calls) == 2
    assert fake_openai.responses.calls[0]["conversation"] == "conv_stale"
    assert fake_openai.responses.calls[1]["conversation"] == "conv_test"
    assert "chat.conversation.recreated_after_unresolved_tool_output" in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert chat_trace["conversation_id"] == "conv_test"
    assert_chat_trace_text_suppressed(chat_trace)


def test_chat_continues_function_call_when_metadata_is_on_arguments_done_event() -> None:
    """SDK event 形状差で call_id が item ではなく done event 側に来ても継続できる。"""
    first_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_1")),
            SimpleNamespace(
                type="response.function_call_arguments.done",
                output_index=0,
                call_id="call_event",
                name="research_sake_brand",
                arguments='{"brand_name":"新政","user_context":"好き"}',
            ),
        ]
    )
    second_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_2")),
            SimpleNamespace(type="response.output_text.delta", delta="冬樹FFF が近い候補です"),
        ]
    )
    fake_openai = _FakeOpenAI(response_streams=[first_stream, second_stream])
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post(
        "/chat",
        json={"message": "新政が好き", "conversation_id": "conv_existing"},
    )

    assert response.status_code == 200
    assert "event: delta\r\ndata: 新政についての情報を確認しますね。" in response.text
    assert "event: delta\r\ndata: 冬樹FFF が近い候補です" in response.text
    assert "event: done" in response.text
    assert len(fake_openai.responses.calls) == 2
    second_call = fake_openai.responses.calls[1]
    assert second_call["previous_response_id"] == "resp_1"
    assert second_call["input"][0]["type"] == "function_call_output"
    assert second_call["input"][0]["call_id"] == "call_event"


def test_chat_errors_when_function_call_metadata_cannot_create_tool_output(caplog) -> None:
    """tool call を検知したのに output を作れない場合は、正常完了にしない。"""
    first_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_1")),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=0,
                name="research_sake_brand",
                delta='{"brand_name":"新政","user_context":"好き"}',
            ),
        ]
    )
    fake_openai = _FakeOpenAI(response_streams=[first_stream])
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/chat",
            json={"message": "新政が好き", "conversation_id": "conv_existing"},
        )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "応答の取得中にエラーが発生しました" in response.text
    assert "event: done" not in response.text
    assert len(fake_openai.responses.calls) == 1
    assert "chat.tool_outputs.missing" in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert_chat_trace_text_suppressed(chat_trace)
    assert chat_trace["response_status"] == "error"


def test_execute_function_call_returns_tool_output_even_when_brand_research_crashes(
    monkeypatch,
) -> None:
    """tool 本体の予期しない例外でも、Responses API には function_call_output を返せる。"""

    def _raise_unexpected_error(**kwargs) -> str:
        del kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "execute_brand_research_tool", _raise_unexpected_error)

    output = main.execute_function_call(
        name="research_sake_brand",
        arguments='{"brand_name":"新政","user_context":"好き"}',
        openai_client=_FakeOpenAI(),
        brand_research_agent_ref=None,
        trace_context={"conversation_id": "conv_existing"},
    )

    body = json.loads(output)
    assert body["status"] == "tool_execution_error"
    assert body["error_type"] == "RuntimeError"


def test_chat_executes_brand_research_function_call_then_streams_answer(caplog) -> None:
    """Agent が銘柄リサーチ tool を要求したら、tool output を返して会話を続ける。"""
    first_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_1")),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call_1",
                    name="research_sake_brand",
                    arguments="",
                ),
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=0,
                delta='{"brand_name":"新政 No.6","user_context":"普段飲む"}',
            ),
        ]
    )
    second_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_2")),
            SimpleNamespace(type="response.output_text.delta", delta="新政 No.6 がお好きなら"),
            SimpleNamespace(type="response.output_text.delta", delta="冬樹FFF が近い候補です"),
        ]
    )
    fake_openai = _FakeOpenAI(response_streams=[first_stream, second_stream])
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/chat",
            json={"message": "普段は新政 No.6 を飲みます", "conversation_id": "conv_existing"},
        )

    assert response.status_code == 200
    assert "event: status\r\ndata: 調べています" in response.text
    assert "event: delta\r\ndata: 新政 No.6についての情報を確認しますね。" in response.text
    assert response.text.count("新政 No.6についての情報を確認しますね。") == 1
    assert "event: delta\r\ndata: 新政 No.6 がお好きなら" in response.text
    assert "event: delta\r\ndata: 冬樹FFF が近い候補です" in response.text
    assert "event: done" in response.text
    assert len(fake_openai.responses.calls) == 2
    assert fake_openai.responses.calls[0] == {
        "conversation": "conv_existing",
        "input": "普段は新政 No.6 を飲みます",
        "extra_body": {
            "agent_reference": {
                "type": "agent_reference",
                "name": "sake-concierge",
                "version": "1",
            }
        },
        "stream": True,
        "tool_choice": "required",
    }
    second_call = fake_openai.responses.calls[1]
    assert "conversation" not in second_call
    assert second_call["previous_response_id"] == "resp_1"
    assert second_call["stream"] is True
    assert second_call["input"][0]["type"] == "function_call_output"
    assert second_call["input"][0]["call_id"] == "call_1"
    assert "research_agent_not_configured" in second_call["input"][0]["output"]
    assert "brand_research.trace.start" in caplog.text
    assert "brand_research.trace.agent_not_configured" in caplog.text
    assert '"conversation_id": "conv_existing"' in caplog.text
    assert '"response_id": "resp_1"' in caplog.text
    assert '"tool_call_id": "call_1"' in caplog.text
    assert '"brand_name": "新政 No.6"' in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert_chat_trace_text_suppressed(chat_trace)


def test_chat_retries_retryable_error_while_reading_continuation_stream(
    monkeypatch,
    caplog,
) -> None:
    """tool output 後の継続 stream で 429 が出ても、短く再試行する。"""
    monkeypatch.setattr(main, "DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS", (0.0,))
    first_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_1")),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call_1",
                    name="research_sake_brand",
                    arguments='{"brand_name":"新政","user_context":"好き"}',
                ),
            ),
        ]
    )
    failing_stream = _FailingStream(AzureError("Too Many Requests"))
    recovered_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_2")),
            SimpleNamespace(type="response.output_text.delta", delta="冬樹FFF が近い候補です"),
        ]
    )
    fake_openai = _FakeOpenAI(response_streams=[first_stream, failing_stream, recovered_stream])
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/chat",
            json={"message": "新政が好きです", "conversation_id": "conv_existing"},
        )

    assert response.status_code == 200
    assert "event: delta\r\ndata: 冬樹FFF が近い候補です" in response.text
    assert "event: done" in response.text
    assert len(fake_openai.responses.calls) == 3
    assert fake_openai.responses.calls[1]["previous_response_id"] == "resp_1"
    assert fake_openai.responses.calls[2]["previous_response_id"] == "resp_1"
    assert failing_stream.closed is True
    assert "chat.stream.read.retry conversation_id=conv_existing" in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert_chat_trace_text_suppressed(chat_trace)
    assert chat_trace["response_status"] == "success"


def test_chat_falls_back_when_brand_research_continuation_stays_throttled(
    monkeypatch,
    caplog,
) -> None:
    """調査後の回答生成が 429 継続なら、店舗データから最低限の候補を返す。"""
    monkeypatch.setattr(main, "DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(
        main,
        "execute_function_call",
        lambda **_: json.dumps(
            {
                "status": "ok",
                "brand_name": "新政",
                "summary": "華やかな香りと爽やかな酸を比較軸にする。",
            },
            ensure_ascii=False,
        ),
    )
    first_stream = _FakeStream(
        events=[
            SimpleNamespace(type="response.created", response=SimpleNamespace(id="resp_1")),
            SimpleNamespace(type="response.output_text.delta", delta="新政について確認します。"),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call_1",
                    name="research_sake_brand",
                    arguments='{"brand_name":"新政","user_context":"好き"}',
                ),
            ),
        ]
    )
    failing_stream = _FailingStream(AzureError("Too Many Requests"))
    failing_retry_stream = _FailingStream(AzureError("Too Many Requests"))
    fake_openai = _FakeOpenAI(
        response_streams=[first_stream, failing_stream, failing_retry_stream]
    )
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/chat",
            json={
                "message": "新政が好きです。この店舗で近いものを1本だけ提案してください。",
                "conversation_id": "conv_existing",
                "store_id": "fukunotomo",
            },
        )

    assert response.status_code == 200
    assert "純米吟醸原酒 冬樹FFF" in response.text
    assert "event: error" not in response.text
    assert "event: done" in response.text
    assert len(fake_openai.responses.calls) == 3
    assert failing_stream.closed is True
    assert "chat.stream.read.fallback conversation_id=conv_existing" in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert_chat_trace_text_suppressed(chat_trace)
    assert chat_trace["response_status"] == "fallback"


def test_create_agent_response_stream_retries_transient_429(monkeypatch) -> None:
    """プレビュー中の一時的な upstream 429 は短くリトライする。"""
    monkeypatch.setattr(main, "DEFAULT_RESPONSE_CREATE_RETRY_DELAYS_SECONDS", (0.0,))
    fake_openai = _FakeOpenAI(
        response_errors=[AzureError("Too Many Requests"), None],
    )

    stream = main.create_agent_response_stream(
        openai_client=fake_openai,
        agent_ref=_FakeAgentRef(),
        conversation_id="conv_existing",
        input_data="冬樹FFFってどんなお酒？",
        previous_response_id=None,
        force_tool_choice=False,
    )

    assert isinstance(stream, _FakeStream)
    assert len(fake_openai.responses.calls) == 2
    assert fake_openai.responses.calls[0] == fake_openai.responses.calls[1]


def test_select_recommendation_product_ids_uses_precise_backend_matches() -> None:
    """共有シリーズ名ではなく、本文に出た明確な銘柄だけカード化する。"""
    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content="純米吟醸原酒 冬樹FFF が近い候補です。",
    ) == ["ftm-fuyuki-fff-genshu"]

    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content="冬樹シリーズが合いそうです。",
    ) == []

    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content="純米吟醸原酒 冬樹FFF ではなく、純米吟醸 神宮寺 を確認しましょう。",
    ) == ["ftm-jinguuji"]

    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content=(
            "1. Fukunotomo DE Fukunotomo 純米大吟醸\n"
            "2. 純米吟醸原酒 冬樹FFF\n"
            "3. 本醸造生原酒 杉玉ラベル"
        ),
    ) == [
        "ftm-fukunotomo-de-fukunotomo-jdg",
        "ftm-fuyuki-fff-genshu",
        "ftm-sugitama-honjozo-nama",
    ]

    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content=(
            "1. 秋田犬ラベル 純米吟醸生酒 F901\n"
            "2. 純米吟醸生原酒 冬樹FFF\n"
            "3. Fukunotomo DE Fukunotomo 純米大吟醸\n"
            "4. 純米吟醸原酒 冬樹"
        ),
    ) == [
        "ftm-akita-inu-f901-nama",
        "ftm-fuyuki-fff-nama-genshu",
        "ftm-fukunotomo-de-fukunotomo-jdg",
        "ftm-fuyuki-genshu",
    ]

    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content=(
            "1. 純米吟醸原酒 冬樹FFF\n"
            "   - 味わい: 香りのある旨口\n"
            "2. 純米吟醸生原酒 冬樹FFF\n"
            "   - 味わい: 香りのある旨口\n"
            "3. 純米吟醸生原酒 冬樹\n"
            "   - 味わい: 華やか・芳醇な米旨み"
        ),
    ) == [
        "ftm-fuyuki-fff-genshu",
        "ftm-fuyuki-fff-nama-genshu",
        "ftm-fuyuki-nama-genshu",
    ]

    assert main.select_recommendation_product_ids(
        store_id="fukunotomo",
        content=(
            "1. 60純米酒\n"
            "2. Fukunotomo DE Fukunotomo 純米大吟醸\n"
            "3. マル秘純米吟醸\n"
            "4. 大吟醸 福\n"
            "5. 本醸造原酒 杉玉ラベル\n"
            "6. 炭酸割り専用純米酒 サワードッグ\n"
            "7. 秋田犬ラベル 純米吟醸生酒 F901\n"
            "8. 純米吟醸 神宮寺\n"
            "9. 純米吟醸原酒 ヤママタクロラベル\n"
            "10. 純米吟醸生にごり酒 春うさぎ\n"
            "11. 馬から 辛口酒"
        ),
    ) == [
        "ftm-60-junmai",
        "ftm-fukunotomo-de-fukunotomo-jdg",
        "ftm-maruhi-junmai-ginjo",
        "ftm-daiginjo-fuku",
        "ftm-sugitama-honjozo-genshu",
        "ftm-sourdog",
        "ftm-akita-inu-f901-nama",
        "ftm-jinguuji",
        "ftm-yamamata-kuro",
        "ftm-haru-usagi",
    ]


def test_store_profile_exposes_public_card_copy_and_actions() -> None:
    """カードと次アクションに内部メモっぽい文言を出さない。"""
    profile = main.load_store_profile("fukunotomo")

    assert "英語で説明" not in profile["next_actions"]["ja"]
    assert "在庫がなかったので近い候補を探したい" in profile["next_actions"]["ja"]

    summaries = "\n".join(product["summary"] for product in profile["products"])
    assert "提案に使う" not in summaries
    assert "提案しやすい" not in summaries
    assert "候補化" not in summaries


def test_to_user_facing_stream_error_summarizes_rate_limit() -> None:
    """そのまま見せづらい 429 は、デモ利用者向けの日本語に変換する。"""
    message = main.to_user_facing_stream_error(AzureError("Too Many Requests"))

    assert "酒あわせAIが混み合っています" in message
    assert "もう一度相談してください" in message


def test_to_user_facing_stream_error_hides_unknown_exception_detail() -> None:
    """予期しない例外の内部文字列は、チャット利用者に返さない。"""
    message = main.to_user_facing_stream_error(RuntimeError("secret upstream detail"))

    assert "応答の取得中にエラーが発生しました" in message
    assert "secret upstream detail" not in message


def test_chat_forces_tool_choice_by_default_for_external_brand_preference() -> None:
    """一般銘柄の好み相談は、既定で外部銘柄リサーチを促す。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post(
        "/chat",
        json={"message": "亜麻猫が好きです。この店舗で近いものはありますか？"},
    )

    assert response.status_code == 200
    assert "event: delta\r\ndata: 亜麻猫についての情報を確認しますね。" in response.text
    assert fake_openai.responses.calls[0]["tool_choice"] == "required"


def test_chat_can_nudge_tool_choice_with_intent_mode(monkeypatch) -> None:
    """プレビュー保険を明示した場合だけ、一般的な銘柄相談形で tool 使用を促す。"""
    monkeypatch.setenv("BRAND_RESEARCH_TOOL_CHOICE_MODE", "intent")
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post(
        "/chat",
        json={"message": "飛鸞が好きです。この店舗で近いものはありますか？"},
    )

    assert response.status_code == 200
    assert "event: delta\r\ndata: 飛鸞についての情報を確認しますね。" in response.text
    assert fake_openai.responses.calls[0]["tool_choice"] == "required"


def test_chat_does_not_force_tool_choice_for_store_brand() -> None:
    """店舗の既知銘柄相談では、注入データを優先して A2A を強制しない。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post("/chat", json={"message": "冬樹FFFってどんなお酒？"})

    assert response.status_code == 200
    assert "event: status" not in response.text
    assert "情報を確認しますね" not in response.text
    assert "tool_choice" not in fake_openai.responses.calls[0]


def test_chat_does_not_force_tool_choice_for_generic_taste() -> None:
    """甘口・辛口など味の一般相談だけなら外部銘柄リサーチへ寄せない。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    response = client.post("/chat", json={"message": "甘口が好きです。おすすめは？"})

    assert response.status_code == 200
    assert "event: status" not in response.text
    assert "情報を確認しますね" not in response.text
    assert "tool_choice" not in fake_openai.responses.calls[0]


def test_chat_reuses_conversation_id(caplog) -> None:
    """継続会話では、会話作成をせず既存 conversation_id を使う。"""
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger="sake_concierge"):
        response = client.post(
            "/chat",
            json={"message": "続けて", "conversation_id": "conv_existing"},
        )

    assert response.status_code == 200
    assert 'event: meta\r\ndata: {"conversation_id": "conv_existing"}' in response.text
    assert fake_openai.conversations.create_calls == 0
    assert fake_openai.responses.calls[0]["conversation"] == "conv_existing"
    assert "chat.conversation.reuse conversation_id=conv_existing" in caplog.text
    assert "chat.conversation.create.start" not in caplog.text
    chat_trace = extract_structured_log(caplog.text, "chat_trace")
    assert chat_trace["conversation_id"] == "conv_existing"
    assert_chat_trace_text_suppressed(chat_trace)


def test_chat_rate_limit_blocks_sixth_request_from_same_ip(monkeypatch) -> None:
    """公開デモのモデル呼び出しは、同一 IP から 1 分 5 回までに絞る。"""
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "5")
    monkeypatch.setenv("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60")
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)
    headers = {"x-forwarded-for": "203.0.113.10"}

    for index in range(5):
        response = client.post(
            "/chat",
            json={"message": f"{index}回目", "conversation_id": "conv_existing"},
            headers=headers,
        )
        assert response.status_code == 200

    response = client.post(
        "/chat",
        json={"message": "6回目", "conversation_id": "conv_existing"},
        headers=headers,
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] != ""
    assert "短時間にアクセスが集中" in response.json()["detail"]
    assert len(fake_openai.responses.calls) == 5


def test_chat_conversation_rate_limit_blocks_third_prefetch(monkeypatch) -> None:
    """conversation 事前作成も Foundry 側のリソース作成なので rate limit する。"""
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60")
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()

    client = TestClient(main.app)

    assert client.post("/chat/conversation").status_code == 200
    assert client.post("/chat/conversation").status_code == 200
    response = client.post("/chat/conversation")

    assert response.status_code == 429
    assert fake_openai.conversations.create_calls == 2


def test_event_rate_limit_blocks_excess_analytics_posts(monkeypatch) -> None:
    """KPI API は本文なしでも、ログ課金や指標汚染を避けるため rate limit する。"""
    monkeypatch.setenv("EVENT_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("EVENT_RATE_LIMIT_WINDOW_SECONDS", "60")
    client = TestClient(main.app)
    payload = {
        "event_type": "product_link_clicked",
        "store_id": "fukunotomo",
        "product_id": "ftm-fuyuki-fff-genshu",
    }

    assert client.post("/api/analytics/events", json=payload).status_code == 200
    assert client.post("/api/analytics/events", json=payload).status_code == 200
    response = client.post("/api/analytics/events", json=payload)

    assert response.status_code == 429


def test_forwarded_headers_are_ignored_unless_trusted(monkeypatch) -> None:
    """攻撃者が x-forwarded-for を変えても、既定では rate limit key を変えない。"""
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60")
    fake_openai = _FakeOpenAI()
    main._state["openai"] = fake_openai
    main._state["agent_ref"] = _FakeAgentRef()
    client = TestClient(main.app)

    for index in range(2):
        response = client.post(
            "/chat",
            json={"message": f"{index}回目", "conversation_id": "conv_existing"},
            headers={"x-forwarded-for": f"203.0.113.{index}"},
        )
        assert response.status_code == 200

    response = client.post(
        "/chat",
        json={"message": "3回目", "conversation_id": "conv_existing"},
        headers={"x-forwarded-for": "203.0.113.99"},
    )

    assert response.status_code == 429




