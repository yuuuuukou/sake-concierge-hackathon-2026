from pathlib import Path

import pytest

from src.api.retriever import StuffingRetriever


def test_stuffing_retriever_reads_supported_store_files(tmp_path: Path) -> None:
    """対応拡張子の店舗ファイルだけを、安定した順序で context に詰める。"""
    store_dir = tmp_path / "default"
    store_dir.mkdir()
    (store_dir / "catalog.md").write_text("# Catalog\n\n雪の茅舎", encoding="utf-8")
    (store_dir / "brewery.txt").write_text("刈穂酒造", encoding="utf-8")
    (store_dir / "ignored.csv").write_text("not included", encoding="utf-8")

    result = StuffingRetriever(data_root=tmp_path).retrieve(query="辛口", store_id="default")

    assert "雪の茅舎" in result.context
    assert "刈穂酒造" in result.context
    assert "not included" not in result.context
    assert [source.path for source in result.sources] == [
        "default/brewery.txt",
        "default/catalog.md",
    ]
    assert [source.title for source in result.sources] == ["brewery", "Catalog"]
    assert result.token_estimate is not None
    assert result.latency_ms is not None


def test_stuffing_retriever_requires_existing_store(tmp_path: Path) -> None:
    """存在しない店舗 ID を、空 context として扱わない。"""
    with pytest.raises(FileNotFoundError):
        StuffingRetriever(data_root=tmp_path).retrieve(query="", store_id="missing")


def test_stuffing_retriever_requires_store_path_to_be_directory(tmp_path: Path) -> None:
    """店舗 ID のパスがファイルだった場合は、読み込み対象にしない。"""
    (tmp_path / "default").write_text("not a directory", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        StuffingRetriever(data_root=tmp_path).retrieve(query="", store_id="default")


def test_stuffing_retriever_skips_empty_supported_files(tmp_path: Path) -> None:
    """空の対応ファイルは、Agent に詰める context へ混ぜない。"""
    store_dir = tmp_path / "default"
    store_dir.mkdir()
    (store_dir / "empty.md").write_text("", encoding="utf-8")
    (store_dir / "catalog.md").write_text("一白水成", encoding="utf-8")

    result = StuffingRetriever(data_root=tmp_path).retrieve(query="", store_id="default")

    assert "empty.md" not in result.context
    assert [source.path for source in result.sources] == ["default/catalog.md"]
    assert [source.title for source in result.sources] == ["catalog"]


def test_stuffing_retriever_requires_supported_files(tmp_path: Path) -> None:
    """対応ファイルがない店舗データでは、Agent 作成へ進めない。"""
    store_dir = tmp_path / "default"
    store_dir.mkdir()
    (store_dir / "catalog.csv").write_text("unsupported", encoding="utf-8")

    with pytest.raises(ValueError):
        StuffingRetriever(data_root=tmp_path).retrieve(query="", store_id="default")
