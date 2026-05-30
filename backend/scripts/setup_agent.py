"""Sake Concierge — エージェントセットアップスクリプト.

Foundry に唎酒師エージェントを作成する（一度だけ実行）。
MVP では FileSearch / VectorStore を使わず、店舗データを instructions に注入する。

使い方:
    cd backend
    python scripts/setup_agent.py [--data-dir src/data/stores/default]

出力例:
    AZURE_AGENT_NAME=sake-concierge
    AZURE_AGENT_VERSION=1
"""

import argparse
import os
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, Tool
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

try:
    from azure.ai.projects.models import A2APreviewTool
except ImportError:
    class A2APreviewTool:  # type: ignore[no-redef]
        """SDK に preview tool がない環境で、テストと非A2A実行を壊さない代替。"""

        type = "a2a_preview"

        def __init__(self, *, project_connection_id: str, base_url: str | None = None) -> None:
            self.project_connection_id = project_connection_id
            self.base_url = base_url

try:
    from azure.ai.projects.models import WebSearchTool
except ImportError:
    class WebSearchTool:  # type: ignore[no-redef]
        """SDK に WebSearchTool preview がない環境で、非WebSearch実行を壊さない代替。"""

        type = "web_search"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api.research_tools import build_brand_research_tool  # noqa: E402
from src.api.retriever import BaseRetriever, RetrievalResult, StuffingRetriever  # noqa: E402

