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
    time_left_minutes: Optional[int] = None
    auction_end_at: str = ""
    auction_ending_soon: str = ""
    auction_is_ended: bool = False
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
    price_source: str
    current_price_yen: Optional[int]
    buy_now_price_yen: Optional[int]
    bid_count: Optional[int]
    time_left: str
    time_left_minutes: Optional[int]
    auction_end_at: str
    auction_ending_soon: str
    time_left_source: str
    raw_time_left_text: str
    auction_is_ended: bool
    seller_name: str
    seller_rating: str
    shipping_japan: str
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
ZENMARKET_CURRENT_PRICE_RE = re.compile(
    r"(?:Current price|Current bid|Prix actuel|Ench[eè]re actuelle|現在価格|入札価格)\s*(?:\[[^\]]*\])?\s*(?:\*+)?\s*"
    r"(?:\n|\r|\s)*[¥￥]\s*([0-9,]+)",
    re.IGNORECASE,
)
ZENMARKET_BUY_NOW_PRICE_RE = re.compile(
    r"(?:Buyout price|Buy now|Instant purchase|Prix d['’]achat imm[eé]diat|Acheter maintenant|即決価格|即決|Buyout)\s*"
    r"(?:\[[^\]]*\])?\s*(?:\*+)?\s*(?:\n|\r|\s)*[¥￥]\s*([0-9,]+)",
    re.IGNORECASE,
)
ZENMARKET_BID_COUNT_RE = re.compile(
    r"(?:Number of bids|Bid count|Nombre d['’]ench[eè]res|入札件数|入札)\s*:?\**\s*([0-9,]+)",
    re.IGNORECASE,
)
ZENMARKET_ENDS_AT_RE = re.compile(
    r"Ends in:\**\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4}\s+[0-9:]+\s*[AP]M)\**\s*\(Tokyo\)",
    re.IGNORECASE,
)
ZENMARKET_CURRENT_TIME_RE = re.compile(
    r"Current time:\**\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4}\s+[0-9:]+\s*[AP]M)\**\s*\(Tokyo\)",
    re.IGNORECASE,
)
ZENMARKET_TIME_LEFT_RE = re.compile(
    r"(?:This auction ends in:|Time left|Temps restant|残り時間)\s*(?:\n|\r|\s|\*){0,20}([^\n\r*]{1,40})",
    re.IGNORECASE,
)
ZENMARKET_SHIPPING_RE = re.compile(
    r"Shipping within Japan:\**\s*([^\n\r*]{1,40})",
    re.IGNORECASE,
)
JP_DURATION_RE = re.compile(
    r"([0-9０-９]+\s*日(?:\s*[0-9０-９]+\s*時間)?(?:\s*[0-9０-９]+\s*分)?|"
    r"[0-9０-９]+\s*時間(?:\s*[0-9０-９]+\s*分)?|"
    r"[0-9０-９]+\s*分)"
)
EN_DURATION_RE = re.compile(
    r"(\d+\s*days?(?:\s*\d+\s*hours?)?(?:\s*\d+\s*minutes?)?|"
    r"\d+\s*hours?(?:\s*\d+\s*minutes?)?|"
    r"\d+\s*minutes?|"
    r"\d+\s*h(?:\s*\d+\s*m)?|"
    r"\d+\s*m\b)",
    re.IGNORECASE,
)
TIME_LEFT_CONTEXT_RE = re.compile(
    r"(?:残り時間|残り|あと|time left|ends in|ending soon|ended)[^|]{0,64}",
    re.IGNORECASE,
)
END_TIME_RE = re.compile(
    r"(?:終了日時|終了予定|deadline|auction_end_time|end_time)[^0-9]*"
    r"(?:(?P<year>\d{4})年)?\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日"
    r"(?:[^0-9]+)?(?P<hour>\d{1,2})時(?:\s*(?P<minute>\d{1,2})分)?"
)
END_TIME_FALLBACK_RE = re.compile(
    r"(?:(?P<year>\d{4})年)?\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日"
    r"(?:[^0-9]+)?(?P<hour>\d{1,2})時(?:\s*(?P<minute>\d{1,2})分)?\s*終了"
)
SELLER_RE = re.compile(r"出品者[:：]\s*([^\s|]+)")
SELLER_RATING_RE = re.compile(r"評価[:：]\s*([0-9.]+)")

DB_EXPORT_COLUMNS = [
    "listing_id",
    "title",
    "url",
    "query",
    "rule_name",
    "price_yen",
    "price_source",
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
    "current_price_yen",
    "buy_now_price_yen",
    "bid_count",
    "time_left",
    "time_left_minutes",
    "auction_end_at",
    "auction_ending_soon",
    "time_left_source",
    "raw_time_left_text",
    "auction_is_ended",
    "seller_name",
    "seller_rating",
    "shipping_japan",
    "detected_at",
]

GOOGLE_DEALS_WORKSHEET_NAME = "Historique"
GOOGLE_MODE_EMPLOI_WORKSHEET_NAME = "Mode d’emploi"
GOOGLE_LEGACY_DEALS_WORKSHEET_NAME = "Deals"
GOOGLE_DEALS_HEADERS = [
    "created_at",
    "opportunity_type",
    "title",
    "time_left",
    "time_left_minutes",
    "auction_ending_soon",
    "auction_is_ended",
    "price_yen",
    "current_price_yen",
    "buy_now_price_yen",
    "bid_count",
    "total_cost_eur",
    "market_price_eur",
    "profit_eur",
    "roi_percent",
    "price_source",
    "time_left_source",
    "risk_flags",
    "deal_quality_score",
    "link_for_zenmarket",
    "url",
    "notes",
    "seller_name",
    "seller_rating",
    "shipping_japan",
    "query",
    "rule_name",
    "data_source",
    "listing_type_reason",
    "matched_market_keyword",
    "market_price_source",
    "market_price_confidence",
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
    "auction_warning",
    "score",
    "score_reliability",
]

GOOGLE_BEST_DEALS_HEADERS = [
    "created_at",
    "opportunity_type",
    "title",
    "time_left",
    "time_left_minutes",
    "auction_ending_soon",
    "price_yen",
    "current_price_yen",
    "buy_now_price_yen",
    "total_cost_eur",
    "market_price_eur",
    "profit_eur",
    "roi_percent",
    "price_source",
    "link_for_zenmarket",
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
    "time_left",
    "time_left_minutes",
    "auction_ending_soon",
    "auction_is_ended",
    "price_yen",
    "buy_now_price_yen",
    "current_price_yen",
    "bid_count",
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
    "opportunity_type",
    "title",
    "time_left",
    "time_left_minutes",
    "auction_ending_soon",
    "price_yen",
    "current_price_yen",
    "buy_now_price_yen",
    "total_cost_eur",
    "manual_market_price_eur",
    "price_source",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "pricecharting_search_url",
    "link_for_zenmarket",
    "notes",
    "url",
]

GOOGLE_BEST_WORKSHEET_NAME = "Opportunités"
GOOGLE_BUY_NOW_WORKSHEET_NAME = "Buy Now Deals"
GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME = "Auctions Watch"
GOOGLE_NEEDS_PRICE_WORKSHEET_NAME = "Prix à remplir"
GOOGLE_LEGACY_BEST_WORKSHEET_NAME = "Best Deals"
GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME = "Needs Price"
DETAIL_ENRICHMENT_LOG_PATH = "detail_enrichment_log.csv"
FINAL_USEFUL_DECISIONS = {
    "NEEDS_PRICE",
    "WATCH_AUCTION",
    "WATCH_LOW_AUCTION",
    "WATCH",
    "BUY ALERT",
}
DOUBLE_CHECK_EXCLUDED_RISK_FLAGS = {
    "GRADED",
    "DAMAGED",
    "MYSTERY_PACK",
    "SEARCHED_PACK",
    "OLD_CARD_SINGLE",
}

