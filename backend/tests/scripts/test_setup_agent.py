from pathlib import Path
from types import SimpleNamespace

import scripts.setup_agent as setup_agent
from scripts.setup_agent import (
    build_a2a_brand_research_tool,
    build_agent_definition,
    build_agent_tools,
    create_agent_version,
    create_project_client,
    load_store_context,
    parse_args,
    print_agent_env,
    print_store_context_summary,
)
from src.api.retriever import RetrievalResult, SourceRef


class _FakeRetriever:
    """実データを読まずに Retriever 連携だけを検証するための代替 Retriever。"""

    def __init__(self) -> None:
        """呼び出し履歴を assert で確認できるようにする。"""
        self.calls: list[dict] = []

    def retrieve(
        self,
        query: str,
        store_id: str,
        *,
        locale: str | None = None,
    ) -> RetrievalResult:
        """実データを読まず、固定の RetrievalResult を返す。"""
        self.calls.append({"query": query, "store_id": store_id, "locale": locale})
        return RetrievalResult(
            context="偽の店舗データ context",
            sources=[SourceRef(store_id=store_id, path=f"{store_id}/catalog.md")],
            token_estimate=42,
            latency_ms=1,
        )


class _FakeAgents:
    """Foundry SDK の agents API を呼ばずに Agent 作成連携だけを検証する代替。"""

    def __init__(self) -> None:
        """create_version の呼び出し履歴を残す。"""
        self.calls: list[dict] = []

    def create_version(self, **kwargs):
        """Foundry には出さず、作成済み Agent 風の値を返す。"""
        self.calls.append(kwargs)
        return SimpleNamespace(id="agent_123", name=kwargs["agent_name"], version="7")


class _FakeConnections:
    """Foundry project connection 取得を外部通信なしで検証する代替。"""

    def __init__(self) -> None:
        """get の呼び出し履歴を残す。"""
        self.calls: list[str] = []

    def get(self, connection_name: str):
        """A2A connection 風の id を返す。"""
        self.calls.append(connection_name)
        return SimpleNamespace(id=f"/connections/{connection_name}")


class _FakeProject:
    """setup_agent が使う project.agents だけを持つ最小の代替 Project。"""

    def __init__(self) -> None:
        """setup_agent が参照する agents だけを持たせる。"""
        self.agents = _FakeAgents()
        self.connections = _FakeConnections()


def test_parse_args_accepts_data_dir(monkeypatch, tmp_path: Path) -> None:
    """実機確認で、店舗データの読み込み先を CLI から差し替える。"""
    data_dir = tmp_path / "custom-store"
    monkeypatch.setattr(
        setup_agent.sys,
        "argv",
        ["setup_agent.py", "--data-dir", str(data_dir)],
    )

    args = parse_args()

    assert args.data_dir == data_dir


def test_create_project_client_returns_none_without_endpoint(monkeypatch, capsys) -> None:
    """endpoint 未設定時は SDK 認証へ進まず、実行前に止める。"""
    monkeypatch.delenv("AZURE_AIPROJECT_ENDPOINT", raising=False)

    project = create_project_client()

    output = capsys.readouterr().out
    assert project is None
    assert "AZURE_AIPROJECT_ENDPOINT" in output
    assert ".env" in output


def test_load_store_context_uses_data_dir_name_as_store_id(tmp_path: Path) -> None:
    """data_dir の末尾名を store_id として Retriever に渡す。"""
    data_dir = tmp_path / "unit-store"
    fake_retriever = _FakeRetriever()

    result = load_store_context(data_dir, retriever=fake_retriever)

    assert result.context == "偽の店舗データ context"
    assert result.sources[0].path == "unit-store/catalog.md"
    assert fake_retriever.calls == [{"query": "", "store_id": "unit-store", "locale": None}]