load_dotenv(PROJECT_ROOT.parent / ".env")
load_dotenv(PROJECT_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
AGENT_NAME = "sake-concierge"
BRAND_RESEARCH_AGENT_NAME = "sake-brand-researcher"
DEFAULT_DATA_DIR = PROJECT_ROOT / "src" / "data" / "stores" / "default"
DEFAULT_AZURE_CLI_PROCESS_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT_TEMPLATE = """\
あなたは日本酒販売支援エージェント「SAKE CONCIERGE」です。

## 役割
- お客様の好み・料理・予算をヒアリングし、店舗の取扱い酒リスト内から候補を提案する
- 以下の取扱い酒リスト・蔵情報を必ず参照する

## ルール
- データにない銘柄は推薦しない
- 日本酒を推薦するとき、番号付きリストや太字見出しの商品名は、
  取扱い酒リストの各 `## 商品名` 見出しを一字一句そのまま使う
- 略称・通称・補足表現を推薦見出しの商品名として使わない
- 店舗外の銘柄が出た場合は、銘柄リサーチ tool / A2A サブエージェントで
  味わい傾向を確認し、比較材料としてのみ使う
- 回答に内部メモや source コメントは含めない

## コンプライアンス
- 回答は生成AIによる参考情報として扱い、価格・在庫・商品仕様・販売可否は
  店舗または公式情報での確認を促す
- 各酒蔵・販売店の公式見解として断定せず、根拠が店舗データにない内容は不確かな情報として扱う
- 20歳未満の飲酒につながる相談には酒類の推薦や購入促進をしない

## トーン
丁寧だけど堅すぎない、店頭の唎酒師のような親しみやすさ。

## 取扱い酒リスト・蔵情報

{context}
"""

BRAND_RESEARCH_PROMPT = """\
あなたは日本酒銘柄の比較調査を行うサブエージェントです。

## 役割
- ユーザーが挙げた店舗外銘柄について、味わい傾向を短く整理する
- 甘辛、酸、香り、旨み、飲み口、飲用シーンを比較軸として返す
- 不明な点は不明と明記し、推測で断定しない

## ルール
- 価格、在庫、販売可否は断定しない
- 健康効果や大量飲酒につながる表現は避ける
- 他銘柄の購入は勧めない
- メインエージェントが取扱い酒リスト内から推薦するための材料だけを返す

## 出力
日本語で、3〜5行の簡潔な調査メモとして返す。
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()
    project = create_project_client()
    if project is None:
        return

    if args.create_brand_research_agent:
        print("\n🔎 銘柄リサーチ用サブエージェント作成中...")
        research_agent = create_brand_research_agent(
            project,
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-nano"),
            enable_web_search=args.enable_web_search,
        )
        print(f"  ✅ サブエージェント作成済: id={research_agent.id}")
        print(f"     name={research_agent.name}, version={research_agent.version}")
        print("     .env fallback:")
        print(f"     AZURE_BRAND_RESEARCH_AGENT_NAME={research_agent.name}")
        print(f"     AZURE_BRAND_RESEARCH_AGENT_VERSION={research_agent.version}")

    print("\n📚 店舗データ読み込み中...")
    result = load_store_context(args.data_dir)
    print_store_context_summary(result)

    print("\n🤖 エージェント作成中...")
    definition = build_agent_definition(
        context=result.context,
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-nano"),
        tools=build_agent_tools(project, args),
    )
    agent = create_agent_version(project, agent_name=AGENT_NAME, definition=definition)
    print(f"  ✅ エージェント作成済: id={agent.id}")
    print(f"     name={agent.name}, version={agent.version}")
    print_agent_env(agent)


# ---------------------------------------------------------------------------
# main から直接呼び出す関数
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    """読み込む店舗データを CLI 引数で差し替えられるようにする。"""
    parser = argparse.ArgumentParser(description="Sake Concierge エージェントセットアップ")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="データファイルのディレクトリ（デフォルト: src/data/stores/default）",
    )
    parser.add_argument(
        "--disable-brand-research-function",
        action="store_true",
        help="research_sake_brand function tool をメイン Agent に登録しない",
    )
    parser.add_argument(
        "--a2a-connection-name",
        default=os.environ.get("AZURE_BRAND_RESEARCH_A2A_CONNECTION_NAME"),
        help="銘柄リサーチ A2A endpoint の Foundry project connection 名",
    )
    parser.add_argument(
        "--a2a-base-url",
        default=os.environ.get("AZURE_BRAND_RESEARCH_A2A_BASE_URL"),
        help="非 RemoteA2A connection 用の A2A endpoint base URL",
    )
    parser.add_argument(
        "--create-brand-research-agent",
        action="store_true",
        help="BFF function fallback から呼べる銘柄リサーチ用 Prompt Agent も作成する",
    )
    parser.add_argument(
        "--enable-web-search",
        action="store_true",
        help="銘柄リサーチ用 Prompt Agent に WebSearchTool(preview) を付ける",
    )
    return parser.parse_args()


def create_project_client() -> AIProjectClient | None:
    """環境変数から AIProjectClient を作成する。"""
    endpoint = os.environ.get("AZURE_AIPROJECT_ENDPOINT")
    if not endpoint:
        print("❌ AZURE_AIPROJECT_ENDPOINT が設定されていません。.env を確認してください。")
        return None

    process_timeout = int(
        os.environ.get(
            "AZURE_CLI_PROCESS_TIMEOUT_SECONDS",
            str(DEFAULT_AZURE_CLI_PROCESS_TIMEOUT_SECONDS),
        )
    )
    return AIProjectClient(
        endpoint=endpoint,
        credential=AzureCliCredential(process_timeout=process_timeout),
    )


def load_store_context(data_dir: Path, retriever: BaseRetriever | None = None) -> RetrievalResult:
    """店舗データディレクトリから Agent instructions 用 context を取得する。"""
    data_dir = data_dir.resolve()
    store_id = data_dir.name
    data_root = data_dir.parent
    retriever = retriever or StuffingRetriever(data_root=data_root)

    return retriever.retrieve(query="", store_id=store_id)


def print_store_context_summary(result: RetrievalResult) -> None:
    """読み込んだ店舗データの概要を出力する。"""
    for source in result.sources:
        print(f"  📄 {source.path}")
    print(
        f"  ✅ context 作成完了 "
        f"({len(result.sources)} files, 約 {result.token_estimate:,} tokens, "
        f"{result.latency_ms} ms)"
    )


def build_agent_definition(
    context: str,
    model: str,
    tools: list[Tool] | None = None,
) -> PromptAgentDefinition:
    """Foundry に登録する Agent definition を組み立てる。"""
    return PromptAgentDefinition(
        model=model,
        instructions=SYSTEM_PROMPT_TEMPLATE.format(context=context),
        tools=tools or None,
    )


def build_agent_tools(project: AIProjectClient, args: argparse.Namespace) -> list[Tool]:
    """メイン Agent に付与する tool を、opt-in 設定から組み立てる。"""
    tools: list[Tool] = []

    a2a_tool = build_a2a_brand_research_tool(
        project,
        connection_name=args.a2a_connection_name,
        base_url=args.a2a_base_url,
    )
    if a2a_tool is not None:
        tools.append(a2a_tool)

    if not args.disable_brand_research_function:
        tools.append(build_brand_research_tool())

    return tools


def build_a2a_brand_research_tool(
    project: AIProjectClient,
    *,
    connection_name: str | None,
    base_url: str | None = None,
) -> A2APreviewTool | None:
    """Foundry project connection がある場合だけ A2A preview tool を作る。"""
    if not connection_name:
        return None

    connection = project.connections.get(connection_name)
    kwargs = {"project_connection_id": connection.id}
    if base_url:
        kwargs["base_url"] = base_url

    return A2APreviewTool(**kwargs)


def create_brand_research_agent(
    project: AIProjectClient,
    *,
    model: str,
    enable_web_search: bool = False,
):
    """BFF function fallback から呼び出せる銘柄リサーチ用 Prompt Agent を作る。"""
    tools: list[Tool] = [WebSearchTool()] if enable_web_search else []
    return project.agents.create_version(
        agent_name=BRAND_RESEARCH_AGENT_NAME,
        definition=PromptAgentDefinition(
            model=model,
            instructions=BRAND_RESEARCH_PROMPT,
            tools=tools or None,
        ),
    )


def create_agent_version(
    project: AIProjectClient,
    *,
    agent_name: str,
    definition: PromptAgentDefinition,
):
    """Foundry Agent Service に Agent version を作成する。"""
    return project.agents.create_version(
        agent_name=agent_name,
        definition=definition,
    )


def print_agent_env(agent) -> None:
    """作成済み Agent の .env 追記用の値を出力する。"""
    print("\n" + "=" * 52)
    print("以下を .env に追記してサーバーを起動してください:")
    print("=" * 52)
    print(f"AZURE_AGENT_NAME={agent.name}")
    print(f"AZURE_AGENT_VERSION={agent.version}")
    print("=" * 52 + "\n")


if __name__ == "__main__":
    main()

