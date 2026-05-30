"""Store data source selection and Blob-backed startup download."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "data" / "stores"
DEFAULT_BLOB_CONTAINER = "store-data"
DEFAULT_BLOB_PREFIX = "stores"
DEFAULT_STORE_IDS = ("fukunotomo",)
DEFAULT_REQUIRED_FILES = (
    "catalog_master.csv",
    "sku_simplified.csv",
    "catalog.md",
    "brewery.md",
)
STORE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")
SAFE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class StoreDataConfigurationError(RuntimeError):
    """Raised when store data source settings are invalid."""


class StoreDataDownloadError(RuntimeError):
    """Raised when required store data cannot be downloaded."""


class BlobServiceFactory(Protocol):
    """Factory type used by tests to avoid touching Azure."""

    def __call__(self, account_url: str) -> object:
        """Return a BlobServiceClient-like object."""


def get_store_data_source() -> str:
    """Return the configured store data source."""
    raw = os.getenv("STORE_DATA_SOURCE", "local").strip().lower()
    if raw in {"local", "file"}:
        return "local"
    if raw == "blob":
        return "blob"
    raise StoreDataConfigurationError(
        "STORE_DATA_SOURCE は local または blob を指定してください"
    )


def get_store_data_root() -> Path:
    """Return the local directory that current code should read from."""
    if get_store_data_source() == "blob":
        return Path(
            os.getenv(
                "STORE_DATA_CACHE_ROOT",
                str(Path(tempfile.gettempdir()) / "sake-concierge-store-data"),
            )
        )
    return Path(os.getenv("STORE_DATA_ROOT", str(DEFAULT_DATA_ROOT)))


def get_store_data_dir(store_id: str) -> Path:
    """Return a validated local directory for a store."""
    validate_store_id(store_id)
    data_root = get_store_data_root().resolve()
    data_dir = (data_root / store_id).resolve()
    if data_root not in data_dir.parents or not data_dir.exists():
        raise FileNotFoundError(f"店舗データディレクトリが見つかりません: {store_id}")
    if not data_dir.is_dir():
        raise NotADirectoryError(f"店舗データパスがディレクトリではありません: {store_id}")
    return data_dir


def prepare_store_data(blob_service_factory: BlobServiceFactory | None = None) -> Path:
    """Download configured Blob store data before the app starts serving requests."""
    source = get_store_data_source()
    data_root = get_store_data_root()
    if source != "blob":
        return data_root

    store_ids = get_store_ids()
    required_files = get_required_files()
    account_url = get_blob_account_url()
    container_name = os.getenv("STORE_DATA_BLOB_CONTAINER", DEFAULT_BLOB_CONTAINER).strip()
    if not container_name:
        raise StoreDataConfigurationError("STORE_DATA_BLOB_CONTAINER が未設定です")

    blob_service_client = (
        blob_service_factory(account_url)
        if blob_service_factory
        else create_blob_service_client(account_url)
    )
    download_blob_store_data(
        blob_service_client=blob_service_client,
        container_name=container_name,
        prefix=os.getenv("STORE_DATA_BLOB_PREFIX", DEFAULT_BLOB_PREFIX),
        store_ids=store_ids,
        required_files=required_files,
        target_root=data_root,
    )
    return data_root


def create_blob_service_client(account_url: str) -> BlobServiceClient:
    """Create a BlobServiceClient using Entra ID / Managed Identity auth."""
    credential = DefaultAzureCredential(
        managed_identity_client_id=os.getenv("AZURE_CLIENT_ID") or None
    )
    return BlobServiceClient(account_url=account_url, credential=credential)


def download_blob_store_data(
    *,
    blob_service_client: object,
    container_name: str,
    prefix: str | None,
    store_ids: tuple[str, ...],
    required_files: tuple[str, ...],
    target_root: Path,
) -> None:
    """Download required files for each store into the local cache root."""
    clean_prefix = normalize_blob_prefix(prefix)
    for store_id in store_ids:
        validate_store_id(store_id)
        store_dir = (target_root / store_id).resolve()
        target_root_resolved = target_root.resolve()
        if target_root_resolved not in store_dir.parents:
            raise StoreDataConfigurationError("STORE_DATA_CACHE_ROOT の外へは保存できません")
        store_dir.mkdir(parents=True, exist_ok=True)

        for file_name in required_files:
            validate_required_file_name(file_name)
            blob_name = build_blob_name(clean_prefix, store_id, file_name)
            target_file = store_dir / file_name
            download_required_blob(
                blob_service_client=blob_service_client,
                container_name=container_name,
                blob_name=blob_name,
                target_file=target_file,
            )


def download_required_blob(
    *,
    blob_service_client: object,
    container_name: str,
    blob_name: str,
    target_file: Path,
) -> None:
    """Download one required blob without logging its contents."""
    try:
        blob_client = blob_service_client.get_blob_client(
            container=container_name,
            blob=blob_name,
        )
        data = blob_client.download_blob(max_concurrency=1).readall()
    except ResourceNotFoundError as exc:
        message = f"必要な店舗データ Blob が見つかりません: {blob_name}"
        raise StoreDataDownloadError(message) from exc
    except AzureError as exc:
        raise StoreDataDownloadError(f"店舗データ Blob の取得に失敗しました: {blob_name}") from exc

    temp_file = target_file.with_suffix(f"{target_file.suffix}.tmp")
    temp_file.write_bytes(data)
    temp_file.replace(target_file)


def get_blob_account_url() -> str:
    """Return Blob account URL from explicit URL or account name."""
    account_url = os.getenv("STORE_DATA_BLOB_ACCOUNT_URL", "").strip()
    if account_url:
        return account_url.rstrip("/")

    account_name = os.getenv("STORE_DATA_BLOB_ACCOUNT_NAME", "").strip()
    if account_name:
        return f"https://{account_name}.blob.core.windows.net"

    raise StoreDataConfigurationError(
        "STORE_DATA_BLOB_ACCOUNT_URL または STORE_DATA_BLOB_ACCOUNT_NAME が未設定です"
    )


def get_store_ids() -> tuple[str, ...]:
    """Return store IDs to prepare from Blob."""
    raw = os.getenv("STORE_DATA_STORE_IDS", ",".join(DEFAULT_STORE_IDS))
    store_ids = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not store_ids:
        raise StoreDataConfigurationError("STORE_DATA_STORE_IDS が空です")
    for store_id in store_ids:
        validate_store_id(store_id)
    return store_ids


def get_required_files() -> tuple[str, ...]:
    """Return required file names to download for each store."""
    raw = os.getenv("STORE_DATA_REQUIRED_FILES", ",".join(DEFAULT_REQUIRED_FILES))
    file_names = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not file_names:
        raise StoreDataConfigurationError("STORE_DATA_REQUIRED_FILES が空です")
    for file_name in file_names:
        validate_required_file_name(file_name)
    return file_names


def validate_store_id(store_id: str) -> None:
    """Reject path traversal and unsupported store IDs."""
    if not STORE_ID_PATTERN.match(store_id):
        raise StoreDataConfigurationError(f"不正な store_id です: {store_id}")


def validate_required_file_name(file_name: str) -> None:
    """Allow only simple file names within one store directory."""
    if not SAFE_FILE_PATTERN.match(file_name) or Path(file_name).name != file_name:
        raise StoreDataConfigurationError(f"不正な店舗データファイル名です: {file_name}")


def normalize_blob_prefix(prefix: str | None) -> str:
    """Normalize optional blob prefix without leading or trailing slashes."""
    return (prefix or "").strip().strip("/")


def build_blob_name(prefix: str, store_id: str, file_name: str) -> str:
    """Build the blob path used for one store data file."""
    parts = [part for part in (prefix, store_id, file_name) if part]
    return "/".join(parts)
