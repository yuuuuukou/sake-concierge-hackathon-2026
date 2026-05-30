"""Store-facing catalog data used by the Sake Concierge UI.

This module intentionally stays separate from Retriever.  The Agent keeps using
stuffed Markdown instructions, while the BFF reads CSV facts for cards, links,
and demo metrics.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.api.store_data import (
    STORE_ID_PATTERN,
    StoreDataConfigurationError,
)
from src.api.store_data import (
    get_store_data_dir as resolve_store_data_dir,
)


class StoreNotFoundError(ValueError):
    """Raised when the requested store slug is not configured."""


ALLOWED_PRODUCT_URL_HOSTS = {"example.com"}


STORE_CONFIGS: dict[str, dict[str, Any]] = {
    "fukunotomo": {
        "store_id": "fukunotomo",
        "slug": "fukunotomo",
        "display_name": "サンプル店舗",
        "service_name": "酒あわせAI",
        "headline": "今日の一本を、お好みから一緒に探します",
        "description": (
            "お好み・料理・ご予算に合わせて取扱い酒から候補を比較します。"
            "正確な価格・在庫は公式オンラインストアで確認してください。"
        ),
        "location_label": "サンプル地域",
        "data_label": "サンプル取扱い酒データ",
        "featured_product_ids": [
            "ftm-fuyuki-fff-genshu",
            "ftm-daiginjo-fuku",
            "ftm-fukunotomo-de-fukunotomo-jdg",
        ],
        "quick_prompts": {
            "ja": [
                "甘口で飲みやすいものを3本教えて",
                "魚料理に合う辛口を予算内で",
                "地域らしい一本を選びたい",
                "贈り物向けに華やかな候補を出して",
                "冷やしておいしいものを比較して",
            ],
            "en": [
                "Recommend three easy-drinking sweet sake options.",
                "I want a dry sake for fish around 3,000 yen.",
                "Pick one bottle that feels distinctly Akita.",
                "Suggest a polished gift bottle with product links.",
                "Compare sake that tastes good chilled.",
            ],
            "zh": [
                "请推荐三款容易入口、偏甜的日本酒。",
                "想找一款适合鱼料理、约3000日元的辛口酒。",
                "请选择一款有地域特色的酒。",
                "请推荐适合作为礼物的华丽风格。",
                "请比较适合冰镇饮用的酒。",
            ],
        },
        "next_actions": {
            "ja": [
                "もっと辛口で",
                "ギフト向けに",
                "料理に合わせる",
                "他の候補を見る",
                "在庫がなかったので近い候補を探したい",
            ],
            "en": [
                "Make it drier",
                "For a gift",
                "Pair with food",
                "Explain in English",
                "Show other options",
                "Similar option if unavailable",
            ],
            "zh": [
                "再偏辛口一点",
                "适合作为礼物",
                "搭配料理",
                "用中文说明",
                "看看其他候选",
                "缺货时找相近款",
            ],
        },
        "language_options": [
            {"code": "ja", "label": "日本語", "short_label": "JP"},
            {"code": "en", "label": "English", "short_label": "EN"},
            {"code": "zh", "label": "中文", "short_label": "ZH"},
        ],
        "compliance_notes": [
            "このサイトは非公式ファンサイトです。",
            "AI回答は参考情報です。正確な価格・在庫は公式オンラインストアで確認してください。",
            "20歳未満の飲酒は法律で禁止されています。飲酒は20歳になってから。",
        ],
    }
}


def load_store_profile(store_id: str) -> dict[str, Any]:
    """Return store metadata and products for the UI."""
    config = get_store_config(store_id)
    data_dir = get_store_data_dir(store_id)
    products = load_products(data_dir)
    product_count = len(products)
    updated_dates = [
        sku["verified_on"]
        for product in products
        for sku in product["skus"]
        if sku.get("verified_on")
    ]

    return {
        **config,
        "product_count": product_count,
        "data_updated_on": max(updated_dates) if updated_dates else None,
        "products": products,
    }


def get_store_config(store_id: str) -> dict[str, Any]:
    """Validate and return static store configuration."""
    if not STORE_ID_PATTERN.match(store_id):
        raise StoreNotFoundError(f"Unknown store: {store_id}")
    config = STORE_CONFIGS.get(store_id)
    if not config:
        raise StoreNotFoundError(f"Unknown store: {store_id}")
    return dict(config)


def get_store_data_dir(store_id: str) -> Path:
    """Return the data directory for a configured store."""
    try:
        return resolve_store_data_dir(store_id)
    except (FileNotFoundError, NotADirectoryError, StoreDataConfigurationError) as exc:
        raise StoreNotFoundError(f"Unknown store: {store_id}") from exc


def load_products(data_dir: Path) -> list[dict[str, Any]]:
    """Join product master and SKU CSV files into card-friendly DTOs."""
    master_rows = read_csv(data_dir / "catalog_master.csv")
    sku_rows = read_csv(data_dir / "sku_simplified.csv")
    skus_by_product: dict[str, list[dict[str, str]]] = defaultdict(list)
    for sku in sku_rows:
        skus_by_product[sku.get("product_master_id", "")].append(sku)

    products = [
        build_product(row, skus_by_product.get(row["product_master_id"], []))
        for row in master_rows
        if row.get("prompt_include", "").upper() == "TRUE"
    ]
    return sorted(products, key=lambda product: product["name"])


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read UTF-8 CSV as dictionaries. Missing files behave like empty datasets."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_product(row: dict[str, str], sku_rows: list[dict[str, str]]) -> dict[str, Any]:
    """Convert one master row and its SKUs into a UI product card payload."""
    skus = [build_sku(sku) for sku in sku_rows]
    primary_sku = choose_primary_sku(skus)
    source_urls = split_semicolon(row.get("source_urls", ""))
    official_url = sanitize_official_url(
        primary_sku.get("official_url") or (source_urls[0] if source_urls else "")
    )
    taste_tags = split_semicolon(row.get("taste_tags", ""))
    pairing_tags = split_semicolon(row.get("pairing_tags", ""))
    service_methods = split_semicolon(row.get("service_methods", ""))

    return {
        "id": row["product_master_id"],
        "name": row.get("canonical_name", ""),
        "brewery_name": row.get("brewery_name", ""),
        "series": row.get("series", ""),
        "style_class": row.get("style_class", ""),
        "taste_type": row.get("taste_type", ""),
        "taste_tags": taste_tags,
        "aroma_tags": split_semicolon(row.get("aroma_tags", "")),
        "service_methods": service_methods,
        "pairing_tags": pairing_tags,
        "reason_tags": build_reason_tags(row, taste_tags, pairing_tags, service_methods),
        "summary": build_public_summary(row, taste_tags, pairing_tags, service_methods),
        "official_url": official_url,
        "price_label": build_price_label(skus),
        "stock_status": summarize_stock_status(skus),
        "stock_label": build_stock_label(summarize_stock_status(skus)),
        "verified_on": primary_sku.get("verified_on"),
        "data_quality_note": row.get("data_quality_note", ""),
        "aliases": build_aliases(row),
        "skus": skus,
    }


def build_sku(row: dict[str, str]) -> dict[str, Any]:
    """Normalize one SKU row."""
    return {
        "sku_id": row.get("sku_id", ""),
        "volume_ml": parse_int(row.get("volume_ml")),
        "price_yen": parse_int(row.get("price_yen")),
        "stock_status": row.get("stock_status") or "needs_verification",
        "official_url": sanitize_official_url(row.get("official_url", "")),
        "verified_on": row.get("verified_on", ""),
    }


def sanitize_official_url(value: str) -> str:
    """Allow only official product URLs that are safe to render as card links."""
    url = value.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return ""
    if parsed.hostname not in ALLOWED_PRODUCT_URL_HOSTS:
        return ""
    return url


def choose_primary_sku(skus: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the 720ml SKU for product cards, then fall back to the first SKU."""
    for sku in skus:
        if sku.get("volume_ml") == 720:
            return sku
    return skus[0] if skus else {}


