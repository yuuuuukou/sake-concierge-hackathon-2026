"""Sake Concierge の Retriever 抽象化。

MVP では検索を行わず、店舗ディレクトリ配下の Markdown/TXT/JSON を全件連結して
Foundry Agent の instructions に注入する。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.api.store_data import get_store_data_root

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
SUPPORTED_EXTENSIONS = {".md", ".txt", ".json"}


# ---------------------------------------------------------------------------
# Retriever が返すデータ
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceRef:
    """実機確認時に、context へ詰めた元ファイルを追跡するための参照情報。"""

    store_id: str
    path: str
    title: str | None = None


@dataclass(frozen=True)
class RetrievalResult:
    """Agent instructions に詰める context と、読み込み結果のメタ情報。"""

    context: str
    sources: list[SourceRef]
    token_estimate: int | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Retriever の差し替え口
# ---------------------------------------------------------------------------
class BaseRetriever(Protocol):
    """将来の検索基盤へ移っても、呼び出し側を変えないための最小インタフェース。"""

    def retrieve(
        self,
        query: str,
        store_id: str,
        *,
        locale: str | None = None,
    ) -> RetrievalResult:
        """実装を差し替えても、同じ引数で店舗 context を取得できるようにする。"""


# ---------------------------------------------------------------------------
# MVP の Retriever 実装
# ---------------------------------------------------------------------------
class StuffingRetriever:
    """外部検索基盤なしで、店舗データを丸ごと context として返す MVP 実装。

    指定された店舗ディレクトリ配下の対応ファイルを全件読み込み、
    Markdown の水平線（---）で区切って1つの文字列に連結する。
    連結した文字列を Foundry Agent の instructions に直接注入することで、
    ベクトル検索などの外部基盤なしに RAG と同等の効果を得る（Context Stuffing 方式）。
    """

    def __init__(
        self,
        data_root: Path | str | None = None,
        supported_extensions: set[str] | None = None,
    ) -> None:
        """テストや店舗切替で、読み込みルートと対象拡張子を差し替えられるようにする。"""
        self.data_root = Path(data_root) if data_root is not None else get_store_data_root()
        self.supported_extensions = supported_extensions or SUPPORTED_EXTENSIONS

    def retrieve(
        self,
        query: str,
        store_id: str,
        *,
        locale: str | None = None,
    ) -> RetrievalResult:
        """指定店舗のファイルを連結し、Agent 作成時に詰める context を作る。"""
        # Stuffing 方式では未使用だが、将来 Retriever と同じシグネチャにそろえる。
        del query, locale

        # 計測開始
        started = time.perf_counter()
        store_dir = self.data_root / store_id

        # ディレクトリ検証
        if not store_dir.exists():
            raise FileNotFoundError(f"店舗データディレクトリが見つかりません: {store_dir}")
        if not store_dir.is_dir():
            raise NotADirectoryError(f"店舗データパスがディレクトリではありません: {store_dir}")

        sources: list[SourceRef] = []
        sections: list[str] = []
        for path in sorted(store_dir.rglob("*")):
            # 対象外ファイルをスキップ
            if not path.is_file() or path.suffix.lower() not in self.supported_extensions:
                continue
            content = path.read_text(encoding="utf-8-sig").strip()
            if not content:
                continue

            # context 作成
            relative_path = path.relative_to(self.data_root).as_posix()
            title = _extract_title(content) or path.stem
            sources.append(SourceRef(store_id=store_id, path=relative_path, title=title))
            sections.append(f"<!-- source: {relative_path} -->\n\n{content}")

        if not sections:
            raise ValueError(
                f"店舗データが見つかりません: {store_dir} "
                f"(対応拡張子: {', '.join(sorted(self.supported_extensions))})"
            )

        # 結合 & 返却
        context = "\n\n---\n\n".join(sections)
        latency_ms = round((time.perf_counter() - started) * 1000)
        return RetrievalResult(
            context=context,
            sources=sources,
            token_estimate=_estimate_tokens(context),
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# StuffingRetriever の補助関数
# ---------------------------------------------------------------------------
def _extract_title(content: str) -> str | None:
    """Markdown 見出しを、実機確認用の source title として使う。"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
    return None


def _estimate_tokens(text: str) -> int:
    """Exit Criteria の目安確認用に、ざっくり token 数を見積もる。"""
    # 旧実装: 英語基準（3文字 ≒ 1トークン）のため日本語では過小評価になる。
    # return max(1, len(text) // 3)

    # 注入するデータは主に日本語。cl100k_base では概ね 1 文字 ≒ 2 トークンで概算する。
    return max(1, len(text) * 2)
