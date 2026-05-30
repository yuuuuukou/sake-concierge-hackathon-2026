from pathlib import Path

import pytest
from azure.core.exceptions import ResourceNotFoundError

from src.api.store_data import (
    StoreDataConfigurationError,
    StoreDataDownloadError,
    build_blob_name,
    get_blob_account_url,
    get_store_data_root,
    prepare_store_data,
    validate_required_file_name,
    validate_store_id,
)


class _FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def readall(self) -> bytes:
        return self.payload


class _FakeBlobClient:
    def __init__(self, payload: bytes | Exception) -> None:
        self.payload = payload

    def download_blob(self, *, max_concurrency: int) -> _FakeDownloader:
        assert max_concurrency == 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return _FakeDownloader(self.payload)


class _FakeBlobServiceClient:
    def __init__(self, blobs: dict[str, bytes | Exception]) -> None:
        self.blobs = blobs
        self.requests: list[tuple[str, str]] = []

    def get_blob_client(self, *, container: str, blob: str) -> _FakeBlobClient:
        self.requests.append((container, blob))
        return _FakeBlobClient(self.blobs[blob])


def test_prepare_store_data_downloads_required_files_from_blob(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """blob source は必要ファイルを cache root へ取得する。"""
    blobs = {
        "stores/fukunotomo/catalog_master.csv": b"master",
        "stores/fukunotomo/sku_simplified.csv": b"sku",
    }
    fake_client = _FakeBlobServiceClient(blobs)
    monkeypatch.setenv("STORE_DATA_SOURCE", "blob")
    monkeypatch.setenv("STORE_DATA_CACHE_ROOT", str(tmp_path))
    monkeypatch.setenv("STORE_DATA_BLOB_ACCOUNT_URL", "https://example.blob.core.windows.net")
    monkeypatch.setenv("STORE_DATA_BLOB_CONTAINER", "private-store-data")
    monkeypatch.setenv("STORE_DATA_STORE_IDS", "fukunotomo")
    monkeypatch.setenv("STORE_DATA_REQUIRED_FILES", "catalog_master.csv,sku_simplified.csv")

    root = prepare_store_data(blob_service_factory=lambda _: fake_client)

    assert root == tmp_path
    assert (tmp_path / "fukunotomo" / "catalog_master.csv").read_bytes() == b"master"
    assert (tmp_path / "fukunotomo" / "sku_simplified.csv").read_bytes() == b"sku"
    assert fake_client.requests == [
        ("private-store-data", "stores/fukunotomo/catalog_master.csv"),
        ("private-store-data", "stores/fukunotomo/sku_simplified.csv"),
    ]


def test_prepare_store_data_raises_when_required_blob_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """必要 Blob が欠けている場合は起動失敗へ寄せる。"""
    fake_client = _FakeBlobServiceClient(
        {"stores/fukunotomo/catalog_master.csv": ResourceNotFoundError("missing")}
    )
    monkeypatch.setenv("STORE_DATA_SOURCE", "blob")
    monkeypatch.setenv("STORE_DATA_CACHE_ROOT", str(tmp_path))
    monkeypatch.setenv("STORE_DATA_BLOB_ACCOUNT_NAME", "examplestore")
    monkeypatch.setenv("STORE_DATA_STORE_IDS", "fukunotomo")
    monkeypatch.setenv("STORE_DATA_REQUIRED_FILES", "catalog_master.csv")

    with pytest.raises(StoreDataDownloadError, match="catalog_master.csv"):
        prepare_store_data(blob_service_factory=lambda _: fake_client)


def test_local_source_uses_store_data_root(monkeypatch, tmp_path: Path) -> None:
    """local source は Blob 設定を要求せず、明示 root を返す。"""
    monkeypatch.setenv("STORE_DATA_SOURCE", "local")
    monkeypatch.setenv("STORE_DATA_ROOT", str(tmp_path))

    assert get_store_data_root() == tmp_path
    assert prepare_store_data() == tmp_path


def test_blob_account_url_can_be_built_from_account_name(monkeypatch) -> None:
    """account URL はURL指定か account name から作れる。"""
    monkeypatch.delenv("STORE_DATA_BLOB_ACCOUNT_URL", raising=False)
    monkeypatch.setenv("STORE_DATA_BLOB_ACCOUNT_NAME", "sakeexample")

    assert get_blob_account_url() == "https://sakeexample.blob.core.windows.net"


def test_store_id_and_file_name_reject_path_traversal() -> None:
    """Blob path / cache path に store_id や file name の traversal を混ぜない。"""
    with pytest.raises(StoreDataConfigurationError):
        validate_store_id("../fukunotomo")
    with pytest.raises(StoreDataConfigurationError):
        validate_required_file_name("../catalog_master.csv")


def test_build_blob_name_omits_empty_prefix() -> None:
    """prefix なしでも store/file の Blob path を安定して作る。"""
    assert build_blob_name("", "fukunotomo", "catalog_master.csv") == (
        "fukunotomo/catalog_master.csv"
    )