def build_price_label(skus: list[dict[str, Any]]) -> str:
    """Return a compact price label without pretending prices are live."""
    prices = sorted({sku["price_yen"] for sku in skus if sku.get("price_yen")})
    if not prices:
        return "価格は公式確認"
    if len(prices) == 1:
        return f"{prices[0]:,}円"
    return f"{prices[0]:,}円〜{prices[-1]:,}円"


def summarize_stock_status(skus: list[dict[str, Any]]) -> str:
    """Collapse SKU statuses into one conservative product-level status."""
    statuses = {sku.get("stock_status") for sku in skus if sku.get("stock_status")}
    if not statuses:
        return "needs_verification"
    if statuses == {"out_of_stock"}:
        return "out_of_stock"
    if "in_stock" in statuses:
        return "in_stock"
    return "needs_verification"


def build_stock_label(status: str) -> str:
    """Human-readable status for cards."""
    labels = {
        "in_stock": "在庫ありの可能性",
        "out_of_stock": "在庫なしの可能性",
        "needs_verification": "在庫は公式商品ページで確認ください",
    }
    return labels.get(status, "在庫は公式商品ページで確認ください")


def build_reason_tags(
    row: dict[str, str],
    taste_tags: list[str],
    pairing_tags: list[str],
    service_methods: list[str],
) -> list[str]:
    """Pick a few tags that explain why the card is relevant."""
    candidates = [
        row.get("taste_type", ""),
        *taste_tags[:2],
        *pairing_tags[:1],
        *service_methods[:1],
    ]
    return [tag for tag in dict.fromkeys(candidates) if tag][:5]