def test_build_agent_definition_wraps_context_in_instructions() -> None:
    """FileSearch なしで、店舗 context を instructions に詰める。"""
    definition = build_agent_definition(
        context="# テスト用カタログ\n\n一白水成",
        model="gpt-5-nano",
    )

    assert definition.model == "gpt-5-nano"
    assert definition.instructions is not None
    assert "取扱い酒リスト・蔵情報" in definition.instructions
    assert "生成AIによる参考情報" in definition.instructions
    assert "20歳未満の飲酒" in definition.instructions
    assert "公式見解として断定せず" in definition.instructions
    assert "取扱い酒リストの各 `## 商品名` 見出しを一字一句そのまま使う" in definition.instructions
    assert "略称・通称・補足表現" in definition.instructions
    assert "一白水成" in definition.instructions
    assert "File Search" not in definition.instructions
    assert "VectorStore" not in definition.instructions
    assert definition.tools is None


def test_build_agent_tools_adds_brand_research_function_by_default() -> None:
    """既定では BFF fallback 用の銘柄リサーチ function tool を登録する。"""
    fake_project = _FakeProject()
    args = SimpleNamespace(
        disable_brand_research_function=False,
        a2a_connection_name=None,
        a2a_base_url=None,
    )

    tools = build_agent_tools(fake_project, args)

    assert len(tools) == 1
    assert tools[0].name == "research_sake_brand"
    assert tools[0].type == "function"
    assert tools[0].strict is True
    assert tools[0].parameters["required"] == ["brand_name", "user_context"]


def test_build_agent_tools_adds_a2a_preview_tool_when_connection_is_set() -> None:
    """A2A connection が指定された場合だけ preview tool も登録する。"""
    fake_project = _FakeProject()
    args = SimpleNamespace(
        disable_brand_research_function=False,
        a2a_connection_name="brand-research-a2a",
        a2a_base_url="https://example.test/a2a",
    )

    tools = build_agent_tools(fake_project, args)

    assert [tool.type for tool in tools] == ["a2a_preview", "function"]
    assert tools[0].project_connection_id == "/connections/brand-research-a2a"
    assert tools[0].base_url == "https://example.test/a2a"
    assert fake_project.connections.calls == ["brand-research-a2a"]


def test_build_a2a_brand_research_tool_returns_none_without_connection() -> None:
    """A2A は preview なので、connection 未指定なら既存動作に影響させない。"""
    fake_project = _FakeProject()

    tool = build_a2a_brand_research_tool(fake_project, connection_name=None)

    assert tool is None
    assert fake_project.connections.calls == []


def test_create_agent_version_delegates_to_project_agents() -> None:
    """Agent 名と definition を Foundry SDK ラッパーへそのまま渡す。"""
    fake_project = _FakeProject()
    definition = build_agent_definition(context="刈穂", model="gpt-5-nano")

    agent = create_agent_version(
        fake_project,
        agent_name="sake-concierge-test",
        definition=definition,
    )

    assert agent.id == "agent_123"
    assert agent.name == "sake-concierge-test"
    assert agent.version == "7"
    assert fake_project.agents.calls == [
        {"agent_name": "sake-concierge-test", "definition": definition}
    ]


def test_print_store_context_summary_outputs_loaded_sources(capsys) -> None:
    """実機実行時に、読み込まれた店舗データの手掛かりを表示する。"""
    result = RetrievalResult(
        context="テスト context",
        sources=[SourceRef(store_id="default", path="default/catalog.md")],
        token_estimate=42,
        latency_ms=3,
    )

    print_store_context_summary(result)

    output = capsys.readouterr().out
    assert "default/catalog.md" in output
    assert "1 files" in output
    assert "42 tokens" in output
    assert "3 ms" in output


def test_print_agent_env_outputs_values_for_dotenv(capsys) -> None:
    """.env に転記すべき Agent 名と version を表示する。"""
    agent = SimpleNamespace(name="sake-concierge-test", version="7")

    print_agent_env(agent)

    output = capsys.readouterr().out
    assert "AZURE_AGENT_NAME=sake-concierge-test" in output
    assert "AZURE_AGENT_VERSION=7" in output