GOOGLE_HEADER_LABELS = {
    "created_at": "Date ajout",
    "decision": "Statut",
    "manual_action_needed": "Action requise",
    "listing_type": "Type annonce",
    "opportunity_type": "Type opportunité",
    "listing_type_reason": "Raison type",
    "title": "Titre",
    "price_yen": "Prix Japon ¥",
    "price_source": "Source prix achat",
    "current_price_yen": "Prix actuel ¥",
    "buy_now_price_yen": "Prix achat immédiat ¥",
    "time_left_minutes": "Minutes restantes",
    "auction_end_at": "Fin enchère",
    "auction_ending_soon": "Fin bientôt",
    "time_left_source": "Source temps restant",
    "raw_time_left_text": "Texte brut temps restant",
    "auction_is_ended": "Enchère terminée",
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
    "risk_flags": "Risk flags",
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
    "opportunity_type": {
        "AUCTION_ONLY": "Enchère seule",
        "AUCTION_PLUS_BUY_NOW": "Enchère + achat immédiat",
        "BUY_NOW": "Achat immédiat",
        "FIXED_PRICE": "Prix fixe",
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
    "price_source": {
        "zenmarket_detail": "Détail ZenMarket",
        "yahoo_detail": "Détail Yahoo",
        "yahoo_search": "Résultat Yahoo",
        "sqlite_cache": "Cache SQLite",
        "unknown": "Inconnu",
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
    "auction_ending_soon": {
        "VERY_SOON": "Oui, très bientôt",
        "SOON": "Oui",
        "NO": "Non",
        "UNKNOWN": "Inconnu",
        "ENDED": "Terminée",
    },
    "auction_is_ended": {
        "True": "Oui",
        "False": "Non",
    },
}
RISK_FLAG_LABELS = {
    "LOW_START_AUCTION": "Enchère basse",
    "AUCTION_ENDED": "Enchère terminée",
    "NO_SHRINK": "Sans shrink",
    "OPENED": "Ouvert",
    "EMPTY_BOX": "Boîte vide",
    "SEARCHED_PACK": "Pack recherché",
    "MYSTERY_PACK": "Pack mystère",
    "DAMAGED": "Abîmé",
    "GENERIC_LOT": "Lot générique",
    "UNKNOWN_PRICE": "Prix inconnu",
    "SELLER_RISK": "Risque vendeur",
    "GOOD_CONDITION_LOOSE": "Carte loose bon état",
    "GRADED": "Carte gradée",
    "OLD_CARD_SINGLE": "Vieille carte seule",
    "OLD_BACK_LOT": "Lot 旧裏/旧裏面",
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
OPPORTUNITY_TYPE_AUCTION_ONLY = "AUCTION_ONLY"
OPPORTUNITY_TYPE_AUCTION_PLUS_BUY_NOW = "AUCTION_PLUS_BUY_NOW"
OPPORTUNITY_TYPE_BUY_NOW = "BUY_NOW"
OPPORTUNITY_TYPE_FIXED_PRICE = "FIXED_PRICE"
OPPORTUNITY_TYPE_UNKNOWN = "UNKNOWN"
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
AUCTION_ENDING_VERY_SOON = "VERY_SOON"
AUCTION_ENDING_SOON = "SOON"
AUCTION_ENDING_NO = "NO"
AUCTION_ENDING_UNKNOWN = "UNKNOWN"
AUCTION_ENDING_ENDED = "ENDED"
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
GOOGLE_SHEET_MIGRATIONS = [
    (GOOGLE_LEGACY_BEST_WORKSHEET_NAME, GOOGLE_BEST_WORKSHEET_NAME),
    (GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME, GOOGLE_NEEDS_PRICE_WORKSHEET_NAME),
    (GOOGLE_LEGACY_DEALS_WORKSHEET_NAME, GOOGLE_DEALS_WORKSHEET_NAME),
]
GRADED_KEYWORDS = ["PSA", "PSA10", "PSA9", "ARS", "ARS10", "CGC", "BGS", "鑑定品", "鑑定", "ケース付き", "スラブ"]
GOOD_CONDITION_KEYWORDS = ["美品", "未使用", "目立った傷なし", "極美品"]
DAMAGED_KEYWORDS = ["傷あり", "傷", "白かけ", "折れ", "凹み", "破損", "ジャンク"]
OLD_BACK_KEYWORDS = ["旧裏", "旧裏面"]
OLD_BACK_ALLOWED_LOT_KEYWORDS = ["まとめ売り", "大量", "引退品", "セット", "まとめ"]
SEALED_BOX_KEYWORDS = ["BOX", "ボックス", "未開封BOX", "シュリンク付き", "未開封", "新品未開封", "ハイクラスパック", "絶版BOX", "スペシャルBOX"]
AUCTION_ENDED_KEYWORDS = [
    "終了",
    "終了しました",
    "オークション終了",
    "このオークションは終了",
    "ended",
    "auction ended",
    "closed",
    "finished",
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


def contains_any_keyword(text: str, keywords: Sequence[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def is_sealed_box_or_display(title: str, matched_product_japanese: str = "") -> bool:
    haystack = combine_context(title or "", matched_product_japanese or "")
    return contains_any_keyword(haystack, SEALED_BOX_KEYWORDS)


def is_old_back_lot(title: str) -> bool:
    return contains_any_keyword(title, OLD_BACK_KEYWORDS) and contains_any_keyword(title, OLD_BACK_ALLOWED_LOT_KEYWORDS)


def is_old_card_single(title: str) -> bool:
    return contains_any_keyword(title, OLD_BACK_KEYWORDS) and not contains_any_keyword(title, OLD_BACK_ALLOWED_LOT_KEYWORDS)


def detect_auction_is_ended(*texts: str) -> bool:
    combined = normalize_numeric_text(combine_context(*texts))
    if not combined:
        return False
    lowered = combined.casefold()
    return (
        "このオークションは終了" in combined
        or "終了しました" in combined
        or "オークション終了" in combined
        or "残り時間 終了" in combined
        or "残り 終了" in combined
        or "time left ended" in lowered
        or "auction ended" in lowered
        or "ended" in lowered
        or "closed" in lowered
        or "finished" in lowered
        or "terminé" in lowered
    )


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
            if contains_any_keyword(keyword, GRADED_KEYWORDS):
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


def compute_opportunity_type(listing_type: str, buy_now_price_yen: Optional[int]) -> str:
    if listing_type in (LISTING_TYPE_AUCTION, LISTING_TYPE_LOW_START_AUCTION):
        return OPPORTUNITY_TYPE_AUCTION_PLUS_BUY_NOW if (buy_now_price_yen or 0) > 0 else OPPORTUNITY_TYPE_AUCTION_ONLY
    if listing_type == LISTING_TYPE_BUY_NOW:
        return OPPORTUNITY_TYPE_BUY_NOW
    if listing_type == LISTING_TYPE_FIXED_PRICE:
        return OPPORTUNITY_TYPE_FIXED_PRICE
    return OPPORTUNITY_TYPE_UNKNOWN


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
OPPORTUNITY_TYPE_BACKGROUND_COLORS = {
    OPPORTUNITY_TYPE_AUCTION_PLUS_BUY_NOW: rgb(221, 235, 247),
    OPPORTUNITY_TYPE_AUCTION_ONLY: DEFAULT_BACKGROUND_COLOR,
    OPPORTUNITY_TYPE_BUY_NOW: rgb(198, 239, 206),
    OPPORTUNITY_TYPE_FIXED_PRICE: rgb(198, 239, 206),
    OPPORTUNITY_TYPE_UNKNOWN: rgb(224, 224, 224),
}
AUCTION_ENDED_BACKGROUND_COLORS = {
    "True": rgb(224, 224, 224),
    "False": DEFAULT_BACKGROUND_COLOR,
}
RISK_FLAGS_BACKGROUND_COLORS = {
    "filled": rgb(252, 228, 214),
    "empty": DEFAULT_BACKGROUND_COLOR,
}
AUCTION_ENDING_SOON_COLORS = {
    AUCTION_ENDING_VERY_SOON: rgb(244, 204, 204),
    AUCTION_ENDING_SOON: rgb(252, 228, 214),
    AUCTION_ENDING_NO: DEFAULT_BACKGROUND_COLOR,
    AUCTION_ENDING_UNKNOWN: rgb(242, 242, 242),
    AUCTION_ENDING_ENDED: rgb(224, 224, 224),
}
LINK_COLUMNS = {
    "link_for_zenmarket",
    "url",
    "cardmarket_search_url",
    "ebay_sold_search_url",
    "pricecharting_search_url",
}
FIXED_COLUMN_WIDTHS = {
    "title": 420,
    "notes": 250,
    "link_for_zenmarket": 140,
    "url": 140,
    "cardmarket_search_url": 140,
    "ebay_sold_search_url": 140,
    "pricecharting_search_url": 140,
    "price_yen": 105,
    "current_price_yen": 105,
    "buy_now_price_yen": 120,
    "total_cost_eur": 115,
    "market_price_eur": 110,
    "profit_eur": 110,
    "roi_percent": 95,
    "time_left": 120,
    "time_left_minutes": 105,
    "auction_ending_soon": 95,
    "opportunity_type": 140,
    "price_source": 160,
    "time_left_source": 160,
    "risk_flags": 220,
    "auction_is_ended": 110,
    "bid_count": 110,
}


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


def build_column_width_request(worksheet_id: int, col_index: int, pixel_size: int) -> Dict[str, object]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": worksheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_index,
                "endIndex": col_index + 1,
            },
            "properties": {"pixelSize": pixel_size},
            "fields": "pixelSize",
        }
    }


def build_text_style_request(
    worksheet_id: int,
    col_index: int,
    row_count: int,
    wrap_strategy: str,
    horizontal_alignment: Optional[str] = None,
) -> Dict[str, object]:
    user_entered_format: Dict[str, object] = {"wrapStrategy": wrap_strategy}
    fields = ["userEnteredFormat.wrapStrategy"]
    if horizontal_alignment:
        user_entered_format["horizontalAlignment"] = horizontal_alignment
        fields.append("userEnteredFormat.horizontalAlignment")
    return {
        "repeatCell": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": 1,
                "endRowIndex": max(2, row_count + 1),
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "cell": {"userEnteredFormat": user_entered_format},
            "fields": ",".join(fields),
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

    opportunity_col = headers.index("opportunity_type") if "opportunity_type" in headers else None

    status_col = headers.index("decision") if "decision" in headers else None
    action_col = headers.index("manual_action_needed") if "manual_action_needed" in headers else None
    ending_col = headers.index("auction_ending_soon") if "auction_ending_soon" in headers else None
    ended_col = headers.index("auction_is_ended") if "auction_is_ended" in headers else None
    risk_col = headers.index("risk_flags") if "risk_flags" in headers else None
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
        if worksheet_name == GOOGLE_BEST_WORKSHEET_NAME and opportunity_col is not None:
            opportunity_color = OPPORTUNITY_TYPE_BACKGROUND_COLORS.get(
                row.get("opportunity_type", OPPORTUNITY_TYPE_UNKNOWN),
                DEFAULT_BACKGROUND_COLOR,
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": opportunity_col,
                            "endColumnIndex": opportunity_col + 1,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": opportunity_color}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
        if ending_col is not None:
            ending_color = AUCTION_ENDING_SOON_COLORS.get(
                row.get("auction_ending_soon", AUCTION_ENDING_UNKNOWN),
                DEFAULT_BACKGROUND_COLOR,
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": worksheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": ending_col,
                            "endColumnIndex": ending_col + 1,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": ending_color}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                    }
                )
        if ended_col is not None:
            ended_value = str(row.get("auction_is_ended", "") or "")
            ended_color = AUCTION_ENDED_BACKGROUND_COLORS.get(ended_value, DEFAULT_BACKGROUND_COLOR)
            if ended_color != DEFAULT_BACKGROUND_COLOR:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet_id,
                                "startRowIndex": row_index,
                                "endRowIndex": row_index + 1,
                                "startColumnIndex": ended_col,
                                "endColumnIndex": ended_col + 1,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": ended_color}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                )
        if risk_col is not None:
            risk_value = str(row.get("risk_flags", "") or "").strip()
            risk_color = RISK_FLAGS_BACKGROUND_COLORS["filled"] if risk_value else RISK_FLAGS_BACKGROUND_COLORS["empty"]
            if risk_color != DEFAULT_BACKGROUND_COLOR:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet_id,
                                "startRowIndex": row_index,
                                "endRowIndex": row_index + 1,
                                "startColumnIndex": risk_col,
                                "endColumnIndex": risk_col + 1,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": risk_color}},
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

    for col_index, field in enumerate(headers):
        if field in FIXED_COLUMN_WIDTHS:
            requests.append(
                build_column_width_request(
                    worksheet_id=worksheet_id,
                    col_index=col_index,
                    pixel_size=FIXED_COLUMN_WIDTHS[field],
                )
            )
        if field in LINK_COLUMNS:
            requests.append(
                build_text_style_request(
                    worksheet_id=worksheet_id,
                    col_index=col_index,
                    row_count=len(rows),
                    wrap_strategy="CLIP",
                )
            )
        elif field == "title":
            requests.append(
                build_text_style_request(
                    worksheet_id=worksheet_id,
                    col_index=col_index,
                    row_count=len(rows),
                    wrap_strategy="WRAP",
                )
            )
        elif field == "notes":
            requests.append(
                build_text_style_request(
                    worksheet_id=worksheet_id,
                    col_index=col_index,
                    row_count=len(rows),
                    wrap_strategy="WRAP",
                )
            )
        else:
            requests.append(
                build_text_style_request(
                    worksheet_id=worksheet_id,
                    col_index=col_index,
                    row_count=len(rows),
                    wrap_strategy="CLIP",
                )
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
    seller_match = SELLER_RE.search(haystack)
    seller_rating_match = SELLER_RATING_RE.search(haystack)
    time_left, time_left_minutes, raw_time_left_text = extract_time_left_data(haystack)
    auction_end_at = extract_auction_end_at(haystack)
    auction_is_ended = detect_auction_is_ended(haystack, time_left)
    if auction_is_ended:
        time_left = "Terminé"
        time_left_minutes = 0
        auction_ending_soon = AUCTION_ENDING_ENDED
    else:
        auction_ending_soon = get_auction_ending_soon_value(time_left_minutes)

    shipping_japan = ""
    if "送料無料" in haystack:
        shipping_japan = "送料無料"

    return {
        "current_price_yen": current_price,
        "buy_now_price_yen": buy_now_price,
        "bid_count": bid_count,
        "time_left": time_left,
        "time_left_minutes": str(time_left_minutes) if time_left_minutes is not None else "",
        "auction_end_at": auction_end_at,
        "auction_ending_soon": auction_ending_soon,
        "time_left_source": "yahoo_search",
        "raw_time_left_text": raw_time_left_text,
        "auction_is_ended": "True" if auction_is_ended else "False",
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
    auction_is_ended: bool = False,
) -> List[str]:
    flags: List[str] = []
    title_text = title or ""
    haystack = combine_context(title or "", context_text or "")

    if listing_type == LISTING_TYPE_LOW_START_AUCTION:
        flags.append("LOW_START_AUCTION")
    if auction_is_ended:
        flags.append("AUCTION_ENDED")
    if any(keyword in haystack for keyword in ("シュリンクなし", "シュリンク無し")):
        flags.append("NO_SHRINK")
    if "開封済み" in haystack:
        flags.append("OPENED")
    if any(keyword in haystack for keyword in ("空箱", "箱のみ", "パックのみ")):
        flags.append("EMPTY_BOX")
    if "サーチ済み" in haystack:
        flags.append("SEARCHED_PACK")
    if any(keyword in haystack for keyword in ("オリパ", "mystery pack", "mystery")):
        flags.append("MYSTERY_PACK")
    if contains_any_keyword(title_text, DAMAGED_KEYWORDS):
        flags.append("DAMAGED")
    if contains_any_keyword(title_text, GRADED_KEYWORDS):
        flags.append("GRADED")
    if contains_any_keyword(title_text, GOOD_CONDITION_KEYWORDS):
        flags.append("GOOD_CONDITION_LOOSE")
    if is_old_card_single(title_text):
        flags.append("OLD_CARD_SINGLE")
    elif is_old_back_lot(title_text):
        flags.append("OLD_BACK_LOT")
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
    if is_sealed_box_or_display(title, matched_product_japanese):
        score += 40
    if listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE):
        score += 30
    if "シュリンク付き" in title:
        score += 20
    if "未開封" in title:
        score += 20
    if "GOOD_CONDITION_LOOSE" in risk_flags:
        score += 15
    if "OLD_BACK_LOT" in risk_flags:
        score += 10
    if manual_market_price_eur > 0:
        score += 20
    if "GRADED" in risk_flags:
        score -= 50
    if "OLD_CARD_SINGLE" in risk_flags:
        score -= 50
    if "LOW_START_AUCTION" in risk_flags:
        score -= 30
    if any(flag in risk_flags for flag in ("EMPTY_BOX", "SEARCHED_PACK", "MYSTERY_PACK", "DAMAGED")):
        score -= 50
    if "NO_SHRINK" in risk_flags and is_sealed_box_or_display(title, matched_product_japanese):
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
    loose_queries = load_keywords_from_file(config.KEYWORDS_LOOSE_CARDS_FILE) or list(config.KEYWORDS_LOOSE_CARDS_DEFAULT)

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

    for query in loose_queries:
        _append_rule_if_new(
            rules=rules,
            known_queries=known_queries,
            name_prefix="LOOSE keyword",
            query=query,
            market_price_eur=float(config.LOOSE_RULE_DEFAULTS["market_price_eur"]),
            max_price_yen=int(config.LOOSE_RULE_DEFAULTS["max_price_yen"]),
        )

    return rules


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def normalize_numeric_text(value: str) -> str:
    return normalize_text(value).translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def parse_time_left_to_minutes(text: str) -> Optional[int]:
    cleaned = normalize_numeric_text(text)
    if not cleaned:
        return None

    lowered = cleaned.casefold()
    matched = False
    days = 0
    hours = 0
    minutes = 0

    for pattern, unit in (
        (r"(\d+)\s*日", "days"),
        (r"(\d+)\s*時間", "hours"),
        (r"(\d+)\s*分", "minutes"),
        (r"(\d+)\s*day[s]?\b", "days"),
        (r"(\d+)\s*jour[s]?\b", "days"),
        (r"(\d+)\s*hour[s]?\b", "hours"),
        (r"(\d+)\s*heure[s]?\b", "hours"),
        (r"(\d+)\s*minute[s]?\b", "minutes"),
        (r"(\d+)\s*d\b", "days"),
        (r"(\d+)\s*h\b", "hours"),
        (r"(\d+)\s*m\b", "minutes"),
    ):
        match = re.search(pattern, lowered, re.IGNORECASE)
        if not match:
            continue
        matched = True
        value = int(match.group(1))
        if unit == "days":
            days = value
        elif unit == "hours":
            hours = value
        else:
            minutes = value

    if matched:
        return (days * 1440) + (hours * 60) + minutes
    if "終了間近" in cleaned or "ending soon" in lowered:
        return 15
    if "bientôt terminé" in lowered:
        return 15
    if detect_auction_is_ended(cleaned):
        return 0
    return None


def extract_time_left_data(haystack: str) -> Tuple[str, Optional[int], str]:
    cleaned = normalize_numeric_text(haystack)
    if not cleaned:
        return "", None, ""

    for match in TIME_LEFT_CONTEXT_RE.finditer(cleaned):
        snippet = normalize_text(match.group(0))
        minutes = parse_time_left_to_minutes(snippet)
        if minutes is not None:
            return format_minutes_as_french_time(minutes, snippet), minutes, snippet

    for pattern in (JP_DURATION_RE, EN_DURATION_RE):
        match = pattern.search(cleaned)
        if match:
            raw = normalize_text(match.group(1))
            minutes = parse_time_left_to_minutes(raw)
            if minutes is not None:
                return format_minutes_as_french_time(minutes, raw), minutes, raw

    return "", None, ""


def format_minutes_as_french_time(minutes: Optional[int], raw_text: str = "") -> str:
    if minutes is None:
        return normalize_text(raw_text)
    if minutes <= 0:
        return "Terminé"

    lowered = normalize_numeric_text(raw_text).casefold()
    if ("ending soon" in lowered or "終了間近" in raw_text) and not (
        re.search(r"\d+\s*(?:日|時間|分|day|hour|minute|d\b|h\b|m\b)", lowered, re.IGNORECASE)
    ):
        return "Bientôt terminé"

    days, remainder = divmod(minutes, 1440)
    hours, mins = divmod(remainder, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days} jour" + ("s" if days > 1 else ""))
    if hours:
        parts.append(f"{hours} heure" + ("s" if hours > 1 else ""))
    if mins or not parts:
        parts.append(f"{mins} minute" + ("s" if mins > 1 else ""))
    return " ".join(parts)


def extract_time_left_text(haystack: str) -> str:
    time_left, _minutes, _raw = extract_time_left_data(haystack)
    return time_left


def extract_auction_end_at(haystack: str) -> str:
    cleaned = normalize_numeric_text(haystack)
    if not cleaned:
        return ""

    now = dt.datetime.now()
    for pattern in (END_TIME_RE, END_TIME_FALLBACK_RE):
        match = pattern.search(cleaned)
        if not match:
            continue
        year = int(match.group("year") or now.year)
        month = int(match.group("month"))
        day = int(match.group("day"))
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        try:
            return dt.datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return ""
    return ""


def get_auction_ending_soon_value(time_left_minutes: Optional[int]) -> str:
    if time_left_minutes is None:
        return AUCTION_ENDING_UNKNOWN
    if time_left_minutes <= 0:
        return AUCTION_ENDING_NO
    if time_left_minutes <= config.ENDING_VERY_SOON_MINUTES:
        return AUCTION_ENDING_VERY_SOON
    if time_left_minutes <= config.ENDING_SOON_MINUTES:
        return AUCTION_ENDING_SOON
    return AUCTION_ENDING_NO


def is_true_text(value: object) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "oui"}


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


def build_reader_url_from_target_url(target_url: str) -> str:
    cleaned = (target_url or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("https://", "").replace("http://", "")
    return f"{config.JINA_READER_PREFIX}{cleaned}"


def fetch_reader_content_verbose(
    session: requests.Session,
    reader_url: str,
    label: str,
    timeout_seconds: Optional[int] = None,
    retries: Optional[int] = None,
    retry_sleep_seconds: float = 1.2,
    backoff_multiplier: float = 1.0,
) -> Tuple[str, str]:
    if not reader_url:
        return "", "empty_reader_url"

    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    last_error = ""
    attempts_total = (config.REQUEST_RETRIES + 1) if retries is None else max(1, retries + 1)
    timeout_value = timeout_seconds or config.REQUEST_TIMEOUT_SECONDS
    for attempt in range(1, attempts_total + 1):
        try:
            response = session.get(reader_url, headers=headers, timeout=timeout_value)
            response.raise_for_status()
            return response.text, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logging.warning("Fetch failed for %s (attempt %s): %s", label, attempt, exc)
            if attempt < attempts_total:
                time.sleep(retry_sleep_seconds * max(1.0, backoff_multiplier ** (attempt - 1)))

    return "", last_error or "unknown_fetch_error"


def fetch_reader_content(session: requests.Session, reader_url: str, label: str) -> str:
    content, _ = fetch_reader_content_verbose(session, reader_url, label)
    return content


def fetch_markdown(session: requests.Session, query: str) -> str:
    url = build_yahoo_reader_url(query)
    content = fetch_reader_content(session, url, f"query '{query}'")
    if content:
        return content
    raise RuntimeError(f"Could not fetch query '{query}'")


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

    listing_type, listing_type_reason = detect_listing_type(listing.title, listing.context_text, listing.url)
    listing_metadata = extract_listing_metadata(listing.title, listing.context_text, listing.price_yen)
    current_price_yen = int(to_float(listing_metadata.get("current_price_yen", 0))) or None
    buy_now_price_yen = int(to_float(listing_metadata.get("buy_now_price_yen", 0))) or None
    effective_price_yen = select_primary_purchase_price(
        listing_type=listing_type,
        base_price_yen=listing.price_yen,
        current_price_yen=current_price_yen,
        buy_now_price_yen=buy_now_price_yen,
    )

    payment_fee_yen = int(round(effective_price_yen * config.ZENMARKET_PAYMENT_FEE_RATE))
    total_cost_yen = (
        effective_price_yen
        + config.ZENMARKET_SERVICE_FEE_YEN
        + payment_fee_yen
        + config.ESTIMATED_DOMESTIC_SHIPPING_YEN
        + config.ESTIMATED_INTERNATIONAL_SHIPPING_YEN
    )

    total_cost_eur = total_cost_yen / config.EUR_TO_JPY
    vat_eur = total_cost_eur * config.VAT_RATE
    landed_cost_eur = total_cost_eur + vat_eur
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
        price_yen=effective_price_yen,
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
        price_source="yahoo_search",
        current_price_yen=current_price_yen,
        buy_now_price_yen=buy_now_price_yen,
        bid_count=int(to_float(listing_metadata.get("bid_count", 0))) or None,
        time_left=listing_metadata.get("time_left", ""),
        time_left_minutes=int(to_float(listing_metadata.get("time_left_minutes", 0))) or None,
        auction_end_at=listing_metadata.get("auction_end_at", ""),
        auction_ending_soon=listing_metadata.get("auction_ending_soon", AUCTION_ENDING_UNKNOWN),
        time_left_source=listing_metadata.get("time_left_source", "yahoo_search"),
        raw_time_left_text=listing_metadata.get("raw_time_left_text", ""),
        auction_is_ended=is_true_text(listing_metadata.get("auction_is_ended", "False")),
        seller_name=listing_metadata.get("seller_name", ""),
        seller_rating=listing_metadata.get("seller_rating", ""),
        shipping_japan=listing_metadata.get("shipping_japan", ""),
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
            price_source TEXT NOT NULL DEFAULT 'unknown',
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
            current_price_yen INTEGER,
            buy_now_price_yen INTEGER,
            bid_count INTEGER,
            time_left TEXT NOT NULL DEFAULT '',
            time_left_minutes INTEGER,
            auction_end_at TEXT NOT NULL DEFAULT '',
            auction_ending_soon TEXT NOT NULL DEFAULT 'UNKNOWN',
            time_left_source TEXT NOT NULL DEFAULT 'unknown',
            raw_time_left_text TEXT NOT NULL DEFAULT '',
            auction_is_ended TEXT NOT NULL DEFAULT 'False',
            seller_name TEXT NOT NULL DEFAULT '',
            seller_rating TEXT NOT NULL DEFAULT '',
            shipping_japan TEXT NOT NULL DEFAULT '',
            detected_at TEXT NOT NULL
        )
        """
    )
    _ensure_db_column(conn, "deals", "listing_type", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
    _ensure_db_column(conn, "deals", "price_source", "TEXT NOT NULL DEFAULT 'unknown'")
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
    _ensure_db_column(conn, "deals", "current_price_yen", "INTEGER")
    _ensure_db_column(conn, "deals", "buy_now_price_yen", "INTEGER")
    _ensure_db_column(conn, "deals", "bid_count", "INTEGER")
    _ensure_db_column(conn, "deals", "time_left", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "time_left_minutes", "INTEGER")
    _ensure_db_column(conn, "deals", "auction_end_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "auction_ending_soon", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
    _ensure_db_column(conn, "deals", "time_left_source", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_db_column(conn, "deals", "raw_time_left_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "auction_is_ended", "TEXT NOT NULL DEFAULT 'False'")
    _ensure_db_column(conn, "deals", "seller_name", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "seller_rating", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "deals", "shipping_japan", "TEXT NOT NULL DEFAULT ''")
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detail_enrichment_cache (
            url TEXT PRIMARY KEY,
            last_checked_at TEXT NOT NULL,
            zenmarket_last_checked_at TEXT NOT NULL DEFAULT '',
            zenmarket_success TEXT NOT NULL DEFAULT 'False',
            zenmarket_error TEXT NOT NULL DEFAULT '',
            time_left TEXT NOT NULL DEFAULT '',
            time_left_minutes INTEGER,
            auction_end_at TEXT NOT NULL DEFAULT '',
            auction_is_ended TEXT NOT NULL DEFAULT 'False',
            time_left_source TEXT NOT NULL DEFAULT 'unknown',
            raw_time_left_text TEXT NOT NULL DEFAULT '',
            current_price_yen INTEGER,
            buy_now_price_yen INTEGER,
            bid_count INTEGER,
            price_source TEXT NOT NULL DEFAULT 'unknown',
            success TEXT NOT NULL DEFAULT 'False',
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _ensure_db_column(conn, "detail_enrichment_cache", "zenmarket_last_checked_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "detail_enrichment_cache", "zenmarket_success", "TEXT NOT NULL DEFAULT 'False'")
    _ensure_db_column(conn, "detail_enrichment_cache", "zenmarket_error", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "detail_enrichment_cache", "time_left_source", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_db_column(conn, "detail_enrichment_cache", "raw_time_left_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_db_column(conn, "detail_enrichment_cache", "price_source", "TEXT NOT NULL DEFAULT 'unknown'")
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
                price_source,
                market_price_eur, max_buy_price_yen, total_cost_yen, total_cost_eur, vat_eur,
                landed_cost_eur, safe_resale_eur, profit_eur, roi_percent,
                score, listing_type, listing_type_reason, matched_market_keyword,
                market_price_source, market_price_confidence, auto_price_used,
                auto_price_source, auto_price_sample_size, auto_price_raw_summary,
                auto_price_last_checked, current_price_yen, buy_now_price_yen,
                bid_count, time_left, time_left_minutes, auction_end_at, time_left_source, raw_time_left_text,
                auction_ending_soon, auction_is_ended, seller_name, seller_rating, shipping_japan, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal.listing_id,
                deal.title,
                deal.url,
                deal.query,
                deal.rule_name,
                deal.price_yen,
                deal.price_source,
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
                deal.current_price_yen,
                deal.buy_now_price_yen,
                deal.bid_count,
                deal.time_left,
                deal.time_left_minutes,
                deal.auction_end_at,
                deal.time_left_source,
                deal.raw_time_left_text,
                deal.auction_ending_soon,
                "True" if deal.auction_is_ended else "False",
                deal.seller_name,
                deal.seller_rating,
                deal.shipping_japan,
                deal.detected_at,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def refresh_existing_deal(conn: sqlite3.Connection, deal: ScoredDeal) -> None:
    conn.execute(
        """
        UPDATE deals
        SET title = ?,
            url = ?,
            query = ?,
            rule_name = ?,
            price_yen = ?,
            price_source = ?,
            market_price_eur = ?,
            max_buy_price_yen = ?,
            total_cost_yen = ?,
            total_cost_eur = ?,
            vat_eur = ?,
            landed_cost_eur = ?,
            safe_resale_eur = ?,
            profit_eur = ?,
            roi_percent = ?,
            score = ?,
            listing_type = ?,
            listing_type_reason = ?,
            matched_market_keyword = ?,
            market_price_source = ?,
            market_price_confidence = ?,
            auto_price_used = ?,
            auto_price_source = ?,
            auto_price_sample_size = ?,
            auto_price_raw_summary = ?,
            auto_price_last_checked = ?,
            current_price_yen = ?,
            buy_now_price_yen = ?,
            bid_count = ?,
            time_left = ?,
            time_left_minutes = ?,
            auction_end_at = ?,
            time_left_source = ?,
            raw_time_left_text = ?,
            auction_ending_soon = ?,
            auction_is_ended = ?,
            seller_name = ?,
            seller_rating = ?,
            shipping_japan = ?
        WHERE listing_id = ?
        """,
        (
            deal.title,
            deal.url,
            deal.query,
            deal.rule_name,
            deal.price_yen,
            deal.price_source,
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
            deal.current_price_yen,
            deal.buy_now_price_yen,
            deal.bid_count,
            deal.time_left,
            deal.time_left_minutes,
            deal.auction_end_at,
            deal.time_left_source,
            deal.raw_time_left_text,
            deal.auction_ending_soon,
            "True" if deal.auction_is_ended else "False",
            deal.seller_name,
            deal.seller_rating,
            deal.shipping_japan,
            deal.listing_id,
        ),
    )
    conn.commit()


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
        "price_source",
        "current_price_yen",
        "buy_now_price_yen",
        "bid_count",
        "time_left",
        "time_left_minutes",
        "auction_end_at",
        "auction_ending_soon",
        "time_left_source",
        "raw_time_left_text",
        "auction_is_ended",
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
                    row.get("price_source", ""),
                    row.get("current_price_yen", ""),
                    row.get("buy_now_price_yen", ""),
                    row.get("bid_count", ""),
                    row.get("time_left", ""),
                    row.get("time_left_minutes", ""),
                    row.get("auction_end_at", ""),
                    row.get("auction_ending_soon", ""),
                    row.get("time_left_source", ""),
                    row.get("raw_time_left_text", ""),
                    row.get("auction_is_ended", ""),
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
    parser.add_argument(
        "--force-detail-refresh",
        action="store_true",
        help="Ignore the detail enrichment cache and re-check final useful Yahoo listing pages.",
    )
    parser.add_argument(
        "--zenmarket-full-refresh",
        action="store_true",
        help="Retry ZenMarket aggressively on all Opportunités lines that are not yet backed by ZenMarket detail.",
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
    auction_is_ended: bool = False,
) -> str:
    if auction_is_ended:
        return DECISION_SKIP

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


def needs_price_priority(row: Dict[str, str]) -> int:
    title = row.get("title", "")
    risk_flags = row.get("risk_flags", "")
    matched_product = row.get("matched_product_japanese", "")
    if is_sealed_box_or_display(title, matched_product):
        return 0
    if "GOOD_CONDITION_LOOSE" in risk_flags:
        return 1
    if "OLD_BACK_LOT" in risk_flags:
        return 2
    return 3


def is_row_auction_ended(row: Dict[str, str]) -> bool:
    return is_true_text(row.get("auction_is_ended", "False"))


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

    base_price_yen = int(to_float(deal.get("price_yen", 0)))
    listing_metadata = extract_listing_metadata(title, context_text, base_price_yen)
    persisted_metadata = {
        "current_price_yen": str(int(to_float(deal.get("current_price_yen", 0))) or "") if deal.get("current_price_yen") not in (None, "") else "",
        "buy_now_price_yen": str(int(to_float(deal.get("buy_now_price_yen", 0))) or "") if deal.get("buy_now_price_yen") not in (None, "") else "",
        "bid_count": str(int(to_float(deal.get("bid_count", 0))) or "") if deal.get("bid_count") not in (None, "") else "",
        "time_left": str(deal.get("time_left", "") or "").strip(),
        "time_left_minutes": str(int(to_float(deal.get("time_left_minutes", 0))) or "") if deal.get("time_left_minutes") not in (None, "") else "",
        "auction_end_at": str(deal.get("auction_end_at", "") or "").strip(),
        "auction_ending_soon": str(deal.get("auction_ending_soon", "") or "").strip(),
        "time_left_source": str(deal.get("time_left_source", "") or "").strip(),
        "raw_time_left_text": str(deal.get("raw_time_left_text", "") or "").strip(),
        "auction_is_ended": str(deal.get("auction_is_ended", "") or "").strip(),
        "seller_name": str(deal.get("seller_name", "") or "").strip(),
        "seller_rating": str(deal.get("seller_rating", "") or "").strip(),
        "shipping_japan": str(deal.get("shipping_japan", "") or "").strip(),
    }
    for key, value in persisted_metadata.items():
        if value:
            listing_metadata[key] = value
    auction_is_ended = is_true_text(listing_metadata.get("auction_is_ended", "False")) or detect_auction_is_ended(
        listing_metadata.get("time_left", ""),
        context_text,
        title,
    )
    if listing_metadata.get("time_left") and not listing_metadata.get("time_left_minutes"):
        parsed_minutes = parse_time_left_to_minutes(listing_metadata.get("time_left", ""))
        if parsed_minutes is not None:
            listing_metadata["time_left_minutes"] = str(parsed_minutes)
    if auction_is_ended:
        listing_metadata["auction_is_ended"] = "True"
        listing_metadata["time_left"] = "Terminé"
        listing_metadata["time_left_minutes"] = "0"
        listing_metadata["auction_ending_soon"] = AUCTION_ENDING_ENDED
    elif listing_metadata.get("auction_ending_soon") in ("", AUCTION_ENDING_UNKNOWN):
        listing_metadata["auction_ending_soon"] = get_auction_ending_soon_value(
            int(to_float(listing_metadata.get("time_left_minutes", 0))) if listing_metadata.get("time_left_minutes") else None
        )
    else:
        listing_metadata["auction_is_ended"] = "False"
    if is_suspicious_time_left(
        listing_metadata.get("time_left", ""),
        listing_metadata.get("time_left_minutes", ""),
        listing_metadata.get("time_left_source", ""),
        listing_metadata.get("raw_time_left_text", ""),
    ):
        listing_metadata = clear_time_left_fields(listing_metadata, source="unknown")

    current_price_yen = parse_optional_int(listing_metadata.get("current_price_yen", ""))
    buy_now_price_yen = parse_optional_int(listing_metadata.get("buy_now_price_yen", ""))
    if listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE) and buy_now_price_yen and not current_price_yen:
        current_price_yen = buy_now_price_yen
        listing_metadata["current_price_yen"] = str(buy_now_price_yen)

    price_yen = select_primary_purchase_price(
        listing_type=listing_type,
        base_price_yen=base_price_yen,
        current_price_yen=current_price_yen,
        buy_now_price_yen=buy_now_price_yen,
    )
    opportunity_type = compute_opportunity_type(listing_type, buy_now_price_yen)
    price_source = choose_price_source(deal, listing_metadata, fallback="yahoo_search")
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
        auction_is_ended=auction_is_ended,
    )
    score_reliability = get_score_reliability(listing_type, market_price_confidence, decision)
    manual_action_needed = compute_manual_action_needed(
        manual_market_price_eur=manual_market_price_eur,
        manual_status=manual_status,
    )
    if auction_is_ended:
        manual_action_needed = ""
    risk_flags = build_risk_flags(
        title=title,
        context_text=context_text,
        listing_type=listing_type,
        decision=decision,
        manual_market_price_eur=manual_market_price_eur,
        seller_rating=listing_metadata.get("seller_rating", ""),
        auction_is_ended=auction_is_ended,
    )
    forced_skip_flags = {"GRADED", "DAMAGED", "OLD_CARD_SINGLE", "MYSTERY_PACK", "SEARCHED_PACK"}
    if decision not in (DECISION_IGNORE,) and any(flag in risk_flags for flag in forced_skip_flags):
        decision = DECISION_SKIP
    if auction_is_ended:
        decision = DECISION_SKIP
    deal_quality_score = compute_deal_quality_score(
        listing_type=listing_type,
        matched_product_japanese=matched_product_japanese,
        manual_market_price_eur=manual_market_price_eur,
        title=title,
        risk_flags=risk_flags,
        decision=decision,
    )
    score_reliability = get_score_reliability(listing_type, market_price_confidence, decision)
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
        "opportunity_type": opportunity_type,
        "price_yen": str(price_yen or ""),
        "price_source": price_source,
        "current_price_yen": listing_metadata.get("current_price_yen", ""),
        "buy_now_price_yen": listing_metadata.get("buy_now_price_yen", ""),
        "bid_count": listing_metadata.get("bid_count", ""),
        "time_left": listing_metadata.get("time_left", ""),
        "time_left_minutes": listing_metadata.get("time_left_minutes", ""),
        "auction_end_at": listing_metadata.get("auction_end_at", ""),
        "auction_ending_soon": listing_metadata.get("auction_ending_soon", AUCTION_ENDING_UNKNOWN),
        "time_left_source": listing_metadata.get("time_left_source", "unknown"),
        "raw_time_left_text": listing_metadata.get("raw_time_left_text", ""),
        "auction_is_ended": "True" if auction_is_ended else "False",
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
        filtered = [
            row
            for row in rows
            if row.get("decision") not in (DECISION_SKIP, DECISION_IGNORE) and not is_row_auction_ended(row)
        ]

    def auction_time_sort_key(row: Dict[str, str]) -> tuple:
        listing_type = row.get("listing_type", LISTING_TYPE_UNKNOWN)
        if listing_type not in (LISTING_TYPE_AUCTION, LISTING_TYPE_LOW_START_AUCTION):
            return (1, 10**9)
        minutes = row.get("time_left_minutes", "")
        if minutes in ("", None):
            return (0, 10**9)
        return (0, int(to_float(minutes)))

    def sort_key(row: Dict[str, str]) -> tuple:
        decision_part = decision_rank(row.get("decision", DECISION_SKIP))
        roi_part = -to_float(row.get("roi_percent", 0.0))
        profit_part = -to_float(row.get("profit_eur", 0.0))
        auction_time_part = auction_time_sort_key(row)
        if prefer_buy_now:
            type_part = listing_type_rank(row.get("listing_type", LISTING_TYPE_UNKNOWN))
            return (decision_part, type_part, auction_time_part, roi_part, profit_part)
        return (decision_part, auction_time_part, roi_part, profit_part)

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
        if not is_row_auction_ended(row)
        if row.get("decision") in (
            DECISION_BUY_ALERT,
            DECISION_WATCH,
            DECISION_WATCH_AUCTION,
            DECISION_WATCH_LOW_AUCTION,
            DECISION_NEEDS_PRICE,
        )
    ]

    def strategic_rank(row: Dict[str, str]) -> int:
        title = row.get("title", "")
        matched_product = row.get("matched_product_japanese", "")
        risk_flags = str(row.get("risk_flags", "") or "")
        if is_sealed_box_or_display(title, matched_product):
            return 0
        if "GOOD_CONDITION_LOOSE" in risk_flags:
            return 1
        if "OLD_BACK_LOT" in risk_flags:
            return 2
        return 3

    def sort_key(row: Dict[str, str]) -> tuple:
        manual_price = to_float(row.get("manual_market_price_eur", ""))
        roi = to_float(row.get("roi_percent", 0.0))
        profit = to_float(row.get("profit_eur", 0.0))
        minutes_raw = row.get("time_left_minutes", "")
        minutes_known = minutes_raw not in ("", None)
        minutes_value = int(to_float(minutes_raw)) if minutes_known else 10**9
        return (
            decision_rank(row.get("decision", DECISION_SKIP)),
            0 if manual_price > 0 and roi > 0 else 1,
            strategic_rank(row),
            0 if minutes_known else 1,
            minutes_value,
            -roi,
            -profit,
        )

    best_rows.sort(key=sort_key)
    return best_rows


def sort_buy_now_deals_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    filtered = [
        row
        for row in rows
        if not is_row_auction_ended(row)
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
        if not is_row_auction_ended(row)
        if row.get("decision") in (DECISION_WATCH_AUCTION, DECISION_WATCH_LOW_AUCTION)
    ]

    def sort_key(row: Dict[str, str]) -> tuple:
        minutes_raw = row.get("time_left_minutes", "")
        minutes_known = minutes_raw not in ("", None)
        minutes_value = int(to_float(minutes_raw)) if minutes_known else 10**9
        return (
            0 if minutes_known else 1,
            minutes_value,
            -to_float(row.get("roi_percent", 0.0)),
            -to_float(row.get("profit_eur", 0.0)),
        )

    filtered.sort(key=sort_key)
    return filtered


def sort_needs_price_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    filtered = [
        row for row in rows if row.get("decision") == DECISION_NEEDS_PRICE and not is_row_auction_ended(row)
    ]

    def sort_key(row: Dict[str, str]) -> tuple:
        return (
            needs_price_priority(row),
            -to_float(row.get("deal_quality_score", 0.0)),
            -to_float(row.get("roi_percent", 0.0)),
            -to_float(row.get("profit_eur", 0.0)),
        )

    filtered.sort(key=sort_key)
    return filtered


def merge_listing_metadata_dicts(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    merged = dict(base)
    for key, value in extra.items():
        if value not in ("", None):
            merged[key] = value
    return merged


def persist_listing_metadata(conn: sqlite3.Connection, listing_id: str, metadata: Dict[str, str]) -> None:
    if not listing_id:
        return
    conn.execute(
        """
        UPDATE deals
        SET price_yen = ?,
            price_source = ?,
            current_price_yen = ?,
            buy_now_price_yen = ?,
            bid_count = ?,
            time_left = ?,
            time_left_minutes = ?,
            auction_end_at = ?,
            auction_ending_soon = ?,
            time_left_source = ?,
            raw_time_left_text = ?,
            auction_is_ended = ?,
            seller_name = ?,
            seller_rating = ?,
            shipping_japan = ?
        WHERE listing_id = ?
        """,
        (
            int(to_float(metadata.get("price_yen", 0))) or None,
            normalize_price_source(metadata.get("price_source", "unknown")),
            int(to_float(metadata.get("current_price_yen", 0))) or None,
            int(to_float(metadata.get("buy_now_price_yen", 0))) or None,
            int(to_float(metadata.get("bid_count", 0))) or None,
            metadata.get("time_left", ""),
            int(to_float(metadata.get("time_left_minutes", 0))) if metadata.get("time_left_minutes", "") not in ("", None) else None,
            metadata.get("auction_end_at", ""),
            metadata.get("auction_ending_soon", AUCTION_ENDING_UNKNOWN),
            metadata.get("time_left_source", "unknown"),
            metadata.get("raw_time_left_text", ""),
            metadata.get("auction_is_ended", "False"),
            metadata.get("seller_name", ""),
            metadata.get("seller_rating", ""),
            metadata.get("shipping_japan", ""),
            listing_id,
        ),
    )
    conn.commit()


def parse_optional_int(value: object) -> Optional[int]:
    if value in ("", None):
        return None
    parsed = int(to_float(value))
    return parsed if parsed != 0 else 0


def normalize_price_source(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = REVERSE_DISPLAY_VALUE_MAPS.get("price_source", {}).get(cleaned, cleaned)
    if cleaned in {"zenmarket_detail", "yahoo_detail", "yahoo_search", "sqlite_cache", "unknown"}:
        return cleaned
    return "unknown"


def to_optional_int_str(value: object) -> str:
    if value in ("", None):
        return ""
    return str(int(to_float(value)))


def select_primary_purchase_price(
    listing_type: str,
    base_price_yen: int,
    current_price_yen: Optional[int],
    buy_now_price_yen: Optional[int],
) -> int:
    current_value = current_price_yen or 0
    buy_now_value = buy_now_price_yen or 0
    if listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE):
        if buy_now_value > 0:
            return buy_now_value
        if current_value > 0:
            return current_value
    if listing_type in (LISTING_TYPE_AUCTION, LISTING_TYPE_LOW_START_AUCTION):
        if current_value > 0:
            return current_value
    if base_price_yen > 0:
        return base_price_yen
    if current_value > 0:
        return current_value
    if buy_now_value > 0:
        return buy_now_value
    return 0


def choose_price_source(deal: Dict[str, object], metadata: Dict[str, str], fallback: str = "unknown") -> str:
    stored = normalize_price_source(str(deal.get("price_source", "") or ""))
    if stored != "unknown":
        return stored
    if metadata.get("current_price_yen") or metadata.get("buy_now_price_yen"):
        return "sqlite_cache"
    if int(to_float(deal.get("price_yen", 0))) > 0:
        return fallback
    return "unknown"


def parse_zenmarket_datetime(value: str) -> Optional[dt.datetime]:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"):
        try:
            return dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def format_duration_from_minutes(total_minutes: int) -> str:
    if total_minutes <= 0:
        return "Terminé"
    days, remainder = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(remainder, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 3:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else f"{total_minutes}m"


def is_meaningful_detail_metadata(metadata: Dict[str, str]) -> bool:
    return any(
        metadata.get(field)
        for field in (
            "current_price_yen",
            "buy_now_price_yen",
            "bid_count",
            "time_left",
            "time_left_minutes",
            "auction_end_at",
        )
    ) or metadata.get("auction_is_ended", "False") == "True"


def is_suspicious_time_left(
    time_left: str,
    time_left_minutes: object,
    time_left_source: str = "",
    raw_time_left_text: str = "",
) -> bool:
    normalized_time = normalize_numeric_text(time_left or "").casefold()
    normalized_raw = normalize_numeric_text(raw_time_left_text or "").casefold()
    source = normalize_price_source(time_left_source or "unknown")
    minutes = parse_optional_int(time_left_minutes)
    if minutes is None:
        return False
    if minutes == 12960 or normalized_time in {"9 jours", "9 days", "9 day"}:
        return source in {"unknown", "yahoo_search", "sqlite_cache"} or not normalized_raw
    if minutes >= 7 * 1440 and source in {"unknown", "yahoo_search", "sqlite_cache"}:
        return True
    return False


def clear_time_left_fields(metadata: Dict[str, str], source: str = "unknown") -> Dict[str, str]:
    cleared = dict(metadata)
    cleared["time_left"] = ""
    cleared["time_left_minutes"] = ""
    cleared["auction_ending_soon"] = AUCTION_ENDING_UNKNOWN
    cleared["auction_end_at"] = ""
    cleared["time_left_source"] = source
    return cleared


def parse_zenmarket_listing_metadata(text: str) -> Dict[str, str]:
    haystack = combine_context(text)

    def _extract(pattern: re.Pattern[str]) -> str:
        match = pattern.search(haystack)
        if not match:
            return ""
        return match.group(1).replace(",", "").strip()

    current_price = _extract(ZENMARKET_CURRENT_PRICE_RE)
    buy_now_price = _extract(ZENMARKET_BUY_NOW_PRICE_RE)
    bid_count = _extract(ZENMARKET_BID_COUNT_RE)
    time_left = ""
    time_left_match = ZENMARKET_TIME_LEFT_RE.search(haystack)
    if time_left_match:
        time_left = normalize_text(time_left_match.group(1))

    end_match = ZENMARKET_ENDS_AT_RE.search(haystack)
    now_match = ZENMARKET_CURRENT_TIME_RE.search(haystack)
    end_dt = parse_zenmarket_datetime(end_match.group(1)) if end_match else None
    current_dt = parse_zenmarket_datetime(now_match.group(1)) if now_match else None

    if not time_left and end_dt and current_dt:
        diff_minutes = max(0, int((end_dt - current_dt).total_seconds() // 60))
        time_left = format_duration_from_minutes(diff_minutes)

    normalized_time = normalize_numeric_text(time_left)
    explicit_ended = "this auction ends in auction ended" in normalize_numeric_text(haystack).casefold()
    auction_is_ended = explicit_ended or normalized_time.casefold() == "auction ended"
    if not auction_is_ended and end_dt and current_dt and end_dt <= current_dt:
        auction_is_ended = True

    if auction_is_ended:
        time_left = "Terminé"
        time_left_minutes = 0
        auction_ending_soon = AUCTION_ENDING_ENDED
    else:
        time_left_minutes = parse_time_left_to_minutes(time_left) if time_left else None
        if time_left_minutes is None and end_dt and current_dt:
            time_left_minutes = max(0, int((end_dt - current_dt).total_seconds() // 60))
            if not time_left:
                time_left = format_duration_from_minutes(time_left_minutes)
        auction_ending_soon = get_auction_ending_soon_value(time_left_minutes)

    shipping_match = ZENMARKET_SHIPPING_RE.search(haystack)
    shipping_japan = shipping_match.group(1).strip() if shipping_match else ""
    if shipping_japan.casefold() == "free":
        shipping_japan = "送料無料"

    if not current_price and buy_now_price:
        current_price = buy_now_price

    return {
        "current_price_yen": current_price,
        "buy_now_price_yen": buy_now_price,
        "bid_count": bid_count,
        "time_left": time_left,
        "time_left_minutes": str(time_left_minutes) if time_left_minutes is not None else "",
        "auction_end_at": end_dt.strftime("%Y-%m-%d %H:%M:%S JST") if end_dt else "",
        "auction_ending_soon": auction_ending_soon,
        "time_left_source": "zenmarket_detail" if (time_left or time_left_minutes is not None or auction_is_ended) else "unknown",
        "raw_time_left_text": time_left,
        "auction_is_ended": "True" if auction_is_ended else "False",
        "shipping_japan": shipping_japan,
    }


def fetch_zenmarket_detail(
    session: requests.Session,
    yahoo_url: str,
    timeout_seconds: Optional[int] = None,
) -> Tuple[Dict[str, str], str, bool, str]:
    zenmarket_url = build_zenmarket_auction_url(yahoo_url)
    if not zenmarket_url:
        return {}, "", False, "missing_zenmarket_url"
    detail_markdown, fetch_error = fetch_reader_content_verbose(
        session,
        build_reader_url_from_target_url(zenmarket_url),
        f"zenmarket detail page '{zenmarket_url}'",
        timeout_seconds=timeout_seconds or config.ZENMARKET_TIMEOUT_SECONDS,
        retries=0,
    )
    if not detail_markdown:
        return {}, zenmarket_url, False, fetch_error or "empty_zenmarket_response"
    metadata = parse_zenmarket_listing_metadata(detail_markdown)
    success = is_meaningful_detail_metadata(metadata)
    return metadata, zenmarket_url, success, "" if success else "zenmarket_no_relevant_data"


def load_detail_enrichment_cache_entry(conn: sqlite3.Connection, url: str) -> Optional[Dict[str, object]]:
    if not url:
        return None
    cursor = conn.execute(
        """
        SELECT url, last_checked_at, zenmarket_last_checked_at, zenmarket_success, zenmarket_error,
               time_left, time_left_minutes, auction_end_at, auction_is_ended, time_left_source, raw_time_left_text,
               current_price_yen, buy_now_price_yen, bid_count, price_source, success, error
        FROM detail_enrichment_cache
        WHERE url = ?
        """,
        (url,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    columns = [
        "url",
        "last_checked_at",
        "zenmarket_last_checked_at",
        "zenmarket_success",
        "zenmarket_error",
        "time_left",
        "time_left_minutes",
        "auction_end_at",
        "auction_is_ended",
        "time_left_source",
        "raw_time_left_text",
        "current_price_yen",
        "buy_now_price_yen",
        "bid_count",
        "price_source",
        "success",
        "error",
    ]
    return dict(zip(columns, row))


def is_detail_cache_recent(cache_entry: Optional[Dict[str, object]]) -> bool:
    if not cache_entry:
        return False
    zenmarket_checked_at = str(cache_entry.get("zenmarket_last_checked_at", "") or "").strip()
    if not zenmarket_checked_at:
        return False
    try:
        checked_at = dt.datetime.fromisoformat(zenmarket_checked_at)
    except ValueError:
        return False
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=config.FINAL_DEALS_DOUBLE_CHECK_CACHE_HOURS)
    return checked_at >= cutoff


def is_true_like(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def can_skip_detail_refresh_from_cache(
    cache_entry: Optional[Dict[str, object]],
    current_price_source: str,
) -> bool:
    if not is_detail_cache_recent(cache_entry):
        return False
    if normalize_price_source(current_price_source) != "zenmarket_detail":
        return False
    if normalize_price_source(str((cache_entry or {}).get("price_source", "") or "")) != "zenmarket_detail":
        return False
    return is_true_like((cache_entry or {}).get("zenmarket_success", False))


def upsert_detail_enrichment_cache(
    conn: sqlite3.Connection,
    url: str,
    metadata: Dict[str, str],
    success: bool,
    zenmarket_attempted: bool = False,
    zenmarket_success: bool = False,
    zenmarket_error: str = "",
    error: str = "",
) -> None:
    if not url:
        return
    conn.execute(
        """
        INSERT INTO detail_enrichment_cache (
            url, last_checked_at, zenmarket_last_checked_at, zenmarket_success, zenmarket_error,
            time_left, time_left_minutes, auction_end_at, auction_is_ended, time_left_source, raw_time_left_text,
            current_price_yen, buy_now_price_yen, bid_count, price_source, success, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            last_checked_at = excluded.last_checked_at,
            zenmarket_last_checked_at = excluded.zenmarket_last_checked_at,
            zenmarket_success = excluded.zenmarket_success,
            zenmarket_error = excluded.zenmarket_error,
            time_left = excluded.time_left,
            time_left_minutes = excluded.time_left_minutes,
            auction_end_at = excluded.auction_end_at,
            auction_is_ended = excluded.auction_is_ended,
            time_left_source = excluded.time_left_source,
            raw_time_left_text = excluded.raw_time_left_text,
            current_price_yen = excluded.current_price_yen,
            buy_now_price_yen = excluded.buy_now_price_yen,
            bid_count = excluded.bid_count,
            price_source = excluded.price_source,
            success = excluded.success,
            error = excluded.error
        """,
        (
            url,
            utc_now_iso(),
            utc_now_iso() if zenmarket_attempted else "",
            "True" if zenmarket_success else "False",
            (zenmarket_error or "")[:500],
            metadata.get("time_left", ""),
            parse_optional_int(metadata.get("time_left_minutes", "")),
            metadata.get("auction_end_at", ""),
            metadata.get("auction_is_ended", "False"),
            metadata.get("time_left_source", "unknown"),
            metadata.get("raw_time_left_text", ""),
            parse_optional_int(metadata.get("current_price_yen", "")),
            parse_optional_int(metadata.get("buy_now_price_yen", "")),
            parse_optional_int(metadata.get("bid_count", "")),
            normalize_price_source(metadata.get("price_source", "unknown")),
            "True" if success else "False",
            (error or "")[:500],
        ),
    )
    conn.commit()


def append_detail_enrichment_log(rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    headers = [
        "run_at",
        "mode",
        "title",
        "url",
        "zenmarket_url",
        "zenmarket_attempted",
        "zenmarket_success",
        "zenmarket_retry_count",
        "zenmarket_error",
        "zenmarket_retry_needed",
        "fallback_used",
        "old_price_source",
        "new_price_source",
        "old_price_yen",
        "new_price_yen",
        "old_time_left",
        "new_time_left",
        "old_time_left_minutes",
        "new_time_left_minutes",
        "time_left_source",
        "raw_time_left_text",
        "suspicious_time_left",
        "cleared_suspicious_time_left",
        "old_auction_is_ended",
        "new_auction_is_ended",
        "old_current_price_yen",
        "new_current_price_yen",
        "old_buy_now_price_yen",
        "new_buy_now_price_yen",
        "old_bid_count",
        "new_bid_count",
        "updated",
        "skipped_by_cache",
        "error",
    ]
    write_mode = "a"
    if os.path.exists(DETAIL_ENRICHMENT_LOG_PATH):
        with open(DETAIL_ENRICHMENT_LOG_PATH, "r", encoding="utf-8", newline="") as existing_handle:
            first_line = existing_handle.readline().strip()
        if first_line != ",".join(headers):
            write_mode = "w"
    else:
        write_mode = "w"
    with open(DETAIL_ENRICHMENT_LOG_PATH, write_mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        if write_mode == "w":
            writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})


def row_has_excluded_double_check_risk(row: Dict[str, str]) -> bool:
    risk_flags = str(row.get("risk_flags", "") or "")
    return any(flag in risk_flags for flag in DOUBLE_CHECK_EXCLUDED_RISK_FLAGS)


def is_final_useful_candidate_row(row: Dict[str, str]) -> bool:
    decision = row.get("decision", "")
    if decision not in FINAL_USEFUL_DECISIONS:
        return False
    if decision in (DECISION_SKIP, DECISION_IGNORE):
        return False
    if is_row_auction_ended(row):
        return False
    if row_has_excluded_double_check_risk(row):
        return False
    url = str(row.get("url", "")).strip()
    if not extract_yahoo_auction_id(url):
        return False
    if decision == DECISION_NEEDS_PRICE:
        title = row.get("title", "")
        matched_product = row.get("matched_product_japanese", "")
        risk_flags = str(row.get("risk_flags", "") or "")
        if not (
            is_sealed_box_or_display(title, matched_product)
            or "GOOD_CONDITION_LOOSE" in risk_flags
            or "OLD_BACK_LOT" in risk_flags
        ):
            return False
    return True


def final_deal_double_check_priority(row: Dict[str, str]) -> tuple:
    decision = row.get("decision", "")
    risk_flags = str(row.get("risk_flags", "") or "")
    title = row.get("title", "")
    matched_product = row.get("matched_product_japanese", "")
    manual_price_filled = to_float(row.get("manual_market_price_eur", "")) > 0
    time_unknown = row.get("time_left_minutes", "") in ("", None)
    listing_type = row.get("listing_type", LISTING_TYPE_UNKNOWN)
    is_auction_type = listing_type in (LISTING_TYPE_AUCTION, LISTING_TYPE_LOW_START_AUCTION)
    is_box = is_sealed_box_or_display(title, matched_product)
    is_good_loose = "GOOD_CONDITION_LOOSE" in risk_flags
    is_old_back_lot = "OLD_BACK_LOT" in risk_flags
    group_rank = 3
    if decision in (DECISION_WATCH_AUCTION, DECISION_WATCH_LOW_AUCTION):
        group_rank = 0
    elif decision == DECISION_NEEDS_PRICE and (is_box or is_good_loose or is_old_back_lot):
        group_rank = 1
    elif manual_price_filled:
        group_rank = 2
    return (
        group_rank,
        0 if time_unknown else 1,
        0 if manual_price_filled else 1,
        0 if is_auction_type else 1,
        0 if is_box else 1,
        0 if is_good_loose else 1,
        0 if is_old_back_lot else 1,
        -to_float(row.get("deal_quality_score", 0.0)),
        to_float(row.get("price_yen", 0.0)),
    )


def row_has_possible_buy_now_signal(row: Dict[str, str]) -> bool:
    listing_type = row.get("listing_type", LISTING_TYPE_UNKNOWN)
    if listing_type in (LISTING_TYPE_BUY_NOW, LISTING_TYPE_FIXED_PRICE):
        return True
    haystack = combine_context(
        row.get("title", ""),
        row.get("query", ""),
        row.get("rule_name", ""),
        row.get("matched_product_japanese", ""),
    )
    buy_now_keywords = list(config.LISTING_TYPE_KEYWORDS.get("buy_now", []))
    fixed_price_keywords = list(config.LISTING_TYPE_KEYWORDS.get("fixed_price", []))
    return any(keyword and keyword in haystack for keyword in buy_now_keywords + fixed_price_keywords)


def best_deals_refresh_priority(row: Dict[str, str], is_best_row: bool) -> tuple:
    current_source = normalize_price_source(row.get("price_source", "") or "")
    missing_buy_now = to_float(row.get("buy_now_price_yen", "")) <= 0
    time_unknown = row.get("time_left_minutes", "") in ("", None)
    if is_best_row:
        if current_source != "zenmarket_detail":
            primary_rank = 0
        elif missing_buy_now and row_has_possible_buy_now_signal(row):
            primary_rank = 1
        elif time_unknown:
            primary_rank = 2
        else:
            primary_rank = 3
        return (primary_rank,) + final_deal_double_check_priority(row)
    return (4,) + final_deal_double_check_priority(row)


def should_retry_zenmarket_for_best_row(
    row: Dict[str, str],
    cache_entry: Optional[Dict[str, object]],
    force_refresh: bool,
) -> bool:
    if not config.ZENMARKET_REQUIRED_FOR_BEST_DEALS:
        return False
    if force_refresh:
        return True
    if not str(row.get("link_for_zenmarket", "") or "").strip():
        return False
    current_source = normalize_price_source(row.get("price_source", "") or "")
    if current_source != "zenmarket_detail":
        return True
    return not can_skip_detail_refresh_from_cache(cache_entry, current_source)


def fetch_zenmarket_detail_with_retries(
    session: requests.Session,
    yahoo_url: str,
    max_retries: int,
    deadline_monotonic: Optional[float] = None,
) -> Tuple[Dict[str, str], str, bool, str, int, int]:
    zenmarket_url = build_zenmarket_auction_url(yahoo_url)
    if not zenmarket_url:
        return {}, "", False, "missing_zenmarket_url", 0, 0

    attempts = 0
    retry_count = 0
    last_error = ""
    last_metadata: Dict[str, str] = {}

    for attempt_index in range(max(1, max_retries)):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            last_error = "zenmarket_time_budget_exceeded"
            break
        attempts += 1
        metadata, returned_url, success, error = fetch_zenmarket_detail(
            session,
            yahoo_url,
            timeout_seconds=config.ZENMARKET_TIMEOUT_SECONDS,
        )
        if returned_url:
            zenmarket_url = returned_url
        if success:
            return metadata, zenmarket_url, True, "", retry_count, attempts

        last_metadata = metadata
        last_error = error or "zenmarket_unknown_error"
        if attempt_index >= max_retries - 1:
            break
        retry_count += 1
        sleep_seconds = config.ZENMARKET_SLEEP_BETWEEN_RETRIES_SECONDS * (
            max(1.0, config.ZENMARKET_BACKOFF_MULTIPLIER ** attempt_index)
        )
        if deadline_monotonic is not None:
            remaining_seconds = deadline_monotonic - time.monotonic()
            if remaining_seconds <= 0:
                last_error = "zenmarket_time_budget_exceeded"
                break
            sleep_seconds = min(sleep_seconds, max(0.0, remaining_seconds))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return last_metadata, zenmarket_url, False, last_error or "zenmarket_retries_exhausted", retry_count, attempts


def double_check_final_deals_details(
    session: requests.Session,
    conn: sqlite3.Connection,
    deals: List[Dict[str, object]],
    existing_sheet_data: Optional[Dict[str, Dict[str, str]]] = None,
    force_refresh: bool = False,
    zenmarket_full_refresh: bool = False,
) -> Dict[str, int]:
    stats = {
        "candidates": 0,
        "attempted": 0,
        "updated": 0,
        "skipped_cache": 0,
        "errors": 0,
        "ended_detected": 0,
        "time_updated": 0,
        "price_updated": 0,
        "bid_updated": 0,
        "suspicious_detected": 0,
        "suspicious_cleared": 0,
        "zenmarket_attempted": 0,
        "zenmarket_success": 0,
        "zenmarket_errors": 0,
        "zenmarket_current_price_updated": 0,
        "zenmarket_buy_now_updated": 0,
        "fallback_yahoo_used": 0,
        "price_source_zenmarket_detail": 0,
        "price_source_yahoo_detail": 0,
        "price_source_yahoo_search": 0,
        "price_source_sqlite_cache": 0,
        "price_source_unknown": 0,
        "best_lines_total": 0,
        "best_lines_buy_now_price": 0,
        "best_lines_auction_plus_buy_now": 0,
        "best_lines_time_9_days": 0,
        "best_lines_minutes_12960": 0,
        "best_time_source_zenmarket": 0,
        "best_time_source_yahoo": 0,
        "best_time_source_unknown": 0,
        "best_without_zenmarket_link": 0,
        "best_zenmarket_attempted": 0,
        "best_zenmarket_success": 0,
        "best_zenmarket_errors": 0,
        "best_already_zenmarket": 0,
        "best_to_retry_zenmarket": 0,
        "best_non_zenmarket_after_run": 0,
        "zenmarket_retries": 0,
        "zenmarket_fail_after_retries": 0,
        "zenmarket_duration_seconds": 0,
    }
    if not config.DETAIL_ENRICHMENT_ENABLED or not config.FINAL_DEALS_DOUBLE_CHECK_ENABLED:
        return stats

    zenmarket_run_started_at = time.monotonic()
    zenmarket_max_minutes = 90 if zenmarket_full_refresh else config.ZENMARKET_MAX_TOTAL_MINUTES_PER_RUN
    zenmarket_deadline = zenmarket_run_started_at + (zenmarket_max_minutes * 60)

    existing_sheet_data = existing_sheet_data or {}
    base_rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=existing_sheet_data.get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]
    best_rows_all = sort_best_deals_rows(base_rows)
    best_urls = {str(row.get("url", "")).strip() for row in best_rows_all if str(row.get("url", "")).strip()}
    deal_by_url = {str(deal.get("url", "")).strip(): deal for deal in deals if str(deal.get("url", "")).strip()}
    candidates: List[Tuple[Dict[str, str], Dict[str, object]]] = []
    seen_urls = set()
    for row in base_rows:
        url = str(row.get("url", "")).strip()
        if not url or url in seen_urls:
            continue
        is_best_row = url in best_urls
        if is_best_row:
            if is_row_auction_ended(row):
                continue
            if not extract_yahoo_auction_id(url):
                continue
        elif not is_final_useful_candidate_row(row):
            continue
        deal = deal_by_url.get(url)
        if not deal:
            continue
        candidates.append((row, deal))
        seen_urls.add(url)

    if config.BEST_DEALS_ZENMARKET_REFRESH_ENABLED:
        candidates.sort(key=lambda pair: best_deals_refresh_priority(pair[0], str(pair[0].get("url", "")).strip() in best_urls))
    else:
        candidates.sort(key=lambda pair: final_deal_double_check_priority(pair[0]))
    max_candidates = config.FINAL_DEALS_DOUBLE_CHECK_MAX_PAGES
    if config.BEST_DEALS_ZENMARKET_REFRESH_ENABLED:
        max_candidates = max(max_candidates, config.BEST_DEALS_ZENMARKET_REFRESH_MAX_PAGES)
    if zenmarket_full_refresh:
        max_candidates = max(max_candidates, len(best_rows_all))
    if len(candidates) > max_candidates:
        candidates = candidates[:max_candidates]
    stats["candidates"] = len(candidates)

    run_at = utc_now_iso()
    mode = "zenmarket_full_refresh" if zenmarket_full_refresh else "force_detail_refresh" if force_refresh else "normal"
    log_rows: List[Dict[str, str]] = []
    consecutive_errors = 0
    best_retry_candidates = 0
    best_already_zenmarket = 0
    best_non_zenmarket_after_run = 0
    best_urls_in_candidates = {str(row.get("url", "")).strip() for row, _deal in candidates if str(row.get("url", "")).strip() in best_urls}

    for index, (row, deal) in enumerate(candidates, start=1):
        if time.monotonic() >= zenmarket_deadline:
            logging.warning("ZenMarket detail enrichment stopped after reaching the run time budget (%s minutes).", zenmarket_max_minutes)
            break

        url = str(row.get("url", "")).strip()
        is_best_row = url in best_urls
        row_sleep_seconds = config.FINAL_DEALS_DOUBLE_CHECK_SLEEP_SECONDS
        if is_best_row and config.ZENMARKET_REQUIRED_FOR_BEST_DEALS:
            row_sleep_seconds = config.ZENMARKET_SLEEP_BETWEEN_ITEMS_SECONDS
        elif is_best_row and config.BEST_DEALS_ZENMARKET_REFRESH_ENABLED:
            row_sleep_seconds = config.BEST_DEALS_ZENMARKET_REFRESH_SLEEP_SECONDS
        title = str(deal.get("title", "") or row.get("title", ""))
        current_metadata = {
            "price_yen": str(deal.get("price_yen", "") or ""),
            "price_source": str(deal.get("price_source", "") or ""),
            "current_price_yen": str(deal.get("current_price_yen", "") or ""),
            "buy_now_price_yen": str(deal.get("buy_now_price_yen", "") or ""),
            "bid_count": str(deal.get("bid_count", "") or ""),
            "time_left": str(deal.get("time_left", "") or ""),
            "time_left_minutes": str(deal.get("time_left_minutes", "") or ""),
            "auction_end_at": str(deal.get("auction_end_at", "") or ""),
            "auction_ending_soon": str(deal.get("auction_ending_soon", "") or ""),
            "time_left_source": str(deal.get("time_left_source", "") or ""),
            "raw_time_left_text": str(deal.get("raw_time_left_text", "") or ""),
            "auction_is_ended": str(deal.get("auction_is_ended", "") or ""),
            "seller_name": str(deal.get("seller_name", "") or ""),
            "seller_rating": str(deal.get("seller_rating", "") or ""),
            "shipping_japan": str(deal.get("shipping_japan", "") or ""),
        }
        log_entry = {
            "run_at": run_at,
            "mode": mode,
            "url": url,
            "zenmarket_url": build_zenmarket_auction_url(url),
            "zenmarket_attempted": "False",
            "zenmarket_success": "False",
            "zenmarket_retry_count": "0",
            "zenmarket_error": "",
            "zenmarket_retry_needed": "False",
            "fallback_used": "False",
            "title": title,
            "old_price_source": normalize_price_source(current_metadata.get("price_source", "unknown")),
            "new_price_source": normalize_price_source(current_metadata.get("price_source", "unknown")),
            "old_price_yen": current_metadata.get("price_yen", ""),
            "new_price_yen": current_metadata.get("price_yen", ""),
            "old_time_left": current_metadata.get("time_left", ""),
            "new_time_left": current_metadata.get("time_left", ""),
            "old_time_left_minutes": current_metadata.get("time_left_minutes", ""),
            "new_time_left_minutes": current_metadata.get("time_left_minutes", ""),
            "time_left_source": current_metadata.get("time_left_source", "unknown"),
            "raw_time_left_text": current_metadata.get("raw_time_left_text", ""),
            "suspicious_time_left": "False",
            "cleared_suspicious_time_left": "False",
            "old_auction_is_ended": current_metadata.get("auction_is_ended", ""),
            "new_auction_is_ended": current_metadata.get("auction_is_ended", ""),
            "old_current_price_yen": current_metadata.get("current_price_yen", ""),
            "new_current_price_yen": current_metadata.get("current_price_yen", ""),
            "old_buy_now_price_yen": current_metadata.get("buy_now_price_yen", ""),
            "new_buy_now_price_yen": current_metadata.get("buy_now_price_yen", ""),
            "old_bid_count": current_metadata.get("bid_count", ""),
            "new_bid_count": current_metadata.get("bid_count", ""),
            "updated": "False",
            "skipped_by_cache": "False",
            "error": "",
        }

        cache_entry = load_detail_enrichment_cache_entry(conn, url)
        current_suspicious = is_suspicious_time_left(
            current_metadata.get("time_left", ""),
            current_metadata.get("time_left_minutes", ""),
            current_metadata.get("time_left_source", ""),
            current_metadata.get("raw_time_left_text", ""),
        )
        if current_suspicious:
            stats["suspicious_detected"] += 1
            log_entry["suspicious_time_left"] = "True"
        if is_best_row:
            if normalize_price_source(current_metadata.get("price_source", "")) == "zenmarket_detail":
                best_already_zenmarket += 1
            if should_retry_zenmarket_for_best_row(row, cache_entry, force_refresh or zenmarket_full_refresh):
                best_retry_candidates += 1
        if (
            not force_refresh
            and not zenmarket_full_refresh
            and not current_suspicious
            and can_skip_detail_refresh_from_cache(cache_entry, current_metadata.get("price_source", ""))
        ):
            stats["skipped_cache"] += 1
            log_entry["skipped_by_cache"] = "True"
            log_entry["error"] = str(cache_entry.get("error", "") or "")
            log_entry["new_price_source"] = normalize_price_source(
                str(cache_entry.get("price_source", "") or log_entry["new_price_source"])
            )
            log_entry["time_left_source"] = str(cache_entry.get("time_left_source", "") or log_entry["time_left_source"])
            log_entry["raw_time_left_text"] = str(cache_entry.get("raw_time_left_text", "") or log_entry["raw_time_left_text"])
            log_rows.append(log_entry)
            continue

        stats["attempted"] += 1
        zenmarket_attempted = False
        zenmarket_success = False
        zenmarket_error = ""
        zenmarket_retry_count = 0
        zenmarket_attempts_for_item = 0
        zenmarket_metadata: Dict[str, str] = {}
        zenmarket_url = build_zenmarket_auction_url(url)
        should_force_best_zenmarket = is_best_row and should_retry_zenmarket_for_best_row(
            row,
            cache_entry,
            force_refresh or zenmarket_full_refresh,
        )
        should_try_zenmarket = bool(zenmarket_url) and (should_force_best_zenmarket or normalize_price_source(current_metadata.get("price_source", "")) != "zenmarket_detail")
        if should_try_zenmarket:
            zenmarket_attempted = True
            zenmarket_metadata, zenmarket_url, zenmarket_success, zenmarket_error, zenmarket_retry_count, zenmarket_attempts_for_item = fetch_zenmarket_detail_with_retries(
                session,
                url,
                max_retries=config.ZENMARKET_MAX_RETRIES_PER_ITEM,
                deadline_monotonic=zenmarket_deadline,
            )
            stats["zenmarket_attempted"] += zenmarket_attempts_for_item
            stats["zenmarket_retries"] += zenmarket_retry_count
            if is_best_row:
                stats["best_zenmarket_attempted"] += zenmarket_attempts_for_item
            log_entry["zenmarket_url"] = zenmarket_url
            log_entry["zenmarket_attempted"] = "True"
            log_entry["zenmarket_success"] = "True" if zenmarket_success else "False"
            log_entry["zenmarket_retry_count"] = str(zenmarket_retry_count)
            log_entry["zenmarket_error"] = zenmarket_error
            log_entry["zenmarket_retry_needed"] = "False" if zenmarket_success else "True"
            if zenmarket_success:
                stats["zenmarket_success"] += 1
                if is_best_row:
                    stats["best_zenmarket_success"] += 1
                if zenmarket_metadata.get("current_price_yen") and zenmarket_metadata.get("current_price_yen") != current_metadata.get("current_price_yen", ""):
                    stats["zenmarket_current_price_updated"] += 1
                if zenmarket_metadata.get("buy_now_price_yen") and zenmarket_metadata.get("buy_now_price_yen") != current_metadata.get("buy_now_price_yen", ""):
                    stats["zenmarket_buy_now_updated"] += 1
            else:
                stats["zenmarket_errors"] += 1
                if is_best_row:
                    stats["best_zenmarket_errors"] += 1
                stats["zenmarket_fail_after_retries"] += 1

        metadata = {}
        detail_source = normalize_price_source(current_metadata.get("price_source", "yahoo_search")) or "yahoo_search"
        yahoo_fallback_used = False
        if zenmarket_success:
            metadata = dict(zenmarket_metadata)
            detail_source = "zenmarket_detail"
        else:
            reader_url = build_reader_url_from_target_url(url)
            detail_markdown, fetch_error = fetch_reader_content_verbose(session, reader_url, f"detail page '{url}'")
            if not detail_markdown:
                stats["errors"] += 1
                consecutive_errors += 1
                log_entry["error"] = fetch_error or zenmarket_error or "empty_detail_response"
                log_rows.append(log_entry)
                upsert_detail_enrichment_cache(
                    conn,
                    url,
                    current_metadata,
                    success=False,
                    zenmarket_attempted=zenmarket_attempted,
                    zenmarket_success=zenmarket_success,
                    zenmarket_error=zenmarket_error,
                    error=log_entry["error"],
                )
                if consecutive_errors >= config.ZENMARKET_MAX_CONSECUTIVE_ERRORS:
                    logging.warning(
                        "Final deals detail double-check stopped after %s consecutive errors.",
                        consecutive_errors,
                    )
                    break
                if index < len(candidates):
                    time.sleep(row_sleep_seconds)
                continue

            yahoo_fallback_used = True
            stats["fallback_yahoo_used"] += 1
            log_entry["fallback_used"] = "True"
            metadata = extract_listing_metadata(
                title=title,
                context_text=detail_markdown,
                price_yen=int(to_float(deal.get("price_yen", 0))),
            )
            metadata["time_left_source"] = "yahoo_detail"
            if metadata.get("current_price_yen") or metadata.get("buy_now_price_yen"):
                detail_source = "yahoo_detail"

        if not is_meaningful_detail_metadata(metadata):
            stats["errors"] += 1
            consecutive_errors += 1
            log_entry["error"] = zenmarket_error or "empty_detail_response"
            log_rows.append(log_entry)
            upsert_detail_enrichment_cache(
                conn,
                url,
                current_metadata,
                success=False,
                zenmarket_attempted=zenmarket_attempted,
                zenmarket_success=zenmarket_success,
                zenmarket_error=zenmarket_error,
                error=log_entry["error"],
            )
            if consecutive_errors >= config.ZENMARKET_MAX_CONSECUTIVE_ERRORS:
                logging.warning(
                    "Final deals detail double-check stopped after %s consecutive errors.",
                    consecutive_errors,
                )
                break
            if index < len(candidates):
                time.sleep(row_sleep_seconds)
            continue

        consecutive_errors = 0
        merged = merge_listing_metadata_dicts(current_metadata, metadata)
        merged["price_source"] = detail_source
        if not merged.get("time_left_source"):
            merged["time_left_source"] = "zenmarket_detail" if detail_source == "zenmarket_detail" else "yahoo_detail" if yahoo_fallback_used else "unknown"
        suspicious_after = is_suspicious_time_left(
            merged.get("time_left", ""),
            merged.get("time_left_minutes", ""),
            merged.get("time_left_source", ""),
            merged.get("raw_time_left_text", ""),
        )
        if suspicious_after:
            stats["suspicious_detected"] += 1
            log_entry["suspicious_time_left"] = "True"
            merged = clear_time_left_fields(merged, source="unknown")
            merged["raw_time_left_text"] = ""
            suspicious_after = False
            stats["suspicious_cleared"] += 1
            log_entry["cleared_suspicious_time_left"] = "True"
        merged["price_yen"] = str(
            select_primary_purchase_price(
                listing_type=row.get("listing_type", LISTING_TYPE_UNKNOWN),
                base_price_yen=int(to_float(current_metadata.get("price_yen", 0))),
                current_price_yen=parse_optional_int(merged.get("current_price_yen", "")),
                buy_now_price_yen=parse_optional_int(merged.get("buy_now_price_yen", "")),
            )
        )
        changed = any(str(current_metadata.get(key, "")) != str(merged.get(key, "")) for key in current_metadata)

        if changed:
            deal.update(merged)
            persist_listing_metadata(conn, str(deal.get("listing_id", "")), merged)
            stats["updated"] += 1
            time_changed = (
                current_metadata.get("time_left", "") != merged.get("time_left", "")
                or current_metadata.get("time_left_minutes", "") != merged.get("time_left_minutes", "")
            )
            price_changed = (
                current_metadata.get("price_yen", "") != merged.get("price_yen", "")
                or current_metadata.get("price_source", "") != merged.get("price_source", "")
                or
                current_metadata.get("current_price_yen", "") != merged.get("current_price_yen", "")
                or current_metadata.get("buy_now_price_yen", "") != merged.get("buy_now_price_yen", "")
            )
            bid_changed = current_metadata.get("bid_count", "") != merged.get("bid_count", "")
            ended_changed = (
                current_metadata.get("auction_is_ended", "False") != "True"
                and merged.get("auction_is_ended", "False") == "True"
            )
            if time_changed:
                stats["time_updated"] += 1
            if price_changed:
                stats["price_updated"] += 1
            if bid_changed:
                stats["bid_updated"] += 1
            if ended_changed:
                stats["ended_detected"] += 1
            if merged.get("price_source") == "zenmarket_detail":
                stats["price_source_zenmarket_detail"] += 1
            elif merged.get("price_source") == "yahoo_detail":
                stats["price_source_yahoo_detail"] += 1
            elif merged.get("price_source") == "yahoo_search":
                stats["price_source_yahoo_search"] += 1

        log_entry.update(
            {
                "new_price_yen": merged.get("price_yen", ""),
                "new_time_left": merged.get("time_left", ""),
                "new_time_left_minutes": merged.get("time_left_minutes", ""),
                "time_left_source": merged.get("time_left_source", ""),
                "raw_time_left_text": merged.get("raw_time_left_text", ""),
                "new_auction_is_ended": merged.get("auction_is_ended", ""),
                "new_current_price_yen": merged.get("current_price_yen", ""),
                "new_buy_now_price_yen": merged.get("buy_now_price_yen", ""),
                "new_bid_count": merged.get("bid_count", ""),
                "new_price_source": merged.get("price_source", detail_source),
                "updated": "True" if changed else "False",
            }
        )
        log_rows.append(log_entry)
        upsert_detail_enrichment_cache(
            conn,
            url,
            merged,
            success=True,
            zenmarket_attempted=zenmarket_attempted,
            zenmarket_success=zenmarket_success,
            zenmarket_error=zenmarket_error,
            error="",
        )

        if index < len(candidates):
            time.sleep(row_sleep_seconds)

    stats["best_already_zenmarket"] = best_already_zenmarket
    stats["best_to_retry_zenmarket"] = best_retry_candidates
    stats["zenmarket_duration_seconds"] = int(time.monotonic() - zenmarket_run_started_at)

    refreshed_rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=existing_sheet_data.get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in deals
    ]
    refreshed_best_rows = sort_best_deals_rows(refreshed_rows)
    refreshed_best_by_url = {str(row.get("url", "")).strip(): row for row in refreshed_best_rows}
    best_non_zenmarket_after_run = sum(
        1
        for url in best_urls_in_candidates
        if normalize_price_source(refreshed_best_by_url.get(url, {}).get("price_source", "")) != "zenmarket_detail"
    )
    stats["best_non_zenmarket_after_run"] = best_non_zenmarket_after_run

    append_detail_enrichment_log(log_rows)
    if stats["candidates"]:
        logging.info(
            "Double-check annonces finales: candidats=%s | pages_tentees=%s | pages_mises_a_jour=%s | skipped_cache=%s | erreurs=%s | ended_via_detail=%s | time_updates=%s | price_updates=%s | bid_updates=%s",
            stats["candidates"],
            stats["attempted"],
            stats["updated"],
            stats["skipped_cache"],
            stats["errors"],
            stats["ended_detected"],
            stats["time_updated"],
            stats["price_updated"],
            stats["bid_updated"],
        )
        logging.info(
            "ZenMarket double-check: tentatives=%s | succes=%s | erreurs=%s | prix_actuel_updates=%s | achat_immediat_updates=%s | bid_count_updates=%s | fallback_yahoo=%s | price_source zenmarket_detail=%s | price_source yahoo_detail=%s | price_source yahoo_search=%s",
            stats["zenmarket_attempted"],
            stats["zenmarket_success"],
            stats["zenmarket_errors"],
            stats["zenmarket_current_price_updated"],
            stats["zenmarket_buy_now_updated"],
            stats["bid_updated"],
            stats["fallback_yahoo_used"],
            stats["price_source_zenmarket_detail"],
            stats["price_source_yahoo_detail"],
            stats["price_source_yahoo_search"],
        )
        logging.info(
            "ZenMarket Opportunités: candidates=%s | deja Détail ZenMarket=%s | a retenter ZenMarket=%s | tentatives ZenMarket=%s | succes ZenMarket=%s | retries effectues=%s | echecs apres retries=%s | fallback Yahoo utilise=%s | encore non ZenMarket apres run=%s | duree enrichissement ZenMarket=%ss",
            len(best_rows_all),
            stats["best_already_zenmarket"],
            stats["best_to_retry_zenmarket"],
            stats["best_zenmarket_attempted"],
            stats["best_zenmarket_success"],
            stats["zenmarket_retries"],
            stats["zenmarket_fail_after_retries"],
            stats["fallback_yahoo_used"],
            stats["best_non_zenmarket_after_run"],
            stats["zenmarket_duration_seconds"],
        )
    return stats


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
        GOOGLE_NEEDS_PRICE_WORKSHEET_NAME: len(sort_needs_price_rows(rows)),
        GOOGLE_BEST_WORKSHEET_NAME: len(sort_best_deals_rows(rows)),
        GOOGLE_DEALS_WORKSHEET_NAME: len(sort_deals_rows(rows, prefer_buy_now=bool(config.PREFER_BUY_NOW))),
    }


def summarize_auction_time_windows(rows: List[Dict[str, str]]) -> Dict[str, int]:
    counts = {"ended": 0, "active_known": 0, "active_unknown": 0, "lt_1h": 0, "lt_3h": 0}
    for row in rows:
        if row.get("listing_type") not in (LISTING_TYPE_AUCTION, LISTING_TYPE_LOW_START_AUCTION):
            continue
        if is_row_auction_ended(row):
            counts["ended"] += 1
            continue
        minutes_raw = row.get("time_left_minutes", "")
        if minutes_raw in ("", None):
            counts["active_unknown"] += 1
            continue
        minutes = int(to_float(minutes_raw))
        if minutes <= 0:
            continue
        counts["active_known"] += 1
        if minutes <= 60:
            counts["lt_1h"] += 1
        if minutes <= 180:
            counts["lt_3h"] += 1
    return counts


def _get_or_create_worksheet(spreadsheet: object, worksheet_name: str) -> object:
    try:
        return spreadsheet.worksheet(worksheet_name)
    except Exception as exc:  # noqa: BLE001
        exc_name = exc.__class__.__name__
        if exc_name != "WorksheetNotFound":
            raise
    return spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=80)


def _get_existing_worksheet(spreadsheet: object, worksheet_name: str) -> Optional[object]:
    try:
        return spreadsheet.worksheet(worksheet_name)
    except Exception:
        return None


def _get_existing_worksheet_from_candidates(spreadsheet: object, worksheet_names: Sequence[str]) -> Optional[object]:
    for worksheet_name in worksheet_names:
        worksheet = _get_existing_worksheet(spreadsheet, worksheet_name)
        if worksheet is not None:
            return worksheet
    return None


def _rename_worksheet_if_needed(spreadsheet: object, old_name: str, new_name: str) -> bool:
    old_ws = _get_existing_worksheet(spreadsheet, old_name)
    if old_ws is None or old_name == new_name:
        return False
    if _get_existing_worksheet(spreadsheet, new_name) is not None:
        return False
    try:
        old_ws.update_title(new_name)
        return True
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not rename worksheet '%s' -> '%s': %s", old_name, new_name, exc)
        return False


def _migrate_google_worksheet_names(spreadsheet: object) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    for old_name, new_name in GOOGLE_SHEET_MIGRATIONS:
        results[f"{old_name}->{new_name}"] = _rename_worksheet_if_needed(spreadsheet, old_name, new_name)
    return results


def _sync_mode_emploi_worksheet(spreadsheet: object) -> bool:
    worksheet = _get_or_create_worksheet(spreadsheet, GOOGLE_MODE_EMPLOI_WORKSHEET_NAME)
    payload = [
        ["Mode d’emploi"],
        ["Opportunités"],
        ["Onglet principal à regarder pour décider quoi acheter."],
        ["Pas de saisie manuelle de prix ici."],
        ["Regarder surtout Prix Japon ¥, Prix actuel ¥, Prix achat immédiat ¥, Coût total estimé €, Prix marché €, Marge estimée € et ROI %."],
        ["Toutes les lignes doivent idéalement avoir Source prix achat = Détail ZenMarket quand possible."],
        [""],
        ["Prix à remplir"],
        ["Onglet où remplir les prix de revente manuels."],
        ["Utiliser les liens Cardmarket / eBay vendu / PriceCharting pour renseigner le prix de revente."],
        ["Le bot recalcule ensuite automatiquement le ROI et la marge."],
        [""],
        ["Historique"],
        ["Historique complet / debug, utile pour contrôler les annonces ignorées ou déjà terminées."],
        [""],
        ["Définitions utiles"],
        ["Prix Japon ¥ = prix utilisé pour calculer le coût."],
        ["Prix actuel ¥ = prix actuel de l’enchère."],
        ["Prix achat immédiat ¥ = achat direct si disponible."],
        ["Source prix achat = source du prix d’achat, idéalement Détail ZenMarket."],
        ["Prix revente manuel € = prix que vous renseignez vous-même."],
        ["Prix marché € = prix utilisé pour calculer la rentabilité."],
        ["Marge estimée € = gain estimé après coût."],
        ["ROI % = rentabilité estimée."],
        ["Notes = zone libre utilisateur."],
        [""],
        ["Les seules colonnes à modifier manuellement sont dans Prix à remplir"],
        ["Prix revente manuel €"],
        ["Notes"],
        [""],
        ["Ne pas modifier les colonnes non jaunes."],
    ]
    worksheet.clear()
    worksheet.update(range_name="A1", values=payload, value_input_option="RAW")
    worksheet_id = getattr(worksheet, "id", None)
    if worksheet_id is not None:
        spreadsheet.batch_update(
            {
                "requests": build_base_sheet_format_requests(
                    worksheet_id=worksheet_id,
                    header_count=1,
                    row_count=max(0, len(payload) - 1),
                )
                + [
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": worksheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": 1,
                            }
                        }
                    }
                ]
            }
        )
    return True


def _cleanup_inactive_google_worksheets(spreadsheet: object) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    inactive_names = [
        GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME,
        GOOGLE_BUY_NOW_WORKSHEET_NAME,
        GOOGLE_LEGACY_BEST_WORKSHEET_NAME,
        GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME,
        GOOGLE_LEGACY_DEALS_WORKSHEET_NAME,
    ]
    active_names = {
        GOOGLE_MODE_EMPLOI_WORKSHEET_NAME,
        GOOGLE_BEST_WORKSHEET_NAME,
        GOOGLE_NEEDS_PRICE_WORKSHEET_NAME,
        GOOGLE_DEALS_WORKSHEET_NAME,
    }
    for worksheet_name in inactive_names:
        if worksheet_name in active_names:
            continue
        worksheet = _get_existing_worksheet(spreadsheet, worksheet_name)
        if worksheet is None:
            results[worksheet_name] = False
            continue
        try:
            spreadsheet.del_worksheet(worksheet)
            results[worksheet_name] = False
            continue
        except Exception:
            try:
                worksheet.clear()
                worksheet.update(
                    range_name="A1",
                    values=[["Onglet désactivé — utiliser Opportunités"]],
                    value_input_option="RAW",
                )
                results[worksheet_name] = True
            except Exception:
                results[worksheet_name] = True
    return results


def _read_existing_sheet_rows(
    worksheet: object,
    headers: List[str],
    extra_headers: Optional[List[str]] = None,
) -> Dict[str, Dict[str, str]]:
    existing_values = worksheet.get_all_values()
    existing_by_url: Dict[str, Dict[str, str]] = {}
    if not existing_values:
        return existing_by_url

    all_headers = list(dict.fromkeys(list(headers) + list(extra_headers or [])))
    existing_headers = [get_internal_header_name(header) for header in existing_values[0]]
    header_index = {h: i for i, h in enumerate(existing_headers)}
    for row in existing_values[1:]:
        row_dict: Dict[str, str] = {}
        for col_name in all_headers:
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
        legacy_manual_headers = ["manual_market_price_eur", "manual_price_source", "manual_price_confidence", "manual_status", "notes"]
        deals_sources = []
        best_sources = []
        needs_price_sources = []

        for worksheet_name in (GOOGLE_DEALS_WORKSHEET_NAME, GOOGLE_LEGACY_DEALS_WORKSHEET_NAME):
            worksheet = _get_existing_worksheet(spreadsheet, worksheet_name)
            if worksheet is not None:
                deals_sources.append(_read_existing_sheet_rows(worksheet, GOOGLE_DEALS_HEADERS, extra_headers=legacy_manual_headers))
        for worksheet_name in (GOOGLE_BEST_WORKSHEET_NAME, GOOGLE_LEGACY_BEST_WORKSHEET_NAME):
            worksheet = _get_existing_worksheet(spreadsheet, worksheet_name)
            if worksheet is not None:
                best_sources.append(_read_existing_sheet_rows(worksheet, GOOGLE_BEST_DEALS_HEADERS, extra_headers=legacy_manual_headers))
        for worksheet_name in (GOOGLE_NEEDS_PRICE_WORKSHEET_NAME, GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME):
            worksheet = _get_existing_worksheet(spreadsheet, worksheet_name)
            if worksheet is not None:
                needs_price_sources.append(_read_existing_sheet_rows(worksheet, GOOGLE_NEEDS_PRICE_HEADERS, extra_headers=legacy_manual_headers))

        deals_existing = merge_manual_values_by_priority(*deals_sources)
        best_existing = merge_manual_values_by_priority(*best_sources)
        needs_price_existing = merge_manual_values_by_priority(*needs_price_sources)
        return merge_manual_values_by_priority(
            needs_price_existing,
            best_existing,
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
    existing_by_url = _read_existing_sheet_rows(
        worksheet,
        headers,
        extra_headers=["manual_market_price_eur", "manual_price_source", "manual_price_confidence", "manual_status", "notes"],
    )

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
    needs_price_rows = sort_needs_price_rows(sheet_rows)

    try:
        migration_results = _migrate_google_worksheet_names(spreadsheet)
        mode_emploi_exists = _sync_mode_emploi_worksheet(spreadsheet)
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
        needs_price_count = _sync_single_google_worksheet(
            spreadsheet=spreadsheet,
            worksheet_name=GOOGLE_NEEDS_PRICE_WORKSHEET_NAME,
            headers=GOOGLE_NEEDS_PRICE_HEADERS,
            rows=needs_price_rows,
        )
        obsolete_results = _cleanup_inactive_google_worksheets(spreadsheet)
    except Exception as exc:  # noqa: BLE001
        logging.error("Google worksheet write failed: %s", exc)
        return {}

    return {
        "counts": {
            GOOGLE_MODE_EMPLOI_WORKSHEET_NAME: 1 if mode_emploi_exists else 0,
            GOOGLE_DEALS_WORKSHEET_NAME: deals_count,
            GOOGLE_BEST_WORKSHEET_NAME: best_count,
            GOOGLE_NEEDS_PRICE_WORKSHEET_NAME: needs_price_count,
        },
        "worksheets": {
            GOOGLE_MODE_EMPLOI_WORKSHEET_NAME: mode_emploi_exists,
            GOOGLE_DEALS_WORKSHEET_NAME: True,
            GOOGLE_BEST_WORKSHEET_NAME: True,
            GOOGLE_NEEDS_PRICE_WORKSHEET_NAME: True,
            GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME: obsolete_results.get(GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME, False),
            GOOGLE_BUY_NOW_WORKSHEET_NAME: obsolete_results.get(GOOGLE_BUY_NOW_WORKSHEET_NAME, False),
            GOOGLE_LEGACY_DEALS_WORKSHEET_NAME: obsolete_results.get(GOOGLE_LEGACY_DEALS_WORKSHEET_NAME, False),
            GOOGLE_LEGACY_BEST_WORKSHEET_NAME: obsolete_results.get(GOOGLE_LEGACY_BEST_WORKSHEET_NAME, False),
            GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME: obsolete_results.get(GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME, False),
        },
        "migrations": migration_results,
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


def main(best_only: bool = False, force_detail_refresh: bool = False, zenmarket_full_refresh: bool = False) -> None:
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
            else:
                refresh_existing_deal(conn, scored)

        if index < len(rules):
            time.sleep(config.REQUEST_SLEEP_BETWEEN_QUERIES_SECONDS)

    all_deals = fetch_all_deals(conn)
    existing_sheet_data = load_google_sheet_existing_values()
    detail_check_stats = double_check_final_deals_details(
        session=session,
        conn=conn,
        deals=all_deals,
        existing_sheet_data=existing_sheet_data,
        force_refresh=force_detail_refresh or zenmarket_full_refresh,
        zenmarket_full_refresh=zenmarket_full_refresh,
    )
    all_deals = fetch_all_deals(conn)
    csv_rows = export_csv(all_deals, config.CSV_EXPORT_PATH, conn=conn, existing_sheet_data=existing_sheet_data)
    gs_result = export_to_google_sheets(all_deals, conn=conn, existing_sheet_data=existing_sheet_data)

    current_run_listing_ids = {deal.listing_id for deal in all_eligible_this_run}
    current_run_deals = [deal for deal in all_deals if str(deal.get("listing_id", "")) in current_run_listing_ids]
    summary_counts = summarize_decisions(current_run_deals, conn=conn, existing_sheet_data=existing_sheet_data)
    listing_type_counts = summarize_listing_types(current_run_deals, conn=conn, existing_sheet_data=existing_sheet_data)
    sheet_view_counts = summarize_sheet_views(current_run_deals, conn=conn, existing_sheet_data=existing_sheet_data)
    current_run_rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in current_run_deals
    ]
    auction_time_counts = summarize_auction_time_windows(current_run_rows)
    all_sheet_rows = [
        build_sheet_row(
            deal,
            conn=conn,
            existing_sheet_values=(existing_sheet_data or {}).get(str(deal.get("url", "")).strip(), {}),
        )
        for deal in all_deals
    ]
    best_rows_all = sort_best_deals_rows(all_sheet_rows)
    detail_check_stats["best_lines_total"] = len(best_rows_all)
    detail_check_stats["best_lines_buy_now_price"] = sum(1 for row in best_rows_all if to_float(row.get("buy_now_price_yen", "")) > 0)
    detail_check_stats["best_lines_auction_plus_buy_now"] = sum(
        1
        for row in best_rows_all
        if to_float(row.get("current_price_yen", "")) > 0 and to_float(row.get("buy_now_price_yen", "")) > 0
    )
    detail_check_stats["best_lines_time_9_days"] = sum(
        1 for row in best_rows_all if normalize_numeric_text(row.get("time_left", "")).casefold() in {"9 jours", "9 days", "9 day"}
    )
    detail_check_stats["best_lines_minutes_12960"] = sum(
        1 for row in best_rows_all if parse_optional_int(row.get("time_left_minutes", "")) == 12960
    )
    detail_check_stats["price_source_zenmarket_detail"] = sum(
        1 for row in best_rows_all if normalize_price_source(row.get("price_source", "") or "") == "zenmarket_detail"
    )
    detail_check_stats["price_source_yahoo_detail"] = sum(
        1 for row in best_rows_all if normalize_price_source(row.get("price_source", "") or "") == "yahoo_detail"
    )
    detail_check_stats["price_source_yahoo_search"] = sum(
        1 for row in best_rows_all if normalize_price_source(row.get("price_source", "") or "") == "yahoo_search"
    )
    detail_check_stats["price_source_sqlite_cache"] = sum(
        1 for row in best_rows_all if normalize_price_source(row.get("price_source", "") or "") == "sqlite_cache"
    )
    detail_check_stats["price_source_unknown"] = sum(
        1
        for row in best_rows_all
        if normalize_price_source(row.get("price_source", "") or "")
        not in {"zenmarket_detail", "yahoo_detail", "yahoo_search", "sqlite_cache"}
    )
    detail_check_stats["best_without_zenmarket_link"] = sum(
        1 for row in best_rows_all if not str(row.get("link_for_zenmarket", "") or "").strip()
    )
    detail_check_stats["best_time_source_zenmarket"] = sum(1 for row in best_rows_all if row.get("time_left_source") == "zenmarket_detail")
    detail_check_stats["best_time_source_yahoo"] = sum(1 for row in best_rows_all if row.get("time_left_source") == "yahoo_detail")
    detail_check_stats["best_time_source_unknown"] = sum(
        1 for row in best_rows_all if row.get("time_left_source", "") not in {"zenmarket_detail", "yahoo_detail"}
    )

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
        gs_worksheets = gs_result.get("worksheets", {}) if isinstance(gs_result, dict) else {}
        logging.info(
            "Google Sheets synced rows: Mode d’emploi=%s | Historique=%s | Opportunités=%s | Prix à remplir=%s (%s)",
            gs_counts.get(GOOGLE_MODE_EMPLOI_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_DEALS_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_BEST_WORKSHEET_NAME, 0),
            gs_counts.get(GOOGLE_NEEDS_PRICE_WORKSHEET_NAME, 0),
            gs_result.get("sheet_name", config.GOOGLE_SHEET_NAME) if isinstance(gs_result, dict) else config.GOOGLE_SHEET_NAME,
        )
        logging.info(
            "Google Sheets onglets: Mode d’emploi=%s | Opportunités=%s | Prix à remplir=%s | Historique=%s | Best Deals actif=%s | Needs Price actif=%s | Deals actif=%s | Auctions Watch présent=%s | Buy Now Deals présent=%s",
            gs_worksheets.get(GOOGLE_MODE_EMPLOI_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_BEST_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_NEEDS_PRICE_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_DEALS_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_LEGACY_BEST_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_LEGACY_NEEDS_PRICE_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_LEGACY_DEALS_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_AUCTIONS_WATCH_WORKSHEET_NAME, False),
            gs_worksheets.get(GOOGLE_BUY_NOW_WORKSHEET_NAME, False),
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
        "Views: Opportunités=%s | Prix à remplir=%s | Historique=%s",
        sheet_view_counts.get(GOOGLE_BEST_WORKSHEET_NAME, 0),
        sheet_view_counts.get(GOOGLE_NEEDS_PRICE_WORKSHEET_NAME, 0),
        sheet_view_counts.get(GOOGLE_DEALS_WORKSHEET_NAME, 0),
    )
    logging.info(
        "Enchères terminées détectées : %s | Enchères actives avec temps connu : %s | Enchères actives temps inconnu : %s | Enchères fin < 1h : %s | Enchères fin < 3h : %s",
        auction_time_counts.get("ended", 0),
        auction_time_counts.get("active_known", 0),
        auction_time_counts.get("active_unknown", 0),
        auction_time_counts.get("lt_1h", 0),
        auction_time_counts.get("lt_3h", 0),
    )
    logging.info(
        "Double-check annonces finales : candidats=%s | pages tentées=%s | pages mises à jour=%s | skipped cache=%s | erreurs=%s | enchères terminées via détail=%s | temps restant mis à jour=%s | prix mis à jour=%s | enchères count mis à jour=%s",
        detail_check_stats.get("candidates", 0),
        detail_check_stats.get("attempted", 0),
        detail_check_stats.get("updated", 0),
        detail_check_stats.get("skipped_cache", 0),
        detail_check_stats.get("errors", 0),
        detail_check_stats.get("ended_detected", 0),
        detail_check_stats.get("time_updated", 0),
        detail_check_stats.get("price_updated", 0),
        detail_check_stats.get("bid_updated", 0),
    )
    logging.info(
        "Opportunités : lignes=%s | avec prix achat immédiat=%s | enchère+achat immédiat=%s | temps=9 jours=%s | minutes=12960=%s | temps suspects détectés=%s | temps suspects effacés=%s | source prix achat ZenMarket=%s | source temps ZenMarket=%s | source temps Yahoo=%s | source temps inconnue=%s",
        detail_check_stats.get("best_lines_total", 0),
        detail_check_stats.get("best_lines_buy_now_price", 0),
        detail_check_stats.get("best_lines_auction_plus_buy_now", 0),
        detail_check_stats.get("best_lines_time_9_days", 0),
        detail_check_stats.get("best_lines_minutes_12960", 0),
        detail_check_stats.get("suspicious_detected", 0),
        detail_check_stats.get("suspicious_cleared", 0),
        detail_check_stats.get("price_source_zenmarket_detail", 0),
        detail_check_stats.get("best_time_source_zenmarket", 0),
        detail_check_stats.get("best_time_source_yahoo", 0),
        detail_check_stats.get("best_time_source_unknown", 0),
    )
    logging.info(
        "Opportunités source prix achat : total Opportunités=%s | Détail ZenMarket=%s | Détail Yahoo=%s | Résultat Yahoo=%s | Cache SQLite=%s | Unknown=%s | Opportunités sans lien ZenMarket=%s | Opportunités ZenMarket tentées=%s | Opportunités ZenMarket succès=%s | Opportunités ZenMarket erreurs=%s",
        detail_check_stats.get("best_lines_total", 0),
        detail_check_stats.get("price_source_zenmarket_detail", 0),
        detail_check_stats.get("price_source_yahoo_detail", 0),
        detail_check_stats.get("price_source_yahoo_search", 0),
        detail_check_stats.get("price_source_sqlite_cache", 0),
        detail_check_stats.get("price_source_unknown", 0),
        detail_check_stats.get("best_without_zenmarket_link", 0),
        detail_check_stats.get("best_zenmarket_attempted", 0),
        detail_check_stats.get("best_zenmarket_success", 0),
        detail_check_stats.get("best_zenmarket_errors", 0),
    )
    logging.info("Telegram alerts sent: %s", telegram_sent)


if __name__ == "__main__":
    cli_args = parse_args()
    if cli_args.needs_price_report:
        run_needs_price_report_only()
    elif cli_args.auto_price_test:
        run_auto_price_test()
    else:
        main(
            best_only=cli_args.best,
            force_detail_refresh=cli_args.force_detail_refresh,
            zenmarket_full_refresh=cli_args.zenmarket_full_refresh,
        )
