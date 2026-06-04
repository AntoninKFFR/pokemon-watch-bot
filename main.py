"""pokemon-watch-bot

Watch-only bot for Yahoo Auctions Japan Pokemon deals.

Important safety rule:
- This script never buys anything.
- It only scans public listings, scores them, stores them and can alert Telegram.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import re
import sqlite3
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

import config


@dataclass
class SearchRule:
    name: str
    query: str
    market_price_eur: float
    max_price_yen: int
    required_keywords: List[str]
    blacklist_keywords: List[str]


@dataclass
class ParsedListing:
    listing_id: str
    title: str
    url: str
    price_yen: Optional[int] = None
    context_text: str = ""
    current_price_yen: Optional[int] = None
    buy_now_price_yen: Optional[int] = None
    bid_count: Optional[int] = None
    time_left: str = ""
    seller_name: str = ""
    seller_rating: str = ""
    shipping_japan: str = ""


@dataclass
class ScoredDeal:
    listing_id: str
    title: str
    url: str
    query: str
    rule_name: str
    price_yen: int
    market_price_eur: float
    max_buy_price_yen: int
    total_cost_yen: int
    total_cost_eur: float
    vat_eur: float
    landed_cost_eur: float
    safe_resale_eur: float
    profit_eur: float
    roi_percent: float
    score: float
    listing_type: str
    listing_type_reason: str
    matched_market_keyword: str
    market_price_source: str
    market_price_confidence: str
    detected_at: str


@dataclass
class MarketPriceEntry:
    keyword: str
    market_price_eur: float
    max_buy_price_yen: int
    category: str
    price_source: str
    confidence: str


@dataclass
class ProductAliasEntry:
    japanese_keyword: str
    search_name_fr: str
    search_name_en: str
    cardmarket_query: str
    ebay_query: str
    pricecharting_query: str
    category: str


@dataclass
class ResolvedMarketPrice:
    market_price_eur: float
    source: str
    confidence: str
    sample_size: int
    raw_summary: str
    matched_market_keyword: str
    currency: str = "EUR"
    auto_price_used: bool = False
    auto_price_source: str = ""
    auto_price_sample_size: int = 0
    auto_price_raw_summary: str = ""
    auto_price_last_checked: str = ""


URL_RE = re.compile(r"(https?://auctions\.yahoo\.co\.jp/jp/auction/([a-z]?\d+))")
HEADING_LINK_RE = re.compile(
    r"^#{1,6}\s*\[([^\]]+)\]\((https?://auctions\.yahoo\.co\.jp/jp/auction/([a-z]?\d+))[^)]*\)"
)
PRICE_LINE_RE = re.compile(
    r"^(.*?)\s*(?:現在|即決)\s*([0-9,]+)\s*円\s*(https?://auctions\.yahoo\.co\.jp/jp/auction/([a-z]?\d+))"
)
IMAGE_TITLE_RE = re.compile(r"Image\s+\d+:\s*([^\]]+)")
PRICE_RE = re.compile(r"(?:現在|即決)\s*([0-9,]+)\s*円")
YAHOO_AUCTION_ID_RE = re.compile(r"auctions\.yahoo\.co\.jp/jp/auction/([a-z]?\d+)", re.IGNORECASE)
BUYOUT_PRICE_RE = re.compile(r"即決\s*[0-9,]+\s*円")
CURRENT_PRICE_RE = re.compile(r"現在\s*([0-9,]+)\s*円")
BUY_NOW_PRICE_RE = re.compile(r"即決\s*([0-9,]+)\s*円")
BID_COUNT_RE = re.compile(r"入札\s*([0-9,]+)")
TIME_LEFT_RE = re.compile(r"残り\s*([^\s|]+)")
SELLER_RE = re.compile(r"出品者[:：]\s*([^\s|]+)")
SELLER_RATING_RE = re.compile(r"評価[:：]\s*([0-9.]+)")

DB_EXPORT_COLUMNS = [
    "listing_id",
    "title",
    "url",
    "query",
    "rule_name",
    "price_yen",
    "market_price_eur",
    "max_buy_price_yen",
    "total_cost_yen",
    "total_cost_eur",
    "vat_eur",
    "landed_cost_eur",
    "safe_resale_eur",
    "profit_eur",
    "roi_percent",
    "score",
    "listing_type",
    "listing_type_reason",
    "matched_market_keyword",
    "market_price_source",
    "market_price_confidence",
    "auto_price_used",
    "auto_price_source",
    "auto_price_sample_size",
    "auto_price_raw_summary",
    "auto_price_last_checked",
    "detected_at",
]

GOOGLE_DEALS_WORKSHEET_NAME = "Deals"
GOOGLE_DEALS_HEADERS = [
    "created_at",
    "decision",
    "manual_action_needed",
    "listing_type",
    "title",
    "price_yen",
    "total_cost_eur",
    "manual_market_price_eur",
    "market_price_eur",
    "profit_eur",
    "roi_percent",
    "link_for_zenmarket",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "notes",
    "url",
    "buy_now_price_yen",
    "current_price_yen",
    "bid_count",
    "time_left",
    "seller_name",
    "seller_rating",
    "shipping_japan",
    "query",
    "rule_name",
    "data_source",
    "listing_type_reason",
    "max_buy_price_yen",
    "total_cost_yen",
    "vat_eur",
    "landed_cost_eur",
    "safe_resale_eur",
    "matched_market_keyword",
    "market_price_source",
    "market_price_confidence",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
    "auto_price_used",
    "auto_price_source",
    "auto_price_sample_size",
    "auto_price_raw_summary",
    "auto_price_last_checked",
    "matched_product_japanese",
    "search_name_fr",
    "search_name_en",
    "cardmarket_query",
    "ebay_query",
    "pricecharting_query",
    "pricecharting_search_url",
    "auction_warning",
    "risk_flags",
    "score",
    "score_reliability",
    "deal_quality_score",
]

GOOGLE_BEST_DEALS_HEADERS = [
    "created_at",
    "decision",
    "manual_action_needed",
    "listing_type",
    "title",
    "price_yen",
    "total_cost_eur",
    "manual_market_price_eur",
    "market_price_eur",
    "profit_eur",
    "roi_percent",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
    "link_for_zenmarket",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "notes",
    "url",
]

GOOGLE_BUY_NOW_DEALS_HEADERS = [
    "created_at",
    "decision",
    "manual_action_needed",
    "listing_type",
    "title",
    "price_yen",
    "buy_now_price_yen",
    "total_cost_eur",
    "manual_market_price_eur",
    "market_price_eur",
    "profit_eur",
    "roi_percent",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
    "link_for_zenmarket",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "notes",
    "url",
]

GOOGLE_AUCTIONS_WATCH_HEADERS = [
    "created_at",
    "decision",
    "manual_action_needed",
    "listing_type",
    "title",
    "price_yen",
    "current_price_yen",
    "bid_count",
    "time_left",
    "total_cost_eur",
    "manual_market_price_eur",
    "market_price_eur",
    "profit_eur",
    "roi_percent",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
    "link_for_zenmarket",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "notes",
    "url",
]

GOOGLE_NEEDS_PRICE_HEADERS = [
    "created_at",
    "manual_action_needed",
    "listing_type",
    "title",
    "price_yen",
    "total_cost_eur",
    "manual_market_price_eur",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "pricecharting_search_url",
    "link_for_zenmarket",
    "notes",
    "url",
]

GOOGLE_BEST_WORKSHEET_NAME = "Best Deals"
GOOGLE_BUY_NOW_WORKSHEET_NAME = "Buy Now Deals"
GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME = "Auctions Watch"
GOOGLE_NEEDS_PRICE_WORKSHEET_NAME = "Needs Price"

GOOGLE_HEADER_LABELS = {
    "created_at": "Date ajout",
    "decision": "Statut",
    "manual_action_needed": "Action requise",
    "listing_type": "Type annonce",
    "listing_type_reason": "Raison type",
    "title": "Titre",
    "price_yen": "Prix Japon ¥",
    "current_price_yen": "Prix actuel ¥",
    "buy_now_price_yen": "Prix achat immédiat ¥",
    "total_cost_eur": "Coût total estimé €",
    "manual_market_price_eur": "Prix revente manuel €",
    "market_price_eur": "Prix marché €",
    "profit_eur": "Marge estimée €",
    "roi_percent": "ROI %",
    "manual_price_source": "Source prix manuel",
    "manual_price_confidence": "Fiabilité prix manuel",
    "manual_status": "Statut manuel",
    "matched_market_keyword": "Mot-clé prix marché",
    "market_price_source": "Source prix marché",
    "market_price_confidence": "Fiabilité prix marché",
    "matched_product_japanese": "Produit japonais détecté",
    "search_name_fr": "Nom FR recherche",
    "search_name_en": "Nom EN recherche",
    "auction_warning": "Alerte enchère",
    "score_reliability": "Fiabilité score",
    "deal_quality_score": "Score qualité",
    "risk_flags": "Risques",
    "bid_count": "Nombre d'enchères",
    "time_left": "Temps restant",
    "seller_name": "Vendeur",
    "seller_rating": "Note vendeur",
    "shipping_japan": "Livraison Japon",
    "link_for_zenmarket": "Lien ZenMarket",
    "cardmarket_search_url": "Recherche Cardmarket",
    "ebay_sold_search_url": "Recherche eBay vendu",
    "pricecharting_search_url": "Recherche PriceCharting",
    "notes": "Notes",
    "url": "Lien Yahoo original",
    "data_source": "Source donnée",
    "auto_price_used": "Prix auto utilisé",
    "auto_price_source": "Source prix auto",
    "auto_price_sample_size": "Échantillon prix auto",
    "auto_price_raw_summary": "Résumé prix auto",
    "auto_price_last_checked": "Dernière vérification prix auto",
    "query": "Recherche utilisée",
    "rule_name": "Règle",
    "max_buy_price_yen": "Prix max achat ¥",
    "total_cost_yen": "Coût total estimé ¥",
    "vat_eur": "TVA estimée €",
    "landed_cost_eur": "Coût livré estimé €",
    "safe_resale_eur": "Revente sécurisée €",
    "score": "Score",
    "cardmarket_query": "Requête Cardmarket",
    "ebay_query": "Requête eBay",
    "pricecharting_query": "Requête PriceCharting",
}
GOOGLE_FRENCH_TO_INTERNAL_HEADERS = {value: key for key, value in GOOGLE_HEADER_LABELS.items()}
DISPLAY_VALUE_MAPS = {
    "decision": {
        "BUY ALERT": "À ACHETER",
        "WATCH": "À surveiller",
        "WATCH_AUCTION": "Enchère à surveiller",
        "WATCH_LOW_AUCTION": "Enchère basse à vérifier",
        "NEEDS_PRICE": "Prix à renseigner",
        "SKIP": "Ignoré",
        "IGNORE": "Ignoré manuel",
    },
    "manual_action_needed": {
        "FILL_PRICE": "Prix à remplir",
        "VALIDATE": "À valider",
        "OK": "OK",
        "IGNORE": "Ignoré",
    },
    "listing_type": {
        "BUY_NOW": "Achat immédiat",
        "FIXED_PRICE": "Prix fixe",
        "AUCTION": "Enchère",
        "LOW_START_AUCTION": "Enchère basse",
        "UNKNOWN": "Inconnu",
    },
    "manual_price_confidence": {
        "HIGH": "Haute",
        "MEDIUM": "Moyenne",
        "LOW": "Faible",
    },
    "market_price_confidence": {
        "HIGH": "Haute",
        "MEDIUM": "Moyenne",
        "LOW": "Faible",
    },
    "score_reliability": {
        "HIGH": "Haute",
        "MEDIUM": "Moyenne",
        "LOW": "Faible",
    },
    "manual_status": {
        "VALIDATED": "Validé",
        "TO_CHECK": "À vérifier",
        "IGNORE": "Ignoré",
    },
    "data_source": {
        "Yahoo Auctions Japan": "Yahoo Auctions Japon",
    },
    "manual_price_source": {
        "Cardmarket": "Cardmarket",
        "eBay sold": "eBay vendu",
        "PriceCharting": "PriceCharting",
        "Manual": "Manuel",
        "Other": "Autre",
    },
    "listing_type_reason": {
        "default_yahoo_auction": "Enchère Yahoo par défaut",
        "detected_low_start": "Enchère basse détectée",
        "detected_buy_now_keyword": "Mot-clé achat immédiat",
        "detected_fixed_price_keyword": "Mot-clé prix fixe",
        "detected_buyout_price": "Prix immédiat détecté",
        "unknown_source": "Source inconnue",
    },
    "auto_price_used": {
        "True": "Oui",
        "False": "Non",
    },
}
RISK_FLAG_LABELS = {
    "LOW_START_AUCTION": "Enchère basse",
    "NO_SHRINK": "Sans shrink",
    "OPENED": "Ouvert",
    "EMPTY_BOX": "Boîte vide",
    "SEARCHED_PACK": "Pack recherché",
    "MYSTERY_PACK": "Pack mystère",
    "DAMAGED": "Abîmé",
    "GENERIC_LOT": "Lot générique",
    "UNKNOWN_PRICE": "Prix inconnu",
    "SELLER_RISK": "Risque vendeur",
}
REVERSE_DISPLAY_VALUE_MAPS = {
    field: {visible: internal for internal, visible in mapping.items()}
    for field, mapping in DISPLAY_VALUE_MAPS.items()
}
USER_EDITABLE_FIELDS = [
    "manual_market_price_eur",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
    "notes",
]
MANUAL_SOURCE_NORMALIZATION = {
    "cardmarket": "Cardmarket",
    "ebay vendu": "eBay sold",
    "ebay sold": "eBay sold",
    "pricecharting": "PriceCharting",
    "manuel": "Manual",
    "manual": "Manual",
    "autre": "Other",
    "other": "Other",
}

DECISION_BUY_ALERT = "BUY ALERT"
DECISION_WATCH = "WATCH"
DECISION_WATCH_AUCTION = "WATCH_AUCTION"
DECISION_WATCH_LOW_AUCTION = "WATCH_LOW_AUCTION"
DECISION_NEEDS_PRICE = "NEEDS_PRICE"
DECISION_SKIP = "SKIP"
DECISION_IGNORE = "IGNORE"

LISTING_TYPE_BUY_NOW = "BUY_NOW"
LISTING_TYPE_FIXED_PRICE = "FIXED_PRICE"
LISTING_TYPE_AUCTION = "AUCTION"
LISTING_TYPE_LOW_START_AUCTION = "LOW_START_AUCTION"
LISTING_TYPE_UNKNOWN = "UNKNOWN"
LOW_START_WARNING_VALUE = "LOW_START_AUCTION"
LOW_START_KEYWORDS = ["1円スタート！！", "1円スタート", "1円", "売り切り"]
LISTING_TYPE_REASON_DEFAULT_YAHOO_AUCTION = "default_yahoo_auction"
LISTING_TYPE_REASON_DETECTED_LOW_START = "detected_low_start"
LISTING_TYPE_REASON_DETECTED_BUY_NOW_KEYWORD = "detected_buy_now_keyword"
LISTING_TYPE_REASON_DETECTED_FIXED_PRICE_KEYWORD = "detected_fixed_price_keyword"
LISTING_TYPE_REASON_DETECTED_BUYOUT_PRICE = "detected_buyout_price"
LISTING_TYPE_REASON_UNKNOWN_SOURCE = "unknown_source"
SCORE_RELIABILITY_HIGH = "HIGH"
SCORE_RELIABILITY_MEDIUM = "MEDIUM"
SCORE_RELIABILITY_LOW = "LOW"
MANUAL_STATUS_TO_CHECK = "TO_CHECK"
MANUAL_STATUS_VALIDATED = "VALIDATED"
MANUAL_STATUS_IGNORE = "IGNORE"
MANUAL_SOURCE_DEFAULT = "manual_google_sheet"
MARKET_PRICES: List[MarketPriceEntry] = []
PRODUCT_ALIASES: List[ProductAliasEntry] = []
CONFIDENCE_RANK = {
    SCORE_RELIABILITY_LOW: 0,
    SCORE_RELIABILITY_MEDIUM: 1,
    SCORE_RELIABILITY_HIGH: 2,
}
COMMON_REPORT_KEYWORDS = [
    "アビスアイ",
    "ムニキスゼロ",
    "MEGAドリームex",
    "メガブレイブ",
    "メガシンフォニア",
    "レイジングサーフ",
    "スペシャルBOX",
    "フクオカ",
    "ヒロシマ",
    "151 BOX",
    "ポケモンカード151",
    "テラスタルフェス",
    "シャイニートレジャー",
    "クレイバースト",
    "スノーハザード",
    "黒炎の支配者",
    "VSTARユニバース",
    "VMAXクライマックス",
    "ロストアビス",
    "白熱のアルカナ",
    "イーブイヒーローズ",
    "蒼空ストリーム",
    "双璧のファイター",
    "BOX",
    "シュリンク付き",
    "未開封",
    "PSA10",
    "PSA9",
    "ARS10",
    "SAR",
    "SR",
    "旧裏",
    "プロモ",
    "引退品",
    "まとめ売り",
    "大量",
    "キラ",
    "ホロ",
    "ミラー",
]
GENERIC_SUGGESTION_SKIP = {
    "BOX",
    "BOX_UNKNOWN",
    "UNKNOWN_PRODUCT",
    "シュリンク付き",
    "未開封",
    "SAR",
    "SR",
    "キラ",
    "ホロ",
    "ミラー",
}
PRESERVED_SHEET_FIELDS = [
    "notes",
    "manual_market_price_eur",
    "manual_price_source",
    "manual_price_confidence",
    "manual_status",
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def to_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_keywords_from_file(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        values = [line.strip() for line in handle if line.strip() and not line.strip().startswith("#")]
    return values


def load_blacklist_keywords() -> List[str]:
    return load_keywords_from_file(config.BLACKLIST_WORDS_FILE) or list(config.GLOBAL_BLACKLIST_KEYWORDS)


def load_market_prices(csv_path: str = "market_prices.csv") -> List[MarketPriceEntry]:
    if not os.path.exists(csv_path):
        logging.warning("Market prices file not found: %s", csv_path)
        return []

    entries: List[MarketPriceEntry] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            keyword = (row.get("keyword") or "").strip()
            if not keyword:
                continue
            entries.append(
                MarketPriceEntry(
                    keyword=keyword,
                    market_price_eur=to_float(row.get("market_price_eur", 0.0)),
                    max_buy_price_yen=int(to_float(row.get("max_buy_price_yen", 0))),
                    category=(row.get("category") or "").strip(),
                    price_source=(row.get("price_source") or "").strip(),
                    confidence=((row.get("confidence") or "").strip() or SCORE_RELIABILITY_LOW).upper(),
                )
            )
    entries.sort(key=lambda entry: len(entry.keyword), reverse=True)
    return entries


def load_product_aliases(csv_path: str = "product_aliases.csv") -> List[ProductAliasEntry]:
    if not os.path.exists(csv_path):
        logging.warning("Product aliases file not found: %s", csv_path)
        return []

    entries: List[ProductAliasEntry] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            japanese_keyword = (row.get("japanese_keyword") or "").strip()
            if not japanese_keyword:
                continue
            entries.append(
                ProductAliasEntry(
                    japanese_keyword=japanese_keyword,
                    search_name_fr=(row.get("search_name_fr") or "").strip(),
                    search_name_en=(row.get("search_name_en") or "").strip(),
                    cardmarket_query=(row.get("cardmarket_query") or "").strip(),
                    ebay_query=(row.get("ebay_query") or "").strip(),
                    pricecharting_query=(row.get("pricecharting_query") or "").strip(),
                    category=(row.get("category") or "").strip(),
                )
            )
    entries.sort(key=lambda entry: len(entry.japanese_keyword), reverse=True)
    return entries


def match_market_price(title: str, market_prices: List[MarketPriceEntry]) -> Optional[MarketPriceEntry]:
    title_folded = title.casefold()
    for entry in market_prices:
        if entry.keyword.casefold() in title_folded:
            return entry
    return None


def match_product_alias(title: str, aliases: List[ProductAliasEntry]) -> Optional[ProductAliasEntry]:
    title_folded = title.casefold()
    for entry in aliases:
        if entry.japanese_keyword.casefold() in title_folded:
            return entry
    return None


def confidence_at_least(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_RANK.get((confidence or "").upper(), 0) >= CONFIDENCE_RANK.get((minimum or "").upper(), 0)


def utc_now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


def normalize_manual_status(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = REVERSE_DISPLAY_VALUE_MAPS.get("manual_status", {}).get(cleaned, cleaned)
    cleaned = cleaned.upper()
    if cleaned in (MANUAL_STATUS_TO_CHECK, MANUAL_STATUS_VALIDATED, MANUAL_STATUS_IGNORE):
        return cleaned
    return cleaned


def normalize_manual_source(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    normalized = MANUAL_SOURCE_NORMALIZATION.get(cleaned.casefold(), cleaned)
    return normalized


def normalize_confidence(value: str, default: str = SCORE_RELIABILITY_LOW) -> str:
    cleaned = (value or "").strip()
    reverse_conf = {
        **REVERSE_DISPLAY_VALUE_MAPS.get("manual_price_confidence", {}),
        **REVERSE_DISPLAY_VALUE_MAPS.get("market_price_confidence", {}),
        **REVERSE_DISPLAY_VALUE_MAPS.get("score_reliability", {}),
    }
    cleaned = reverse_conf.get(cleaned, cleaned)
    cleaned = cleaned.upper()
    if cleaned in (SCORE_RELIABILITY_HIGH, SCORE_RELIABILITY_MEDIUM, SCORE_RELIABILITY_LOW):
        return cleaned
    return default


def compute_manual_action_needed(
    manual_market_price_eur: float,
    manual_status: str,
) -> str:
    if manual_status == MANUAL_STATUS_IGNORE:
        return MANUAL_STATUS_IGNORE
    if manual_market_price_eur <= 0:
        return "FILL_PRICE"
    if manual_market_price_eur > 0 and not manual_status:
        return "VALIDATE"
    if manual_market_price_eur > 0 and manual_status == MANUAL_STATUS_VALIDATED:
        return "OK"
    if manual_market_price_eur > 0 and manual_status == MANUAL_STATUS_TO_CHECK:
        return "VALIDATE"
    return ""


def get_sheet_header_label(header: str) -> str:
    return GOOGLE_HEADER_LABELS.get(header, header)


def get_internal_header_name(header: str) -> str:
    return GOOGLE_FRENCH_TO_INTERNAL_HEADERS.get(header, header)


def get_sheet_display_headers(headers: List[str]) -> List[str]:
    return [get_sheet_header_label(header) for header in headers]


def translate_visible_value(field: str, value: str) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text
    if field == "risk_flags":
        labels = [RISK_FLAG_LABELS.get(flag.strip(), flag.strip()) for flag in text.split("|") if flag.strip()]
        return " | ".join(labels)
    mapping = DISPLAY_VALUE_MAPS.get(field, {})
    return mapping.get(text, text)


def parse_visible_value(field: str, value: str) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text
    if field == "risk_flags":
        reverse_risks = {visible: internal for internal, visible in RISK_FLAG_LABELS.items()}
        labels = [reverse_risks.get(flag.strip(), flag.strip()) for flag in text.split("|") if flag.strip()]
        return " | ".join(labels)
    mapping = REVERSE_DISPLAY_VALUE_MAPS.get(field, {})
    return mapping.get(text, text)


def build_sheet_display_row(headers: List[str], row: Dict[str, str]) -> List[str]:
    return [translate_visible_value(header, row.get(header, "")) for header in headers]


def column_letter(index: int) -> str:
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def rgb(red: int, green: int, blue: int) -> Dict[str, float]:
    return {
        "red": round(red / 255, 4),
        "green": round(green / 255, 4),
        "blue": round(blue / 255, 4),
    }


STATUS_BACKGROUND_COLORS = {
    "BUY ALERT": rgb(198, 239, 206),
    "WATCH": rgb(221, 235, 247),
    "WATCH_AUCTION": rgb(252, 228, 214),
    "WATCH_LOW_AUCTION": rgb(255, 242, 204),
    "NEEDS_PRICE": rgb(244, 204, 204),
    "SKIP": rgb(224, 224, 224),
    "IGNORE": rgb(224, 224, 224),
}
ACTION_BACKGROUND_COLORS = {
    "FILL_PRICE": rgb(255, 242, 204),
    "VALIDATE": rgb(252, 228, 214),
    "OK": rgb(198, 239, 206),
    "IGNORE": rgb(224, 224, 224),
}
HEADER_BACKGROUND_COLOR = rgb(217, 217, 217)
EDITABLE_BACKGROUND_COLOR = rgb(255, 249, 196)
DEFAULT_BACKGROUND_COLOR = rgb(255, 255, 255)


def build_base_sheet_format_requests(worksheet_id: int, header_count: int, row_count: int) -> List[Dict[str, object]]:
    end_row_index = max(2, row_count + 1)
    return [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": worksheet_id,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": header_count,
                    }
                }
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": worksheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": header_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HEADER_BACKGROUND_COLOR,
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": worksheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row_index,
                    "startColumnIndex": 0,
                    "endColumnIndex": header_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": DEFAULT_BACKGROUND_COLOR,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
    ]


def build_data_validation_request(
    worksheet_id: int,
    col_index: int,
    row_count: int,
    options: List[str],
) -> Dict[str, object]:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": 1,
                "endRowIndex": max(2, row_count + 1),
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": option} for option in options],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }


def apply_google_sheet_formatting(
    spreadsheet: object,
    worksheet: object,
    worksheet_name: str,
    headers: List[str],
    rows: List[Dict[str, str]],
) -> None:
    worksheet_id = getattr(worksheet, "id", None)
    if worksheet_id is None:
        return

    requests: List[Dict[str, object]] = build_base_sheet_format_requests(
        worksheet_id=worksheet_id,
        header_count=len(headers),
        row_count=len(rows),
    )

    end_row_index = max(2, len(rows) + 1)
    if worksheet_name in (
        GOOGLE_BEST_WORKSHEET_NAME,
        GOOGLE_BUY_NOW_WORKSHEET_NAME,
        GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME,
        GOOGLE_NEEDS_PRICE_WORKSHEET_NAME,
    ):
        for field in USER_EDITABLE_FIELDS:
            if field not in headers:
                continue
            col_index = headers.index(field)
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": end_row_index,
                            "startColumnIndex": col_index,
                            "endColumnIndex": col_index + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": EDITABLE_BACKGROUND_COLOR,
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )

        dropdowns = {
            "manual_price_source": ["Cardmarket", "eBay vendu", "PriceCharting", "Manuel", "Autre"],
            "manual_price_confidence": ["Haute", "Moyenne", "Faible"],
            "manual_status": ["Validé", "À vérifier", "Ignoré"],
        }
        for field, options in dropdowns.items():
            if field in headers:
                requests.append(
                    build_data_validation_request(
                        worksheet_id=worksheet_id,
                        col_index=headers.index(field),
                        row_count=len(rows),
                        options=options,
                    )
                )

    status_col = headers.index("decision") if "decision" in headers else None
    action_col = headers.index("manual_action_needed") if "manual_action_needed" in headers else None
    for row_index, row in enumerate(rows, start=1):
        if status_col is not None:
            status_color = STATUS_BACKGROUND_COLORS.get(row.get("decision", ""))
            if status_color:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet_id,
                                "startRowIndex": row_index,
                                "endRowIndex": row_index + 1,
                                "startColumnIndex": status_col,
                                "endColumnIndex": status_col + 1,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": status_color}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                )
        if action_col is not None:
            action_color = ACTION_BACKGROUND_COLORS.get(row.get("manual_action_needed", ""))
            if action_color:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet_id,
                                "startRowIndex": row_index,
                                "endRowIndex": row_index + 1,
                                "startColumnIndex": action_col,
                                "endColumnIndex": action_col + 1,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": action_color}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                )

    requests.append(
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": worksheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": len(headers),
                }
            }
        }
    )

    spreadsheet.batch_update({"requests": requests})


def extract_listing_metadata(title: str, context_text: str, price_yen: int) -> Dict[str, str]:
    haystack = combine_context(title, context_text)

    def _extract_int(pattern: re.Pattern[str]) -> str:
        match = pattern.search(haystack)
        if not match:
            return ""
        return match.group(1).replace(",", "").strip()

    current_price = _extract_int(CURRENT_PRICE_RE) or (str(price_yen) if price_yen > 0 else "")
    buy_now_price = _extract_int(BUY_NOW_PRICE_RE)
    bid_count = _extract_int(BID_COUNT_RE)
    time_left_match = TIME_LEFT_RE.search(haystack)
    seller_match = SELLER_RE.search(haystack)
    seller_rating_match = SELLER_RATING_RE.search(haystack)

    shipping_japan = ""
    if "送料無料" in haystack:
        shipping_japan = "送料無料"

    return {
        "current_price_yen": current_price,
        "buy_now_price_yen": buy_now_price,
        "bid_count": bid_count,
        "time_left": time_left_match.group(1).strip() if time_left_match else "",
        "seller_name": seller_match.group(1).strip() if seller_match else "",
        "seller_rating": seller_rating_match.group(1).strip() if seller_rating_match else "",
        "shipping_japan": shipping_japan,
    }


def build_risk_flags(
    title: str,
    context_text: str,
    listing_type: str,
    decision: str,
    manual_market_price_eur: float,
    seller_rating: str,
) -> List[str]:
    flags: List[str] = []
    haystack = combine_context(title or "", context_text or "")

    if listing_type == LISTING_TYPE_LOW_START_AUCTION:
        flags.append("LOW_START_AUCTION")
    if any(keyword in haystack for keyword in ("シュリンクなし", "シュリンク無し")):
        flags.append("NO_SHRINK")
    if "開封済み" in haystack:
        flags.append("OPENED")
    if any(keyword in haystack for keyword in ("空箱", "箱のみ", "パックのみ")):
        flags.append("EMPTY_BOX")
    if "サーチ済み" in haystack:
        flags.append("SEARCHED_PACK")
    if "オリパ" in haystack:
        flags.append("MYSTERY_PACK")
    if any(keyword in haystack for keyword in ("傷あり", "破損", "ジャンク")):
        flags.append("DAMAGED")
    if any(keyword in haystack for keyword in ("まとめ売り", "引退品", "大量")):
        flags.append("GENERIC_LOT")
    if decision == DECISION_NEEDS_PRICE and manual_market_price_eur <= 0:
        flags.append("UNKNOWN_PRICE")
    if seller_rating:
        try:
            if float(seller_rating) < 95.0:
                flags.append("SELLER_RISK")
        except ValueError:
            pass

    unique_flags: List[str] = []
    for flag in flags:
        if flag not in unique_flags:
            unique_flags.append(flag)
    return unique_flags


def compute_deal_quality_score(
    listing_type: str,
    matched_product_japanese: str,
    manual_market_price_eur: float,
    title: str,
    risk_flags: List[str],
    decision: str,
) -> int:
    score = 0
    if listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE):
        score += 30
    if matched_product_japanese:
        score += 20
    if manual_market_price_eur > 0:
        score += 20
    if "シュリンク付き" in title:
        score += 10
    if "未開封" in title:
        score += 10
    if "LOW_START_AUCTION" in risk_flags:
        score -= 30
    if any(flag in risk_flags for flag in ("NO_SHRINK", "EMPTY_BOX", "SEARCHED_PACK", "MYSTERY_PACK", "DAMAGED")):
        score -= 50
    if decision == DECISION_NEEDS_PRICE:
        score -= 20
    if "GENERIC_LOT" in risk_flags:
        score -= 10
    return max(-100, min(100, score))


def _append_rule_if_new(
    rules: List[SearchRule],
    known_queries: set,
    name_prefix: str,
    query: str,
    market_price_eur: float,
    max_price_yen: int,
) -> None:
    if query in known_queries:
        return
    rules.append(
        SearchRule(
            name=f"{name_prefix}: {query}",
            query=query,
            market_price_eur=market_price_eur,
            max_price_yen=max_price_yen,
            required_keywords=[],
            blacklist_keywords=[],
        )
    )
    known_queries.add(query)


def load_rules() -> List[SearchRule]:
    rules: List[SearchRule] = []
    known_queries: set = set()

    for raw in config.SEARCH_RULES:
        query = raw["query"]
        _append_rule_if_new(
            rules=rules,
            known_queries=known_queries,
            name_prefix=raw["name"],
            query=query,
            market_price_eur=float(raw["market_price_eur"]),
            max_price_yen=int(raw["max_price_yen"]),
        )
        # Keep explicit required/blacklist settings from base rules.
        rules[-1].required_keywords = list(raw.get("required_keywords", []))
        rules[-1].blacklist_keywords = list(raw.get("blacklist_keywords", []))

    buy_now_queries = load_keywords_from_file(config.KEYWORDS_BUY_NOW_FILE) or list(config.KEYWORDS_BUY_NOW_DEFAULT)
    auction_queries = load_keywords_from_file(config.KEYWORDS_AUCTION_FILE) or list(config.KEYWORDS_AUCTION_DEFAULT)
    lots_queries = load_keywords_from_file(config.KEYWORDS_LOTS_FILE) or list(config.KEYWORDS_LOTS_DEFAULT)

    for query in buy_now_queries:
        _append_rule_if_new(
            rules=rules,
            known_queries=known_queries,
            name_prefix="BUY_NOW keyword",
            query=query,
            market_price_eur=float(config.BUY_NOW_RULE_DEFAULTS["market_price_eur"]),
            max_price_yen=int(config.BUY_NOW_RULE_DEFAULTS["max_price_yen"]),
        )

    for query in auction_queries:
        _append_rule_if_new(
            rules=rules,
            known_queries=known_queries,
            name_prefix="AUCTION keyword",
            query=query,
            market_price_eur=float(config.AUCTION_RULE_DEFAULTS["market_price_eur"]),
            max_price_yen=int(config.AUCTION_RULE_DEFAULTS["max_price_yen"]),
        )

    for query in lots_queries:
        _append_rule_if_new(
            rules=rules,
            known_queries=known_queries,
            name_prefix="LOTS keyword",
            query=query,
            market_price_eur=float(config.LOTS_RULE_DEFAULTS["market_price_eur"]),
            max_price_yen=int(config.LOTS_RULE_DEFAULTS["max_price_yen"]),
        )

    return rules


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def parse_yen_amount(text: str) -> Optional[int]:
    match = PRICE_RE.search(text)
    if not match:
        return None
    amount = match.group(1).replace(",", "")
    try:
        return int(amount)
    except ValueError:
        return None


def combine_context(*parts: str) -> str:
    return " | ".join([part for part in parts if part]).strip()


def extract_yahoo_auction_id(url: str) -> Optional[str]:
    if not url:
        return None
    match = YAHOO_AUCTION_ID_RE.search(url.strip())
    if not match:
        return None
    return match.group(1)


def build_zenmarket_auction_url(url: str) -> str:
    auction_id = extract_yahoo_auction_id(url)
    if not auction_id:
        # Fallback allowed by requirement for non Yahoo Auctions URLs.
        return url or ""
    return f"https://zenmarket.jp/auction.aspx?itemCode={auction_id}"


def build_cardmarket_search_url(query: str) -> str:
    if not query:
        return ""
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={encoded}"


def build_ebay_sold_search_url(query: str) -> str:
    if not query:
        return ""
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.ebay.com/sch/i.html?_nkw={encoded}&LH_Sold=1&LH_Complete=1"


def build_pricecharting_search_url(query: str) -> str:
    if not query:
        return ""
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.pricecharting.com/search-products?type=prices&q={encoded}"


def get_auction_warning(title: str, context_text: str = "") -> str:
    title_normalized = combine_context(title or "", context_text or "")
    for keyword in LOW_START_KEYWORDS:
        if keyword in title_normalized:
            return LOW_START_WARNING_VALUE
    return ""


def detect_listing_type(title: str, context_text: str, url: str) -> tuple[str, str]:
    haystack = combine_context(title, context_text)

    for keyword in config.LISTING_TYPE_KEYWORDS.get("low_start_auction", []):
        if keyword and keyword in haystack:
            return LISTING_TYPE_LOW_START_AUCTION, LISTING_TYPE_REASON_DETECTED_LOW_START

    if BUYOUT_PRICE_RE.search(haystack):
        return LISTING_TYPE_BUY_NOW, LISTING_TYPE_REASON_DETECTED_BUYOUT_PRICE

    for keyword in config.LISTING_TYPE_KEYWORDS.get("buy_now", []):
        if keyword and keyword in haystack:
            return LISTING_TYPE_BUY_NOW, LISTING_TYPE_REASON_DETECTED_BUY_NOW_KEYWORD

    for keyword in config.LISTING_TYPE_KEYWORDS.get("fixed_price", []):
        if keyword and keyword in haystack:
            return LISTING_TYPE_FIXED_PRICE, LISTING_TYPE_REASON_DETECTED_FIXED_PRICE_KEYWORD

    if extract_yahoo_auction_id(url):
        return LISTING_TYPE_AUCTION, LISTING_TYPE_REASON_DEFAULT_YAHOO_AUCTION

    return LISTING_TYPE_UNKNOWN, LISTING_TYPE_REASON_UNKNOWN_SOURCE


def resolve_listing_type_from_deal(deal: Dict[str, object]) -> str:
    title = str(deal.get("title", ""))
    url = str(deal.get("url", ""))
    current = str(deal.get("listing_type", "")).strip()
    if current and current != LISTING_TYPE_UNKNOWN:
        return current
    listing_type, _ = detect_listing_type(title, "", url)
    return listing_type


def resolve_listing_type_reason_from_deal(deal: Dict[str, object]) -> str:
    title = str(deal.get("title", ""))
    url = str(deal.get("url", ""))
    current = str(deal.get("listing_type_reason", "")).strip()
    if current:
        return current
    _, reason = detect_listing_type(title, "", url)
    return reason


def get_score_reliability(listing_type: str, market_price_confidence: str, decision: str) -> str:
    if decision == DECISION_NEEDS_PRICE:
        return SCORE_RELIABILITY_LOW
    if market_price_confidence == SCORE_RELIABILITY_HIGH and listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE):
        return SCORE_RELIABILITY_HIGH
    if market_price_confidence == SCORE_RELIABILITY_MEDIUM:
        return SCORE_RELIABILITY_MEDIUM
    if listing_type in (LISTING_TYPE_LOW_START_AUCTION, LISTING_TYPE_AUCTION, LISTING_TYPE_UNKNOWN) or market_price_confidence == SCORE_RELIABILITY_LOW:
        return SCORE_RELIABILITY_LOW
    return SCORE_RELIABILITY_MEDIUM


def build_yahoo_reader_url(query: str) -> str:
    encoded_query = urllib.parse.quote_plus(query)
    raw_url = config.YAHOO_SEARCH_URL_TEMPLATE.format(query=encoded_query)
    raw_url = raw_url.replace("https://", "").replace("http://", "")
    return f"{config.JINA_READER_PREFIX}{raw_url}"


def fetch_markdown(session: requests.Session, query: str) -> str:
    url = build_yahoo_reader_url(query)
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, config.REQUEST_RETRIES + 2):
        try:
            response = session.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logging.warning(
                "Fetch failed for query '%s' (attempt %s): %s",
                query,
                attempt,
                exc,
            )
            time.sleep(1.2)

    if last_error:
        raise last_error
    raise RuntimeError("Unknown fetch error")


def parse_listings_from_markdown(markdown: str, max_items: int) -> List[ParsedListing]:
    items: Dict[str, ParsedListing] = {}

    for line in markdown.splitlines():
        line = normalize_text(line)
        if "/jp/auction/" not in line:
            continue

        heading_match = HEADING_LINK_RE.search(line)
        if heading_match:
            title = normalize_text(heading_match.group(1))
            url = heading_match.group(2)
            listing_id = heading_match.group(3)
            existing = items.get(listing_id)
            if not existing:
                items[listing_id] = ParsedListing(
                    listing_id=listing_id,
                    title=title,
                    url=url,
                    price_yen=None,
                    context_text=line,
                )
            elif existing.title == "Unknown title":
                existing.title = title
            existing.context_text = combine_context(existing.context_text, line)
            continue

        price_line_match = PRICE_LINE_RE.search(line)
        if price_line_match:
            title = normalize_text(price_line_match.group(1)) or "Unknown title"
            price_text = price_line_match.group(2)
            url = price_line_match.group(3)
            listing_id = price_line_match.group(4)
            price = int(price_text.replace(",", ""))

            existing = items.get(listing_id)
            if not existing:
                items[listing_id] = ParsedListing(
                    listing_id=listing_id,
                    title=title,
                    url=url,
                    price_yen=price,
                    context_text=line,
                )
            else:
                if existing.title == "Unknown title" and title != "Unknown title":
                    existing.title = title
                existing.price_yen = price
                existing.context_text = combine_context(existing.context_text, line)
            continue

        url_match = URL_RE.search(line)
        if not url_match:
            continue

        url = url_match.group(1)
        listing_id = url_match.group(2)

        title = "Unknown title"
        image_title_match = IMAGE_TITLE_RE.search(line)
        if image_title_match:
            title = normalize_text(image_title_match.group(1))

        if title == "Unknown title":
            title_part = line.split(url)[0].strip()
            title_part = re.sub(r"(?:現在|即決)\s*[0-9,]+\s*円", "", title_part).strip()
            title = normalize_text(title_part) if title_part else "Unknown title"

        price = parse_yen_amount(line)

        existing = items.get(listing_id)
        if not existing:
            items[listing_id] = ParsedListing(
                listing_id=listing_id,
                title=title,
                url=url,
                price_yen=price,
                context_text=line,
            )
        else:
            if existing.title == "Unknown title" and title != "Unknown title":
                existing.title = title
            elif (
                title != "Unknown title"
                and len(title) < len(existing.title)
                and "[" not in title
                and "!" not in title
            ):
                existing.title = title
            if existing.price_yen is None and price is not None:
                existing.price_yen = price
            existing.context_text = combine_context(existing.context_text, line)

    parsed = list(items.values())
    return parsed[:max_items]


def title_has_keywords(title: str, keywords: Iterable[str]) -> bool:
    title_lower = title.casefold()
    for keyword in keywords:
        if keyword.casefold() not in title_lower:
            return False
    return True


def title_has_any_blacklisted(title: str, keywords: Iterable[str]) -> Optional[str]:
    title_lower = title.casefold()
    for keyword in keywords:
        if keyword.casefold() in title_lower:
            return keyword
    return None


def compute_deal(
    rule: SearchRule,
    listing: ParsedListing,
    market_prices: List[MarketPriceEntry],
    conn: Optional[sqlite3.Connection] = None,
) -> ScoredDeal:
    if listing.price_yen is None:
        raise ValueError("Cannot score a listing without price")

    payment_fee_yen = int(round(listing.price_yen * config.ZENMARKET_PAYMENT_FEE_RATE))
    total_cost_yen = (
        listing.price_yen
        + config.ZENMARKET_SERVICE_FEE_YEN
        + payment_fee_yen
        + config.ESTIMATED_DOMESTIC_SHIPPING_YEN
        + config.ESTIMATED_INTERNATIONAL_SHIPPING_YEN
    )

    total_cost_eur = total_cost_yen / config.EUR_TO_JPY
    vat_eur = total_cost_eur * config.VAT_RATE
    landed_cost_eur = total_cost_eur + vat_eur
    listing_type, listing_type_reason = detect_listing_type(listing.title, listing.context_text, listing.url)
    market_match = match_market_price(listing.title, market_prices)
    alias_match = match_product_alias(listing.title, PRODUCT_ALIASES)

    market_price_eur = 0.0
    max_buy_price_yen = 0
    matched_market_keyword = ""
    market_price_source = ""
    market_price_confidence = SCORE_RELIABILITY_LOW

    if market_match:
        market_price_eur = market_match.market_price_eur
        max_buy_price_yen = market_match.max_buy_price_yen
        matched_market_keyword = market_match.keyword
        market_price_source = market_match.price_source
        market_price_confidence = market_match.confidence

    resolved_price = resolve_market_price_auto(conn, listing.title, alias_match, market_match)
    if resolved_price and resolved_price.market_price_eur > 0:
        market_price_eur = resolved_price.market_price_eur
        market_price_source = resolved_price.source
        market_price_confidence = resolved_price.confidence
        if resolved_price.matched_market_keyword:
            matched_market_keyword = resolved_price.matched_market_keyword

    if market_price_eur > 0:
        safe_resale_eur = market_price_eur * (1.0 - config.SAFETY_MARGIN_RATE)
        profit_eur = safe_resale_eur - landed_cost_eur
        roi_percent = (profit_eur / landed_cost_eur) * 100 if landed_cost_eur > 0 else 0.0
        score = 50.0
        score += max(-35.0, min(35.0, (roi_percent - config.MIN_ROI_PERCENT) * 1.2))
        score += max(-20.0, min(20.0, (profit_eur - config.MIN_PROFIT_EUR) * 0.8))
        score = max(0.0, min(100.0, score))
    else:
        safe_resale_eur = 0.0
        profit_eur = 0.0
        roi_percent = 0.0
        score = 0.0

    return ScoredDeal(
        listing_id=listing.listing_id,
        title=listing.title,
        url=listing.url,
        query=rule.query,
        rule_name=rule.name,
        price_yen=listing.price_yen,
        market_price_eur=round(market_price_eur, 2),
        max_buy_price_yen=max_buy_price_yen,
        total_cost_yen=total_cost_yen,
        total_cost_eur=round(total_cost_eur, 2),
        vat_eur=round(vat_eur, 2),
        landed_cost_eur=round(landed_cost_eur, 2),
        safe_resale_eur=round(safe_resale_eur, 2),
        profit_eur=round(profit_eur, 2),
        roi_percent=round(roi_percent, 2),
        score=round(score, 2),
        listing_type=listing_type,
        listing_type_reason=listing_type_reason,
        matched_market_keyword=matched_market_keyword,
        market_price_source=market_price_source,
        market_price_confidence=market_price_confidence,
        detected_at=dt.datetime.now().isoformat(timespec="seconds"),
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            query TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            price_yen INTEGER NOT NULL,
            market_price_eur REAL NOT NULL,
            max_buy_price_yen INTEGER NOT NULL DEFAULT 0,
            total_cost_yen INTEGER NOT NULL,
            total_cost_eur REAL NOT NULL,
            vat_eur REAL NOT NULL,
            landed_cost_eur REAL NOT NULL,
            safe_resale_eur REAL NOT NULL,
            profit_eur REAL NOT NULL,
            roi_percent REAL NOT NULL,
            score REAL NOT NULL,
            listing_type TEXT NOT NULL DEFAULT 'UNKNOWN',
            listing_type_reason TEXT NOT NULL DEFAULT '',
            matched_market_keyword TEXT NOT NULL DEFAULT '',
            market_price_source TEXT NOT NULL DEFAULT '',
            market_price_confidence TEXT NOT NULL DEFAULT 'LOW',
            auto_price_used TEXT NOT NULL DEFAULT 'False',
            auto_price_source TEXT NOT NULL DEFAULT '',
            auto_price_sample_size INTEGER NOT NULL DEFAULT 0,
            auto_price_raw_summary TEXT NOT NULL DEFAULT '',
            auto_price_last_checked TEXT NOT NULL DEFAULT '',
            detected_at TEXT NOT NULL
        )
        """
    )
    _ensure_db_column(conn, "deals", "listing_type", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
    _ensure_db_column(conn, "deals", "max_buy_price_yen", "INTEGER NOT NULL DEFAULT 0")
    _ensure_db_column(conn, "deals", "listing_type_reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "matched_market_keyword", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "market_price_source", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "market_price_confidence", "TEXT NOT NULL DEFAULT 'LOW'")
    _ensure_db_column(conn, "deals", "auto_price_used", "TEXT NOT NULL DEFAULT 'False'")
    _ensure_db_column(conn, "deals", "auto_price_source", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "auto_price_sample_size", "INTEGER NOT NULL DEFAULT 0")
    _ensure_db_column(conn, "deals", "auto_price_raw_summary", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "auto_price_last_checked", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_price_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            source TEXT NOT NULL,
            market_price_eur REAL NOT NULL,
            currency TEXT NOT NULL,
            sample_size INTEGER NOT NULL DEFAULT 0,
            confidence TEXT NOT NULL DEFAULT 'LOW',
            raw_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _ensure_db_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def save_if_new(conn: sqlite3.Connection, deal: ScoredDeal) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO deals (
                listing_id, title, url, query, rule_name, price_yen,
                market_price_eur, max_buy_price_yen, total_cost_yen, total_cost_eur, vat_eur,
                landed_cost_eur, safe_resale_eur, profit_eur, roi_percent,
                score, listing_type, listing_type_reason, matched_market_keyword,
                market_price_source, market_price_confidence, auto_price_used,
                auto_price_source, auto_price_sample_size, auto_price_raw_summary,
                auto_price_last_checked, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal.listing_id,
                deal.title,
                deal.url,
                deal.query,
                deal.rule_name,
                deal.price_yen,
                deal.market_price_eur,
                deal.max_buy_price_yen,
                deal.total_cost_yen,
                deal.total_cost_eur,
                deal.vat_eur,
                deal.landed_cost_eur,
                deal.safe_resale_eur,
                deal.profit_eur,
                deal.roi_percent,
                deal.score,
                deal.listing_type,
                deal.listing_type_reason,
                deal.matched_market_keyword,
                deal.market_price_source,
                deal.market_price_confidence,
                "False",
                "",
                0,
                "",
                "",
                deal.detected_at,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def fetch_all_deals(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    cursor = conn.execute(
        f"SELECT {', '.join(DB_EXPORT_COLUMNS)} FROM deals ORDER BY score DESC, detected_at DESC"
    )
    rows = cursor.fetchall()
    return [dict(zip(DB_EXPORT_COLUMNS, row)) for row in rows]


def export_csv(
    deals: List[Dict[str, object]],
    csv_path: str,
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> int:
    csv_headers = [
        "listing_id",
        "title",
        "url",
        "query",
        "rule_name",
        "data_source",
        "price_yen",
        "current_price_yen",
        "buy_now_price_yen",
        "bid_count",
        "time_left",
        "seller_name",
        "seller_rating",
        "shipping_japan",
        "market_price_eur",
        "max_buy_price_yen",
        "total_cost_yen",
        "total_cost_eur",
        "vat_eur",
        "landed_cost_eur",
        "safe_resale_eur",
        "profit_eur",
        "roi_percent",
        "score",
        "decision",
        "manual_action_needed",
        "listing_type",
        "listing_type_reason",
        "matched_market_keyword",
        "market_price_source",
        "market_price_confidence",
        "manual_market_price_eur",
        "manual_price_source",
        "manual_price_confidence",
        "manual_status",
        "auto_price_used",
        "auto_price_source",
        "auto_price_sample_size",
        "auto_price_raw_summary",
        "auto_price_last_checked",
        "matched_product_japanese",
        "search_name_fr",
        "search_name_en",
        "cardmarket_query",
        "ebay_query",
        "pricecharting_query",
        "cardmarket_search_url",
        "ebay_sold_search_url",
        "pricecharting_search_url",
        "score_reliability",
        "deal_quality_score",
        "risk_flags",
        "detected_at",
        "link_for_zenmarket",
        "auction_warning",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(csv_headers)
        for deal in deals:
            row = build_sheet_row(
                deal,
                conn=conn,
                existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
            )
            writer.writerow(
                [
                    deal.get("listing_id", ""),
                    deal.get("title", ""),
                    deal.get("url", ""),
                    deal.get("query", ""),
                    deal.get("rule_name", ""),
                    row.get("data_source", ""),
                    row.get("price_yen", ""),
                    row.get("current_price_yen", ""),
                    row.get("buy_now_price_yen", ""),
                    row.get("bid_count", ""),
                    row.get("time_left", ""),
                    row.get("seller_name", ""),
                    row.get("seller_rating", ""),
                    row.get("shipping_japan", ""),
                    row.get("market_price_eur", ""),
                    row.get("max_buy_price_yen", ""),
                    row.get("total_cost_yen", ""),
                    row.get("total_cost_eur", ""),
                    row.get("vat_eur", ""),
                    row.get("landed_cost_eur", ""),
                    row.get("safe_resale_eur", ""),
                    row.get("profit_eur", ""),
                    row.get("roi_percent", ""),
                    row.get("score", ""),
                    row.get("decision", ""),
                    row.get("manual_action_needed", ""),
                    row.get("listing_type", ""),
                    row.get("listing_type_reason", ""),
                    row.get("matched_market_keyword", ""),
                    row.get("market_price_source", ""),
                    row.get("market_price_confidence", ""),
                    row.get("manual_market_price_eur", ""),
                    row.get("manual_price_source", ""),
                    row.get("manual_price_confidence", ""),
                    row.get("manual_status", ""),
                    row.get("auto_price_used", ""),
                    row.get("auto_price_source", ""),
                    row.get("auto_price_sample_size", ""),
                    row.get("auto_price_raw_summary", ""),
                    row.get("auto_price_last_checked", ""),
                    row.get("matched_product_japanese", ""),
                    row.get("search_name_fr", ""),
                    row.get("search_name_en", ""),
                    row.get("cardmarket_query", ""),
                    row.get("ebay_query", ""),
                    row.get("pricecharting_query", ""),
                    row.get("cardmarket_search_url", ""),
                    row.get("ebay_sold_search_url", ""),
                    row.get("pricecharting_search_url", ""),
                    row.get("score_reliability", ""),
                    row.get("deal_quality_score", ""),
                    row.get("risk_flags", ""),
                    deal.get("detected_at", ""),
                    row.get("link_for_zenmarket", ""),
                    row.get("auction_warning", ""),
                ]
            )

    return len(deals)


def merge_preserved_sheet_values(
    primary: Dict[str, Dict[str, str]],
    secondary: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {url: dict(values) for url, values in primary.items()}
    for url, values in secondary.items():
        current = merged.setdefault(url, {})
        for field in PRESERVED_SHEET_FIELDS:
            if not current.get(field) and values.get(field):
                current[field] = values[field]
    return merged


def merge_manual_values_by_priority(*sources: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {}
    for source in sources:
        for url, values in source.items():
            current = merged.setdefault(url, {})
            for field in PRESERVED_SHEET_FIELDS:
                incoming = values.get(field, "")
                if incoming and not current.get(field):
                    current[field] = incoming
    return merged


def get_cache_query_candidates(
    title: str,
    alias_match: Optional[ProductAliasEntry],
    matched_market_keyword: str = "",
) -> List[str]:
    candidates: List[str] = []
    for value in (
        matched_market_keyword,
        alias_match.japanese_keyword if alias_match else "",
        alias_match.pricecharting_query if alias_match else "",
        alias_match.ebay_query if alias_match else "",
        alias_match.search_name_en if alias_match else "",
        title,
    ):
        cleaned = (value or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


def get_cached_market_price(conn: sqlite3.Connection, queries: Sequence[str]) -> Optional[ResolvedMarketPrice]:
    if not queries:
        return None

    cache_cutoff = dt.datetime.utcnow() - dt.timedelta(days=config.AUTO_PRICE_CACHE_DAYS)
    cutoff_iso = cache_cutoff.isoformat(timespec="seconds")

    best_row: Optional[sqlite3.Row] = None
    conn.row_factory = sqlite3.Row
    for query in queries:
        row = conn.execute(
            """
            SELECT query, source, market_price_eur, currency, sample_size, confidence, raw_summary, created_at
            FROM market_price_cache
            WHERE query = ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (query, cutoff_iso),
        ).fetchone()
        if not row:
            continue
        if best_row is None or CONFIDENCE_RANK.get(str(row["confidence"]).upper(), 0) > CONFIDENCE_RANK.get(
            str(best_row["confidence"]).upper(), 0
        ):
            best_row = row

    if not best_row:
        return None

    return ResolvedMarketPrice(
        market_price_eur=to_float(best_row["market_price_eur"]),
        source=str(best_row["source"]),
        confidence=str(best_row["confidence"]).upper() or SCORE_RELIABILITY_LOW,
        sample_size=int(to_float(best_row["sample_size"])),
        raw_summary=str(best_row["raw_summary"]),
        matched_market_keyword="",
        currency=str(best_row["currency"] or "EUR"),
        auto_price_used=True,
        auto_price_source=str(best_row["source"]),
        auto_price_sample_size=int(to_float(best_row["sample_size"])),
        auto_price_raw_summary=str(best_row["raw_summary"]),
        auto_price_last_checked=str(best_row["created_at"]),
    )


def save_market_price_cache(conn: sqlite3.Connection, query: str, result: ResolvedMarketPrice) -> None:
    if not query or result.market_price_eur <= 0:
        return
    conn.execute(
        """
        INSERT INTO market_price_cache (
            query, source, market_price_eur, currency, sample_size, confidence, raw_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query,
            result.source,
            result.market_price_eur,
            result.currency or "EUR",
            result.sample_size,
            result.confidence,
            result.raw_summary,
            utc_now_iso(),
        ),
    )
    conn.commit()


def resolve_price_pricecharting(query: str) -> Optional[ResolvedMarketPrice]:
    if not config.PRICECHARTING_ENABLED or not config.PRICECHARTING_API_TOKEN or not query:
        return None
    logging.info("PriceCharting resolver configured but no API integration is executed automatically yet for query: %s", query)
    return None


def resolve_price_apify_ebay_sold(query: str) -> Optional[ResolvedMarketPrice]:
    if (
        not config.APIFY_EBAY_SOLD_ENABLED
        or not config.APIFY_API_TOKEN
        or not config.APIFY_EBAY_SOLD_ACTOR_ID
        or not query
    ):
        return None
    logging.info("Apify eBay sold resolver configured but no API integration is executed automatically yet for query: %s", query)
    return None


def resolve_price_ebay_browse_active(query: str) -> Optional[ResolvedMarketPrice]:
    if not config.EBAY_BROWSE_ENABLED or not config.EBAY_CLIENT_ID or not config.EBAY_CLIENT_SECRET or not query:
        return None
    logging.info("eBay Browse resolver configured but no API integration is executed automatically yet for query: %s", query)
    return None


def resolve_market_price_auto(
    conn: Optional[sqlite3.Connection],
    title: str,
    alias_match: Optional[ProductAliasEntry],
    csv_market_match: Optional[MarketPriceEntry],
) -> Optional[ResolvedMarketPrice]:
    if csv_market_match and csv_market_match.market_price_eur > 0 and confidence_at_least(
        csv_market_match.confidence, config.AUTO_PRICE_MIN_CONFIDENCE
    ):
        return ResolvedMarketPrice(
            market_price_eur=csv_market_match.market_price_eur,
            source=csv_market_match.price_source or "market_prices.csv",
            confidence=csv_market_match.confidence,
            sample_size=0,
            raw_summary=f"Matched market_prices.csv keyword '{csv_market_match.keyword}'.",
            matched_market_keyword=csv_market_match.keyword,
            auto_price_used=False,
            auto_price_source="",
            auto_price_sample_size=0,
            auto_price_raw_summary="",
            auto_price_last_checked="",
        )

    cache_queries = get_cache_query_candidates(
        title=title,
        alias_match=alias_match,
        matched_market_keyword=csv_market_match.keyword if csv_market_match else "",
    )
    if conn is not None:
        cached = get_cached_market_price(conn, cache_queries)
        if cached and confidence_at_least(cached.confidence, config.AUTO_PRICE_MIN_CONFIDENCE):
            return cached

    if not config.AUTO_PRICE_ENABLED:
        return None

    source_query_map = {
        "pricecharting": (alias_match.pricecharting_query if alias_match else "") or (alias_match.search_name_en if alias_match else "") or title,
        "apify_ebay_sold": (alias_match.ebay_query if alias_match else "") or (alias_match.search_name_en if alias_match else "") or title,
        "ebay_browse_active": (alias_match.ebay_query if alias_match else "") or (alias_match.search_name_en if alias_match else "") or title,
    }
    resolvers = {
        "pricecharting": resolve_price_pricecharting,
        "apify_ebay_sold": resolve_price_apify_ebay_sold,
        "ebay_browse_active": resolve_price_ebay_browse_active,
    }

    for source_name in config.PRICE_SOURCES_PRIORITY:
        resolver = resolvers.get(source_name)
        query = source_query_map.get(source_name, "").strip()
        if not resolver or not query:
            continue
        try:
            result = resolver(query)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Auto price resolver failed for source %s and query '%s': %s", source_name, query, exc)
            continue
        if not result or result.market_price_eur <= 0:
            continue
        result.auto_price_used = True
        result.auto_price_source = result.source
        result.auto_price_sample_size = result.sample_size
        result.auto_price_raw_summary = result.raw_summary
        result.auto_price_last_checked = utc_now_iso()
        if conn is not None:
            save_market_price_cache(conn, query, result)
        if confidence_at_least(result.confidence, config.AUTO_PRICE_MIN_CONFIDENCE):
            return result

    return None


def should_alert_telegram(deal: ScoredDeal) -> bool:
    return (
        get_decision(
            profit_eur=deal.profit_eur,
            roi_percent=deal.roi_percent,
            listing_type=deal.listing_type,
            market_price_eur=deal.market_price_eur,
            market_price_confidence=deal.market_price_confidence,
            max_buy_price_yen=deal.max_buy_price_yen,
            price_yen=deal.price_yen,
        )
        == DECISION_BUY_ALERT
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pokemon-watch-bot")
    parser.add_argument(
        "--best",
        action="store_true",
        help="Show only best deals (BUY ALERT + WATCH) in terminal and Google Sheets.",
    )
    parser.add_argument(
        "--needs-price-report",
        action="store_true",
        help="Generate NEEDS_PRICE analysis reports from stored deals without modifying market_prices.csv.",
    )
    parser.add_argument(
        "--auto-price-test",
        action="store_true",
        help="Test automatic market price resolution on 5 NEEDS_PRICE listings without writing to Google Sheets.",
    )
    return parser.parse_args()


def is_good_deal(profit_eur: float, roi_percent: float) -> bool:
    return profit_eur >= config.MIN_PROFIT_EUR and roi_percent >= config.MIN_ROI_PERCENT


def get_decision(
    profit_eur: float,
    roi_percent: float,
    listing_type: str,
    market_price_eur: float,
    market_price_confidence: str,
    max_buy_price_yen: int,
    price_yen: int,
    manual_status: str = "",
    manual_price_used: bool = False,
) -> str:
    if manual_status == MANUAL_STATUS_IGNORE:
        return DECISION_IGNORE

    if market_price_eur <= 0:
        return DECISION_NEEDS_PRICE

    good = is_good_deal(profit_eur, roi_percent)

    if listing_type == LISTING_TYPE_LOW_START_AUCTION:
        return DECISION_WATCH_LOW_AUCTION

    if listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE):
        if (
            good
            and market_price_confidence in (SCORE_RELIABILITY_HIGH, SCORE_RELIABILITY_MEDIUM)
            and (max_buy_price_yen <= 0 or price_yen <= max_buy_price_yen)
        ):
            return DECISION_BUY_ALERT
        if manual_price_used:
            if profit_eur > 0:
                return DECISION_WATCH
            return DECISION_SKIP
        if profit_eur > 0 or roi_percent > 0:
            return DECISION_WATCH
        return DECISION_SKIP

    if listing_type == LISTING_TYPE_AUCTION:
        if manual_price_used:
            if good:
                return DECISION_WATCH_AUCTION
            return DECISION_SKIP
        if good:
            return DECISION_WATCH_AUCTION
        return DECISION_SKIP

    if listing_type == LISTING_TYPE_UNKNOWN:
        if manual_price_used:
            if profit_eur > 0:
                return DECISION_WATCH
            return DECISION_SKIP
        if good:
            return DECISION_WATCH
        return DECISION_SKIP

    if manual_price_used:
        if profit_eur > 0:
            return DECISION_WATCH
        return DECISION_SKIP
    if good:
        return DECISION_WATCH
    return DECISION_SKIP


def decision_rank(decision: str) -> int:
    if decision == DECISION_BUY_ALERT:
        return 0
    if decision == DECISION_WATCH:
        return 1
    if decision == DECISION_WATCH_AUCTION:
        return 2
    if decision == DECISION_WATCH_LOW_AUCTION:
        return 3
    if decision == DECISION_NEEDS_PRICE:
        return 4
    if decision == DECISION_IGNORE:
        return 6
    return 5


def listing_type_rank(listing_type: str) -> int:
    if listing_type == LISTING_TYPE_BUY_NOW:
        return 0
    if listing_type == LISTING_TYPE_FIXED_PRICE:
        return 1
    if listing_type == LISTING_TYPE_AUCTION:
        return 2
    if listing_type == LISTING_TYPE_LOW_START_AUCTION:
        return 3
    if listing_type == LISTING_TYPE_UNKNOWN:
        return 4
    return 5


def build_sheet_row(
    deal: Dict[str, object],
    existing_notes: str = "",
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_values: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    url = str(deal.get("url", "")).strip()
    title = str(deal.get("title", ""))
    context_text = str(deal.get("context_text", ""))
    listing_type, listing_type_reason = detect_listing_type(title, context_text, url)
    market_match = match_market_price(title, MARKET_PRICES)
    alias_match = match_product_alias(title, PRODUCT_ALIASES)

    # We intentionally recompute these market fields from market_prices.csv only.
    # Old DB rows can contain legacy generic estimates that we no longer trust.
    market_price_eur = 0.0
    max_buy_price_yen = 0
    matched_market_keyword = ""
    market_price_source = ""
    market_price_confidence = SCORE_RELIABILITY_LOW

    if market_match:
        market_price_eur = market_match.market_price_eur
        max_buy_price_yen = market_match.max_buy_price_yen
        matched_market_keyword = market_match.keyword
        market_price_source = market_match.price_source
        market_price_confidence = market_match.confidence

    resolved_price = resolve_market_price_auto(conn, title, alias_match, market_match)
    auto_price_used = "False"
    auto_price_source = ""
    auto_price_sample_size = ""
    auto_price_raw_summary = ""
    auto_price_last_checked = ""
    existing_sheet_values = existing_sheet_values or {}

    manual_market_price_eur = to_float(existing_sheet_values.get("manual_market_price_eur", ""))
    manual_price_source = normalize_manual_source(existing_sheet_values.get("manual_price_source", ""))
    manual_price_confidence = normalize_confidence(
        existing_sheet_values.get("manual_price_confidence", ""),
        default=SCORE_RELIABILITY_MEDIUM,
    )
    manual_status = normalize_manual_status(existing_sheet_values.get("manual_status", ""))

    manual_price_used = manual_market_price_eur > 0
    if manual_price_used:
        market_price_eur = manual_market_price_eur
        market_price_source = manual_price_source or MANUAL_SOURCE_DEFAULT
        market_price_confidence = manual_price_confidence or SCORE_RELIABILITY_MEDIUM
        matched_market_keyword = MANUAL_SOURCE_DEFAULT
        auto_price_used = "False"
        auto_price_source = ""
        auto_price_sample_size = ""
        auto_price_raw_summary = ""
        auto_price_last_checked = ""
    elif resolved_price and resolved_price.market_price_eur > 0:
        market_price_eur = resolved_price.market_price_eur
        market_price_source = resolved_price.source
        market_price_confidence = resolved_price.confidence
        if resolved_price.matched_market_keyword:
            matched_market_keyword = resolved_price.matched_market_keyword
        auto_price_used = "True" if resolved_price.auto_price_used else "False"
        auto_price_source = resolved_price.auto_price_source or resolved_price.source
        auto_price_sample_size = str(resolved_price.auto_price_sample_size or resolved_price.sample_size or "")
        auto_price_raw_summary = resolved_price.auto_price_raw_summary or resolved_price.raw_summary
        auto_price_last_checked = resolved_price.auto_price_last_checked

    matched_product_japanese = alias_match.japanese_keyword if alias_match else ""
    search_name_fr = alias_match.search_name_fr if alias_match else ""
    search_name_en = alias_match.search_name_en if alias_match else ""
    cardmarket_query = alias_match.cardmarket_query if alias_match else ""
    ebay_query = alias_match.ebay_query if alias_match else ""
    pricecharting_query = alias_match.pricecharting_query if alias_match else ""

    fallback_query = (
        cardmarket_query
        or ebay_query
        or pricecharting_query
        or matched_product_japanese
        or matched_market_keyword
        or title
    )
    if not cardmarket_query:
        cardmarket_query = fallback_query
    if not ebay_query:
        ebay_query = fallback_query
    if not pricecharting_query:
        pricecharting_query = fallback_query

    price_yen = int(to_float(deal.get("price_yen", 0)))
    listing_metadata = extract_listing_metadata(title, context_text, price_yen)
    total_cost_yen = (
        price_yen
        + config.ZENMARKET_SERVICE_FEE_YEN
        + int(round(price_yen * config.ZENMARKET_PAYMENT_FEE_RATE))
        + config.ESTIMATED_DOMESTIC_SHIPPING_YEN
        + config.ESTIMATED_INTERNATIONAL_SHIPPING_YEN
    )
    total_cost_eur = round(total_cost_yen / config.EUR_TO_JPY, 2)
    vat_eur = round(total_cost_eur * config.VAT_RATE, 2)
    landed_cost_eur = round(total_cost_eur + vat_eur, 2)

    if market_price_eur > 0:
        if manual_price_used:
            safe_resale_eur = round(market_price_eur, 2)
            profit_eur = round(market_price_eur - total_cost_eur, 2)
            roi_percent = round((profit_eur / total_cost_eur) * 100, 2) if total_cost_eur > 0 else 0.0
        else:
            safe_resale_eur = round(market_price_eur * (1.0 - config.SAFETY_MARGIN_RATE), 2)
            profit_eur = round(safe_resale_eur - landed_cost_eur, 2)
            roi_percent = round((profit_eur / landed_cost_eur) * 100, 2) if landed_cost_eur > 0 else 0.0
        score = 50.0
        score += max(-35.0, min(35.0, (roi_percent - config.MIN_ROI_PERCENT) * 1.2))
        score += max(-20.0, min(20.0, (profit_eur - config.MIN_PROFIT_EUR) * 0.8))
        score = round(max(0.0, min(100.0, score)), 2)
    else:
        safe_resale_eur = 0.0
        profit_eur = 0.0
        roi_percent = 0.0
        score = 0.0

    decision = get_decision(
        profit_eur=profit_eur,
        roi_percent=roi_percent,
        listing_type=listing_type,
        market_price_eur=market_price_eur,
        market_price_confidence=market_price_confidence,
        max_buy_price_yen=max_buy_price_yen,
        price_yen=price_yen,
        manual_status=manual_status,
        manual_price_used=manual_price_used,
    )
    score_reliability = get_score_reliability(listing_type, market_price_confidence, decision)
    manual_action_needed = compute_manual_action_needed(
        manual_market_price_eur=manual_market_price_eur,
        manual_status=manual_status,
    )
    risk_flags = build_risk_flags(
        title=title,
        context_text=context_text,
        listing_type=listing_type,
        decision=decision,
        manual_market_price_eur=manual_market_price_eur,
        seller_rating=listing_metadata.get("seller_rating", ""),
    )
    deal_quality_score = compute_deal_quality_score(
        listing_type=listing_type,
        matched_product_japanese=matched_product_japanese,
        manual_market_price_eur=manual_market_price_eur,
        title=title,
        risk_flags=risk_flags,
        decision=decision,
    )
    return {
        "created_at": str(deal.get("detected_at", "")),
        "source": "Yahoo Auctions Japan",
        "data_source": "Yahoo Auctions Japan",
        "query": str(deal.get("query", "")),
        "rule_name": str(deal.get("rule_name", "")),
        "listing_type": listing_type,
        "listing_type_reason": listing_type_reason,
        "decision": decision,
        "manual_action_needed": manual_action_needed,
        "title": title,
        "price_yen": str(price_yen or ""),
        "current_price_yen": listing_metadata.get("current_price_yen", ""),
        "buy_now_price_yen": listing_metadata.get("buy_now_price_yen", ""),
        "bid_count": listing_metadata.get("bid_count", ""),
        "time_left": listing_metadata.get("time_left", ""),
        "seller_name": listing_metadata.get("seller_name", ""),
        "seller_rating": listing_metadata.get("seller_rating", ""),
        "shipping_japan": listing_metadata.get("shipping_japan", ""),
        "market_price_eur": str(round(market_price_eur, 2) if market_price_eur > 0 else ""),
        "max_buy_price_yen": str(max_buy_price_yen or ""),
        "total_cost_yen": str(total_cost_yen or ""),
        "total_cost_eur": str(total_cost_eur if total_cost_eur else ""),
        "vat_eur": str(vat_eur if vat_eur else ""),
        "landed_cost_eur": str(landed_cost_eur if landed_cost_eur else ""),
        "safe_resale_eur": str(safe_resale_eur if safe_resale_eur else ""),
        "manual_market_price_eur": str(round(manual_market_price_eur, 2) if manual_market_price_eur > 0 else ""),
        "profit_eur": str(profit_eur if market_price_eur > 0 else ""),
        "roi_percent": str(roi_percent if market_price_eur > 0 else ""),
        "score": str(score if market_price_eur > 0 else ""),
        "matched_market_keyword": matched_market_keyword,
        "market_price_source": market_price_source,
        "market_price_confidence": market_price_confidence,
        "manual_price_source": manual_price_source,
        "manual_price_confidence": manual_price_confidence if manual_market_price_eur > 0 or manual_status else "",
        "manual_status": manual_status,
        "auto_price_used": auto_price_used,
        "auto_price_source": auto_price_source,
        "auto_price_sample_size": auto_price_sample_size,
        "auto_price_raw_summary": auto_price_raw_summary,
        "auto_price_last_checked": auto_price_last_checked,
        "matched_product_japanese": matched_product_japanese,
        "search_name_fr": search_name_fr,
        "search_name_en": search_name_en,
        "cardmarket_query": cardmarket_query,
        "ebay_query": ebay_query,
        "pricecharting_query": pricecharting_query,
        "cardmarket_search_url": build_cardmarket_search_url(cardmarket_query),
        "ebay_sold_search_url": build_ebay_sold_search_url(ebay_query),
        "pricecharting_search_url": build_pricecharting_search_url(pricecharting_query),
        "url": url,
        "link_for_zenmarket": build_zenmarket_auction_url(url),
        "auction_warning": get_auction_warning(title, context_text),
        "risk_flags": " | ".join(risk_flags),
        "score_reliability": score_reliability,
        "deal_quality_score": str(deal_quality_score),
        "notes": existing_sheet_values.get("notes", "") or existing_notes or "",
    }


def sort_rows_for_watch(rows: List[Dict[str, str]], hide_skip: bool, prefer_buy_now: bool) -> List[Dict[str, str]]:
    filtered = rows
    if hide_skip:
        filtered = [row for row in rows if row.get("decision") not in (DECISION_SKIP, DECISION_IGNORE)]

    def sort_key(row: Dict[str, str]) -> tuple:
        decision_part = decision_rank(row.get("decision", DECISION_SKIP))
        roi_part = -to_float(row.get("roi_percent", 0.0))
        profit_part = -to_float(row.get("profit_eur", 0.0))
        if prefer_buy_now:
            type_part = listing_type_rank(row.get("listing_type", LISTING_TYPE_UNKNOWN))
            return (decision_part, type_part, roi_part, profit_part)
        return (decision_part, roi_part, profit_part)

    filtered.sort(key=sort_key)
    return filtered


def sort_deals_rows(rows: List[Dict[str, str]], prefer_buy_now: bool) -> List[Dict[str, str]]:
    sorted_rows = list(rows)

    def sort_key(row: Dict[str, str]) -> tuple:
        decision_part = decision_rank(row.get("decision", DECISION_SKIP))
        type_part = listing_type_rank(row.get("listing_type", LISTING_TYPE_UNKNOWN)) if prefer_buy_now else 99
        roi_part = -to_float(row.get("roi_percent", 0.0))
        profit_part = -to_float(row.get("profit_eur", 0.0))
        return (type_part, decision_part, roi_part, profit_part)

    sorted_rows.sort(key=sort_key)
    return sorted_rows


def sort_best_deals_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    best_rows = [
        row
        for row in rows
        if row.get("decision") in (
            DECISION_BUY_ALERT,
            DECISION_WATCH,
            DECISION_WATCH_AUCTION,
            DECISION_WATCH_LOW_AUCTION,
            DECISION_NEEDS_PRICE,
        )
    ]
    return sort_rows_for_watch(best_rows, hide_skip=True, prefer_buy_now=True)


def sort_buy_now_deals_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    filtered = [
        row
        for row in rows
        if row.get("listing_type") in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE)
        and row.get("decision") in (DECISION_BUY_ALERT, DECISION_WATCH, DECISION_NEEDS_PRICE)
    ]

    def sort_key(row: Dict[str, str]) -> tuple:
        return (
            decision_rank(row.get("decision", DECISION_SKIP)),
            -to_float(row.get("deal_quality_score", 0.0)),
            -to_float(row.get("roi_percent", 0.0)),
            -to_float(row.get("profit_eur", 0.0)),
        )

    filtered.sort(key=sort_key)
    return filtered


def sort_auctions_watch_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    filtered = [
        row
        for row in rows
        if row.get("decision") in (DECISION_WATCH_AUCTION, DECISION_WATCH_LOW_AUCTION)
    ]

    def time_left_key(value: str) -> tuple:
        if not value:
            return (1, "")
        return (0, value)

    def sort_key(row: Dict[str, str]) -> tuple:
        return (
            decision_rank(row.get("decision", DECISION_SKIP)),
            time_left_key(row.get("time_left", "")),
            -to_float(row.get("deal_quality_score", 0.0)),
            -to_float(row.get("roi_percent", 0.0)),
        )

    filtered.sort(key=sort_key)
    return filtered


def sort_needs_price_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    filtered = [row for row in rows if row.get("decision") == DECISION_NEEDS_PRICE]

    def sort_key(row: Dict[str, str]) -> tuple:
        return (
            listing_type_rank(row.get("listing_type", LISTING_TYPE_UNKNOWN)),
            -to_float(row.get("deal_quality_score", 0.0)),
            -to_float(row.get("roi_percent", 0.0)),
            -to_float(row.get("profit_eur", 0.0)),
        )

    filtered.sort(key=sort_key)
    return filtered


def determine_candidate_metadata(keyword_candidate: str, example_title: str = "") -> Tuple[str, str, str]:
    candidate = keyword_candidate or ""
    haystack = combine_context(candidate, example_title)

    for alias in PRODUCT_ALIASES:
        if alias.japanese_keyword == candidate:
            return (alias.category or "sealed", "manual_to_verify", "HIGH")
    for market_entry in MARKET_PRICES:
        if market_entry.keyword == candidate:
            return (
                market_entry.category or "unknown_or_suspect",
                market_entry.price_source or "manual",
                market_entry.confidence or "LOW",
            )

    if any(token in haystack for token in ("引退品", "まとめ売り", "大量")):
        return ("lot", "manual", "LOW")
    if any(token in haystack for token in ("PSA10", "PSA9", "ARS10", "鑑定")):
        return ("graded", "manual", "MEDIUM")
    if "旧裏" in haystack:
        return ("old_series", "manual_to_verify", "MEDIUM")
    if any(token in haystack for token in ("BOX", "未開封", "シュリンク", "スペシャルBOX")):
        return ("sealed", "manual_to_verify", "HIGH")
    if any(token in haystack for token in ("SAR", "SR", "キラ", "ホロ", "ミラー", "プロモ")):
        return ("singles_or_hits", "manual_to_verify", "MEDIUM")
    if candidate in ("UNKNOWN_PRODUCT", "BOX_UNKNOWN"):
        return ("unknown_or_suspect", "manual", "LOW")
    return ("unknown_or_suspect", "manual", "LOW")


def derive_keyword_candidate(row: Dict[str, str], title: str) -> str:
    title_text = title or ""
    if row.get("matched_product_japanese"):
        return row["matched_product_japanese"]
    if row.get("matched_market_keyword"):
        return row["matched_market_keyword"]

    candidates = [entry.japanese_keyword for entry in PRODUCT_ALIASES] + COMMON_REPORT_KEYWORDS
    for candidate in sorted(set(candidates), key=len, reverse=True):
        if candidate and candidate in title_text:
            return candidate

    if "BOX" in title_text:
        return "BOX_UNKNOWN"
    return "UNKNOWN_PRODUCT"


def collect_needs_price_rows(
    deals: Sequence[Dict[str, object]],
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]
    return [row for row in rows if row.get("decision") == DECISION_NEEDS_PRICE]


def generate_needs_price_reports(
    deals: Sequence[Dict[str, object]],
    report_path: str = "needs_price_report.csv",
    suggestions_path: str = "market_prices_suggestions.csv",
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, object]:
    needs_rows = collect_needs_price_rows(deals, conn=conn, existing_sheet_data=existing_sheet_data)

    grouped: Dict[str, Dict[str, object]] = {}
    token_counts: Counter = Counter()
    difficult_titles: List[str] = []

    for row in needs_rows:
        title = row.get("title", "")
        candidate = derive_keyword_candidate(row, title)
        category, price_source, priority = determine_candidate_metadata(candidate, title)

        token_counts[candidate] += 1
        bucket = grouped.setdefault(
            candidate,
            {
                "count": 0,
                "example_titles": [],
                "suggested_category": category,
                "suggested_price_source": price_source,
                "priority": priority,
            },
        )
        bucket["count"] += 1
        example_titles = bucket["example_titles"]
        if title and title not in example_titles and len(example_titles) < 3:
            example_titles.append(title)

        if category in ("unknown_or_suspect", "lot") and len(difficult_titles) < 10:
            difficult_titles.append(title)

    report_rows = sorted(
        grouped.items(),
        key=lambda item: (-int(item[1]["count"]), str(item[0])),
    )

    with open(report_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "keyword_candidate",
                "count",
                "example_titles",
                "suggested_category",
                "suggested_price_source",
                "priority",
            ]
        )
        for keyword_candidate, data in report_rows:
            writer.writerow(
                [
                    keyword_candidate,
                    data["count"],
                    " || ".join(data["example_titles"]),
                    data["suggested_category"],
                    data["suggested_price_source"],
                    data["priority"],
                ]
            )

    existing_market_keywords = {entry.keyword for entry in MARKET_PRICES}
    suggestion_rows: List[List[object]] = []
    for keyword_candidate, data in report_rows:
        if keyword_candidate in GENERIC_SUGGESTION_SKIP:
            continue
        if keyword_candidate in existing_market_keywords:
            continue
        reason = (
            f"Observed {data['count']} NEEDS_PRICE listing(s); examples: "
            + " | ".join(data["example_titles"][:2])
        )
        suggestion_rows.append(
            [
                keyword_candidate,
                0,
                0,
                data["suggested_category"],
                data["suggested_price_source"],
                "LOW",
                reason,
            ]
        )
        if len(suggestion_rows) >= 40:
            break

    if len(suggestion_rows) < 20:
        existing_suggestion_keywords = {str(row[0]) for row in suggestion_rows}
        fallback_candidates = [entry.japanese_keyword for entry in PRODUCT_ALIASES] + COMMON_REPORT_KEYWORDS
        for keyword_candidate in fallback_candidates:
            if keyword_candidate in GENERIC_SUGGESTION_SKIP:
                continue
            if keyword_candidate in existing_market_keywords or keyword_candidate in existing_suggestion_keywords:
                continue
            category, price_source, _priority = determine_candidate_metadata(keyword_candidate)
            suggestion_rows.append(
                [
                    keyword_candidate,
                    0,
                    0,
                    category,
                    price_source,
                    "LOW",
                    "Fallback keyword to verify manually before adding trusted market pricing.",
                ]
            )
            existing_suggestion_keywords.add(keyword_candidate)
            if len(suggestion_rows) >= 20:
                break

    with open(suggestions_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "keyword",
                "market_price_eur",
                "max_buy_price_yen",
                "category",
                "price_source",
                "confidence",
                "reason",
            ]
        )
        writer.writerows(suggestion_rows)

    if len(difficult_titles) < 10:
        for row in needs_rows:
            title = row.get("title", "")
            if title and title not in difficult_titles:
                difficult_titles.append(title)
            if len(difficult_titles) >= 10:
                break

    return {
        "needs_price_count": len(needs_rows),
        "top_keywords": token_counts.most_common(20),
        "suggestions_count": len(suggestion_rows),
        "difficult_titles": difficult_titles[:10],
        "report_path": report_path,
        "suggestions_path": suggestions_path,
    }


def show_best_in_terminal(
    deals: List[Dict[str, object]],
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> None:
    rows = sort_best_deals_rows(
        [
            build_sheet_row(
                deal,
                conn=conn,
                existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
            )
            for deal in deals
        ]
    )

    sections = [
        DECISION_BUY_ALERT,
        DECISION_WATCH,
        DECISION_WATCH_AUCTION,
        DECISION_WATCH_LOW_AUCTION,
        DECISION_NEEDS_PRICE,
    ]

    for decision_label in sections:
        decision_rows = [row for row in rows if row.get("decision") == decision_label]
        logging.info("----- %s -----", decision_label)
        if not decision_rows:
            logging.info("No %s deals.", decision_label)
            continue

        for row in decision_rows:
            logging.info(
                "[%s] listing_type=%s | listing_type_reason=%s | title=%s | price_yen=%s | market_price_eur=%s | matched_market_keyword=%s | market_price_source=%s | market_price_confidence=%s | profit_eur=%s | roi_percent=%s | link_for_zenmarket=%s",
                decision_label,
                row.get("listing_type", ""),
                row.get("listing_type_reason", ""),
                row.get("title", ""),
                row.get("price_yen", ""),
                row.get("market_price_eur", ""),
                row.get("matched_market_keyword", ""),
                row.get("market_price_source", ""),
                row.get("market_price_confidence", ""),
                row.get("profit_eur", ""),
                row.get("roi_percent", ""),
                row.get("link_for_zenmarket", ""),
            )
            if row.get("decision") == DECISION_NEEDS_PRICE:
                logging.info("⚠️ Prix marché inconnu : à renseigner dans market_prices.csv")
            if row.get("auction_warning") == LOW_START_WARNING_VALUE:
                logging.info("⚠️ Enchère basse : ROI temporaire, à vérifier proche de la fin")


def run_needs_price_report_only() -> None:
    setup_logging()
    logging.info("Generating NEEDS_PRICE reports from stored deals.")

    global MARKET_PRICES
    global PRODUCT_ALIASES
    MARKET_PRICES = load_market_prices()
    PRODUCT_ALIASES = load_product_aliases()

    conn = sqlite3.connect(config.DATABASE_PATH)
    init_db(conn)
    all_deals = fetch_all_deals(conn)
    existing_sheet_data = load_google_sheet_existing_values()
    report_result = generate_needs_price_reports(all_deals, conn=conn, existing_sheet_data=existing_sheet_data)
    conn.close()
    logging.info("Total NEEDS_PRICE: %s", report_result["needs_price_count"])
    logging.info("Top 20 keyword_candidate:")
    for keyword_candidate, count in report_result["top_keywords"]:
        logging.info("- %s: %s", keyword_candidate, count)
    logging.info("Suggestions created: %s", report_result["suggestions_count"])
    logging.info("10 difficult example titles:")
    for title in report_result["difficult_titles"]:
        logging.info("- %s", title)
    logging.info("Report written: %s", report_result["report_path"])
    logging.info("Suggestions written: %s", report_result["suggestions_path"])


def run_auto_price_test() -> None:
    setup_logging()
    logging.info("Running auto price dry-run on stored NEEDS_PRICE listings.")

    global MARKET_PRICES
    global PRODUCT_ALIASES
    MARKET_PRICES = load_market_prices()
    PRODUCT_ALIASES = load_product_aliases()

    conn = sqlite3.connect(config.DATABASE_PATH)
    init_db(conn)
    all_deals = fetch_all_deals(conn)
    existing_sheet_data = load_google_sheet_existing_values()
    needs_rows = collect_needs_price_rows(all_deals, conn=conn, existing_sheet_data=existing_sheet_data)[:5]

    if not needs_rows:
        logging.info("No NEEDS_PRICE listings available for auto price test.")
        conn.close()
        return

    logging.info(
        "AUTO_PRICE_ENABLED=%s | no Google Sheets write | no market_prices.csv write",
        config.AUTO_PRICE_ENABLED,
    )
    for row in needs_rows:
        alias_match = match_product_alias(row.get("title", ""), PRODUCT_ALIASES)
        market_match = match_market_price(row.get("title", ""), MARKET_PRICES)
        resolved = resolve_market_price_auto(conn, row.get("title", ""), alias_match, market_match)
        logging.info("----- AUTO PRICE TEST -----")
        logging.info("title=%s", row.get("title", ""))
        logging.info("listing_type=%s | decision=%s", row.get("listing_type", ""), row.get("decision", ""))
        logging.info("matched_product_japanese=%s", row.get("matched_product_japanese", ""))
        logging.info(
            "queries | cardmarket=%s | ebay=%s | pricecharting=%s",
            row.get("cardmarket_query", ""),
            row.get("ebay_query", ""),
            row.get("pricecharting_query", ""),
        )
        if resolved:
            logging.info(
                "resolved | market_price_eur=%s | source=%s | confidence=%s | sample_size=%s | auto_used=%s | raw_summary=%s",
                round(resolved.market_price_eur, 2),
                resolved.source,
                resolved.confidence,
                resolved.sample_size,
                resolved.auto_price_used,
                resolved.raw_summary,
            )
        else:
            logging.info("resolved | None (AUTO_PRICE disabled, cache miss, or no successful provider result)")

    conn.close()


def summarize_decisions(
    deals: List[Dict[str, object]],
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, int]:
    counts = {
        DECISION_BUY_ALERT: 0,
        DECISION_WATCH: 0,
        DECISION_WATCH_AUCTION: 0,
        DECISION_WATCH_LOW_AUCTION: 0,
        DECISION_NEEDS_PRICE: 0,
        DECISION_SKIP: 0,
        DECISION_IGNORE: 0,
    }
    for row in [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]:
        decision = row.get("decision", DECISION_SKIP)
        if decision in counts:
            counts[decision] += 1
    return counts


def summarize_listing_types(
    deals: List[Dict[str, object]],
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, int]:
    counts = {
        LISTING_TYPE_BUY_NOW: 0,
        LISTING_TYPE_FIXED_PRICE: 0,
        LISTING_TYPE_AUCTION: 0,
        LISTING_TYPE_LOW_START_AUCTION: 0,
        LISTING_TYPE_UNKNOWN: 0,
    }
    for row in [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]:
        listing_type = row.get("listing_type", LISTING_TYPE_UNKNOWN)
        if listing_type in counts:
            counts[listing_type] += 1
    return counts


def summarize_sheet_views(
    deals: List[Dict[str, object]],
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, int]:
    rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]
    return {
        GOOGLE_BUY_NOW_WORKSHEET_NAME: len(sort_buy_now_deals_rows(rows)),
        GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME: len(sort_auctions_watch_rows(rows)),
        GOOGLE_NEEDS_PRICE_WORKSHEET_NAME: len(sort_needs_price_rows(rows)),
    }


def _get_or_create_worksheet(spreadsheet: object, worksheet_name: str) -> object:
    try:
        return spreadsheet.worksheet(worksheet_name)
    except Exception as exc:  # noqa: BLE001
        exc_name = exc.__class__.__name__
        if exc_name != "WorksheetNotFound":
            raise
    return spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=80)


def _read_existing_sheet_rows(worksheet: object, headers: List[str]) -> Dict[str, Dict[str, str]]:
    existing_values = worksheet.get_all_values()
    existing_by_url: Dict[str, Dict[str, str]] = {}
    if not existing_values:
        return existing_by_url

    existing_headers = [get_internal_header_name(header) for header in existing_values[0]]
    header_index = {h: i for i, h in enumerate(existing_headers)}
    for row in existing_values[1:]:
        row_dict: Dict[str, str] = {}
        for col_name in headers:
            idx = header_index.get(col_name)
            raw_value = row[idx] if idx is not None and idx < len(row) else ""
            row_dict[col_name] = parse_visible_value(col_name, raw_value)
        row_url = row_dict.get("url", "").strip()
        if row_url:
            existing_by_url[row_url] = row_dict
    return existing_by_url


def load_google_sheet_existing_values() -> Dict[str, Dict[str, str]]:
    if not config.GOOGLE_SHEETS_ENABLED:
        return {}

    try:
        import gspread
        from gspread.exceptions import SpreadsheetNotFound
    except Exception as exc:  # noqa: BLE001
        logging.error("Google Sheets dependencies are missing: %s", exc)
        return {}

    try:
        gc = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
        spreadsheet = gc.open(config.GOOGLE_SHEET_NAME)
    except SpreadsheetNotFound:
        logging.error(
            "Google Sheet '%s' not found while loading manual sheet values.",
            config.GOOGLE_SHEET_NAME,
        )
        return {}
    except Exception as exc:  # noqa: BLE001
        logging.error("Could not load existing Google Sheet values: %s", exc)
        return {}

    try:
        deals_ws = _get_or_create_worksheet(spreadsheet, GOOGLE_DEALS_WORKSHEET_NAME)
        best_ws = _get_or_create_worksheet(spreadsheet, GOOGLE_BEST_WORKSHEET_NAME)
        buy_now_ws = _get_or_create_worksheet(spreadsheet, GOOGLE_BUY_NOW_WORKSHEET_NAME)
        auctions_ws = _get_or_create_worksheet(spreadsheet, GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME)
        needs_price_ws = _get_or_create_worksheet(spreadsheet, GOOGLE_NEEDS_PRICE_WORKSHEET_NAME)
        deals_existing = _read_existing_sheet_rows(deals_ws, GOOGLE_DEALS_HEADERS)
        best_existing = _read_existing_sheet_rows(best_ws, GOOGLE_BEST_DEALS_HEADERS)
        buy_now_existing = _read_existing_sheet_rows(buy_now_ws, GOOGLE_BUY_NOW_DEALS_HEADERS)
        auctions_existing = _read_existing_sheet_rows(auctions_ws, GOOGLE_AUCTIONS_WATCH_HEADERS)
        needs_price_existing = _read_existing_sheet_rows(needs_price_ws, GOOGLE_NEEDS_PRICE_HEADERS)
        return merge_manual_values_by_priority(
            needs_price_existing,
            best_existing,
            buy_now_existing,
            auctions_existing,
            deals_existing,
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("Could not merge existing manual Google Sheet values: %s", exc)
        return {}


def _sync_single_google_worksheet(
    spreadsheet: object,
    worksheet_name: str,
    headers: List[str],
    rows: List[Dict[str, str]],
) -> int:
    worksheet = _get_or_create_worksheet(spreadsheet, worksheet_name)
    pre_existing_values = worksheet.get_all_values()
    existing_by_url = _read_existing_sheet_rows(worksheet, headers)

    merged_by_url: Dict[str, Dict[str, str]] = {}
    for url, row in existing_by_url.items():
        merged_by_url[url] = dict(row)

    for row in rows:
        url = row.get("url", "").strip()
        if not url:
            continue
        next_row = dict(row)
        existing_row = merged_by_url.get(url, {})
        for field in PRESERVED_SHEET_FIELDS:
            if existing_row.get(field) and not next_row.get(field):
                next_row[field] = existing_row.get(field, "")
        merged_by_url[url] = next_row

    ordered_rows: List[Dict[str, str]] = []
    seen_urls = set()
    for row in rows:
        url = row.get("url", "").strip()
        if not url or url in seen_urls:
            continue
        ordered_rows.append(merged_by_url[url])
        seen_urls.add(url)

    display_headers = get_sheet_display_headers(headers)
    payload = [display_headers]
    for row in ordered_rows:
        payload.append(build_sheet_display_row(headers, row))

    worksheet.clear()
    worksheet.update(range_name="A1", values=payload, value_input_option="RAW")
    existing_row_count = len(worksheet.get_all_values())
    payload_row_count = len(payload)
    existing_col_count = max((len(row) for row in pre_existing_values), default=0)
    if existing_row_count > payload_row_count:
        last_col = column_letter(len(headers))
        worksheet.batch_clear([f"A{payload_row_count + 1}:{last_col}{existing_row_count}"])
    if existing_col_count > len(headers):
        extra_start_col = column_letter(len(headers) + 1)
        extra_end_col = column_letter(existing_col_count)
        worksheet.batch_clear([f"{extra_start_col}1:{extra_end_col}{max(existing_row_count, payload_row_count)}"])
    apply_google_sheet_formatting(spreadsheet, worksheet, worksheet_name, headers, ordered_rows)
    return len(ordered_rows)


def export_to_google_sheets(
    deals: List[Dict[str, object]],
    conn: Optional[sqlite3.Connection] = None,
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, object]:
    if not config.GOOGLE_SHEETS_ENABLED:
        return {}

    if not deals:
        return {}

    try:
        import gspread
        from gspread.exceptions import SpreadsheetNotFound
    except Exception as exc:  # noqa: BLE001
        logging.error("Google Sheets dependencies are missing: %s", exc)
        return {}

    try:
        gc = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
    except Exception as exc:  # noqa: BLE001
        logging.error("Google service account auth failed: %s", exc)
        return {}

    try:
        spreadsheet = gc.open(config.GOOGLE_SHEET_NAME)
    except SpreadsheetNotFound:
        logging.error(
            "Google Sheet '%s' not found. Create it and share it with the service account email.",
            config.GOOGLE_SHEET_NAME,
        )
        return {}
    except Exception as exc:  # noqa: BLE001
        logging.error("Google Sheet open failed: %s", exc)
        return {}

    sheet_rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]
    deals_rows = sort_deals_rows(
        sheet_rows,
        prefer_buy_now=bool(config.PREFER_BUY_NOW),
    )
    best_rows = sort_best_deals_rows(sheet_rows)
    buy_now_rows = sort_buy_now_deals_rows(sheet_rows)
    auctions_watch_rows = sort_auctions_watch_rows(sheet_rows)
    needs_price_rows = sort_needs_price_rows(sheet_rows)

    try:
        deals_count = _sync_single_google_worksheet(
            spreadsheet=spreadsheet,
            worksheet_name=GOOGLE_DEALS_WORKSHEET_NAME,
            headers=GOOGLE_DEALS_HEADERS,
            rows=deals_rows,
        )
        best_count = _sync_single_google_worksheet(
            spreadsheet=spreadsheet,
            worksheet_name=GOOGLE_BEST_WORKSHEET_NAME,
            headers=GOOGLE_BEST_DEALS_HEADERS,
            rows=best_rows,
        )
        buy_now_count = _sync_single_google_worksheet(
            spreadsheet=spreadsheet,
            worksheet_name=GOOGLE_BUY_NOW_WORKSHEET_NAME,
            headers=GOOGLE_BUY_NOW_DEALS_HEADERS,
            rows=buy_now_rows,
        )
        auctions_count = _sync_single_google_worksheet(
            spreadsheet=spreadsheet,
            worksheet_name=GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME,
            headers=GOOGLE_AUCTIONS_WATCH_HEADERS,
            rows=auctions_watch_rows,
        )
        needs_price_count = _sync_single_google_worksheet(
            spreadsheet=spreadsheet,
            worksheet_name=GOOGLE_NEEDS_PRICE_WORKSHEET_NAME,
            headers=GOOGLE_NEEDS_PRICE_HEADERS,
            rows=needs_price_rows,
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("Google worksheet write failed: %s", exc)
        return {}

    return {
        "counts": {
            GOOGLE_DEALS_WORKSHEET_NAME: deals_count,
            GOOGLE_BEST_WORKSHEET_NAME: best_count,
            GOOGLE_BUY_NOW_WORKSHEET_NAME: buy_now_count,
            GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME: auctions_count,
            GOOGLE_NEEDS_PRICE_WORKSHEET_NAME: needs_price_count,
        },
        "sheet_name": config.GOOGLE_SHEET_NAME,
        "sheet_url": getattr(spreadsheet, "url", ""),
    }


def send_telegram_alerts(deals: List[ScoredDeal]) -> int:
    if not config.TELEGRAM_ENABLED:
        return 0

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logging.warning("Telegram enabled but token/chat_id is missing. Skipping alerts.")
        return 0

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    sent = 0

    for deal in deals[: config.MAX_TELEGRAM_ALERTS_PER_RUN]:
        message = (
            "Pokemon Watch Bot - Interesting deal detected\n"
            f"Rule: {deal.rule_name}\n"
            f"Title: {deal.title}\n"
            f"Price: {deal.price_yen:,} JPY\n"
            f"Landed cost: {deal.landed_cost_eur:.2f} EUR\n"
            f"Safe resale: {deal.safe_resale_eur:.2f} EUR\n"
            f"Profit est.: {deal.profit_eur:.2f} EUR\n"
            f"ROI est.: {deal.roi_percent:.2f}%\n"
            f"Score: {deal.score:.1f}/100\n"
            f"URL: {deal.url}\n"
            "Manual buy only: copy this link into ZenMarket."
        )
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, data=payload, timeout=config.TELEGRAM_TIMEOUT_SECONDS)
            resp.raise_for_status()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logging.warning("Telegram alert failed for %s: %s", deal.listing_id, exc)

    return sent


def main(best_only: bool = False) -> None:
    setup_logging()
    logging.info("Starting pokemon-watch-bot in watch-only mode (no auto-buy).")

    global MARKET_PRICES
    global PRODUCT_ALIASES
    MARKET_PRICES = load_market_prices()
    PRODUCT_ALIASES = load_product_aliases()

    rules = load_rules()
    runtime_blacklist_keywords = load_blacklist_keywords()
    session = requests.Session()

    conn = sqlite3.connect(config.DATABASE_PATH)
    init_db(conn)

    new_deals: List[ScoredDeal] = []
    all_eligible_this_run: List[ScoredDeal] = []
    total_raw_listings_parsed = 0

    for index, rule in enumerate(rules, start=1):
        logging.info("[%s/%s] Query: %s", index, len(rules), rule.query)
        try:
            markdown = fetch_markdown(session, rule.query)
        except Exception as exc:  # noqa: BLE001
            logging.error("Failed to fetch query '%s': %s", rule.query, exc)
            continue

        raw_listings = parse_listings_from_markdown(markdown, config.MAX_RESULTS_PER_RULE)
        logging.info("Parsed %s raw listings", len(raw_listings))
        total_raw_listings_parsed += len(raw_listings)

        for listing in raw_listings:
            if listing.price_yen is None:
                continue

            if listing.price_yen > rule.max_price_yen:
                continue

            if rule.required_keywords and not title_has_keywords(listing.title, rule.required_keywords):
                continue

            all_blacklist = list(runtime_blacklist_keywords) + list(rule.blacklist_keywords)
            blocked_by = title_has_any_blacklisted(listing.title, all_blacklist)
            if blocked_by:
                logging.debug("Skipping %s due to blacklist keyword: %s", listing.listing_id, blocked_by)
                continue

            try:
                scored = compute_deal(rule, listing, MARKET_PRICES, conn=conn)
            except Exception as exc:  # noqa: BLE001
                logging.debug("Could not score %s: %s", listing.listing_id, exc)
                continue

            all_eligible_this_run.append(scored)
            if save_if_new(conn, scored):
                logging.info(
                    "NEW | score=%.1f roi=%.1f%% profit=%.2fEUR | %s",
                    scored.score,
                    scored.roi_percent,
                    scored.profit_eur,
                    scored.title,
                )
                new_deals.append(scored)

        if index < len(rules):
            time.sleep(config.REQUEST_SLEEP_BETWEEN_QUERIES_SECONDS)

    all_deals = fetch_all_deals(conn)
    existing_sheet_data = load_google_sheet_existing_values()
    csv_rows = export_csv(all_deals, config.CSV_EXPORT_PATH, conn=conn, existing_sheet_data=existing_sheet_data)
    gs_result = export_to_google_sheets(all_deals, conn=conn, existing_sheet_data=existing_sheet_data)

    current_run_deals = [deal.__dict__.copy() for deal in all_eligible_this_run]
    summary_counts = summarize_decisions(current_run_deals, conn=conn, existing_sheet_data=existing_sheet_data)
    listing_type_counts = summarize_listing_types(current_run_deals, conn=conn, existing_sheet_data=existing_sheet_data)
    sheet_view_counts = summarize_sheet_views(current_run_deals, conn=conn, existing_sheet_data=existing_sheet_data)

    if best_only:
        show_best_in_terminal(all_deals, conn=conn, existing_sheet_data=existing_sheet_data)

    interesting_deals = [d for d in new_deals if should_alert_telegram(d)]
    telegram_sent = send_telegram_alerts(interesting_deals)
    conn.close()

    logging.info("Run complete.")
    logging.info("New unique deals stored: %s", len(new_deals))
    logging.info("Total raw listings parsed: %s", total_raw_listings_parsed)
    logging.info("Eligible scored deals this run: %s", len(all_eligible_this_run))
    logging.info("CSV exported rows: %s (%s)", csv_rows, config.CSV_EXPORT_PATH)
    if config.GOOGLE_SHEETS_ENABLED:
        gs_counts = gs_result.get("counts", {}) if isinstance(gs_result, dict) else {}
        logging.info(
            "Google Sheets synced rows: Deals=%s | Best Deals=%s | Buy Now Deals=%s | Auctions Watch=%s | Needs Price=%s (%s)",
            gs_counts.get(GOOGLE_DEALS_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_BEST_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_BUY_NOW_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_NEEDS_PRICE_WORKSHEET_NAME, 0),
            gs_result.get("sheet_name", config.GOOGLE_SHEET_NAME) if isinstance(gs_result, dict) else config.GOOGLE_SHEET_NAME,
        )
    else:
        logging.info("Google Sheets export disabled.")
    logging.info(
        "Summary: raw_parsed=%s | eligible_scored=%s | BUY ALERT=%s | WATCH=%s | WATCH_AUCTION=%s | WATCH_LOW_AUCTION=%s | NEEDS_PRICE=%s | SKIP=%s | sheet=%s",
        total_raw_listings_parsed,
        len(all_eligible_this_run),
        summary_counts.get(DECISION_BUY_ALERT, 0),
        summary_counts.get(DECISION_WATCH, 0),
        summary_counts.get(DECISION_WATCH_AUCTION, 0),
        summary_counts.get(DECISION_WATCH_LOW_AUCTION, 0),
        summary_counts.get(DECISION_NEEDS_PRICE, 0),
        summary_counts.get(DECISION_SKIP, 0),
        (
            gs_result.get("sheet_url") or gs_result.get("sheet_name", config.GOOGLE_SHEET_NAME)
            if isinstance(gs_result, dict)
            else config.GOOGLE_SHEET_NAME
        ),
    )
    logging.info(
        "Listing types: BUY_NOW=%s | FIXED_PRICE=%s | AUCTION=%s | LOW_START_AUCTION=%s | UNKNOWN=%s",
        listing_type_counts.get(LISTING_TYPE_BUY_NOW, 0),
        listing_type_counts.get(LISTING_TYPE_FIXED_PRICE, 0),
        listing_type_counts.get(LISTING_TYPE_AUCTION, 0),
        listing_type_counts.get(LISTING_TYPE_LOW_START_AUCTION, 0),
        listing_type_counts.get(LISTING_TYPE_UNKNOWN, 0),
    )
    logging.info(
        "Views: Buy Now Deals=%s | Auctions Watch=%s | Needs Price=%s",
        sheet_view_counts.get(GOOGLE_BUY_NOW_WORKSHEET_NAME, 0),
        sheet_view_counts.get(GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME, 0),
        sheet_view_counts.get(GOOGLE_NEEDS_PRICE_WORKSHEET_NAME, 0),
    )
    logging.info("Telegram alerts sent: %s", telegram_sent)


if __name__ == "__main__":
    cli_args = parse_args()
    if cli_args.needs_price_report:
        run_needs_price_report_only()
    elif cli_args.auto_price_test:
        run_auto_price_test()
    else:
        main(best_only=cli_args.best)