def build_public_summary(
    row: dict[str, str],
    taste_tags: list[str],
    pairing_tags: list[str],
    service_methods: list[str],
) -> str:
    """Build card copy that does not expose internal memo wording."""
    style = row.get("style_class") or "日本酒"
    taste_type = row.get("taste_type", "")
    if taste_type:
        parts = [f"{taste_type}の{style}です。"]
    else:
        parts = [f"{style}です。"]

    features = [tag for tag in taste_tags[:2] if tag]
    if features:
        parts.append(f"{'・'.join(features)}が特徴です。")

    pairings = [tag for tag in pairing_tags[:2] if tag]
    if pairings:
        parts.append(f"{'・'.join(pairings)}に向いています。")
    elif service_methods:
        parts.append(f"{service_methods[0]}で楽しみやすい一本です。")

    return "".join(parts)


def build_aliases(row: dict[str, str]) -> list[str]:
    """Build lightweight aliases for client-side recommendation matching."""
    name = row.get("canonical_name", "")
    series = row.get("series", "")
    aliases = [name, normalize_alias(name), series]
    if "冬樹FFF" in name:
        aliases.extend(["冬樹FFF", "Fuyuki FFF"])
    if "冬樹" in name:
        aliases.append("冬樹")
    if "福" in name:
        aliases.append("大吟醸 福")
    if "DE Fukunotomo" in name:
        aliases.extend(["DE Fukunotomo", "Fukunotomo DE Fukunotomo"])
    return [alias for alias in dict.fromkeys(aliases) if alias]


def split_semicolon(value: str) -> list[str]:
    """Split semicolon-separated CSV cells."""
    return [item.strip() for item in value.split(";") if item.strip()]


def normalize_alias(value: str) -> str:
    """Remove spaces so Japanese/English mixed names match more easily."""
    return re.sub(r"\s+", "", value or "")


def parse_int(value: str | None) -> int | None:
    """Parse optional integer fields."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


