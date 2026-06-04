"""Configuration for pokemon-watch-bot.

Edit this file to tune search behavior, risk model and alerting.
"""

from __future__ import annotations

import os


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

# ----- Runtime paths -----
DATABASE_PATH = "deals.db"
CSV_EXPORT_PATH = "pokemon_deals_export.csv"

# ----- Yahoo Auctions scraping -----
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 2
REQUEST_SLEEP_BETWEEN_QUERIES_SECONDS = 2.0
MAX_RESULTS_PER_RULE = 50
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# We use a public markdown mirror to avoid direct anti-bot blocking.
YAHOO_SEARCH_URL_TEMPLATE = "https://auctions.yahoo.co.jp/search/search?p={query}&auccat=0"
JINA_READER_PREFIX = "https://r.jina.ai/http://"

# ----- Financial model (editable) -----
# Example: if 1 EUR ~= 170 JPY then EUR_TO_JPY = 170.0
EUR_TO_JPY = 170.0
ZENMARKET_SERVICE_FEE_YEN = 500
ZENMARKET_PAYMENT_FEE_RATE = 0.035
ESTIMATED_DOMESTIC_SHIPPING_YEN = 900
ESTIMATED_INTERNATIONAL_SHIPPING_YEN = 2600
VAT_RATE = 0.20
SAFETY_MARGIN_RATE = 0.12

# Scoring thresholds
MIN_PROFIT_EUR = 15.0
MIN_ROI_PERCENT = 18.0

# ----- Alerting -----
TELEGRAM_ENABLED = env_bool("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SECONDS = 20
MAX_TELEGRAM_ALERTS_PER_RUN = 7

# ----- Google Sheets export (optional) -----
GOOGLE_SHEETS_ENABLED = env_bool("GOOGLE_SHEETS_ENABLED", False)
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Pokemon Deals Watch")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Deals")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google_service_account.json")
PREFER_BUY_NOW = env_bool("PREFER_BUY_NOW", True)

# ----- Automatic market price resolver (safe by default) -----
AUTO_PRICE_ENABLED = env_bool("AUTO_PRICE_ENABLED", False)

PRICE_SOURCES_PRIORITY = [
    "pricecharting",
    "apify_ebay_sold",
    "ebay_browse_active",
]

PRICECHARTING_ENABLED = env_bool("PRICECHARTING_ENABLED", False)
PRICECHARTING_API_TOKEN = os.getenv("PRICECHARTING_API_TOKEN", "")

APIFY_EBAY_SOLD_ENABLED = env_bool("APIFY_EBAY_SOLD_ENABLED", False)
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
APIFY_EBAY_SOLD_ACTOR_ID = os.getenv("APIFY_EBAY_SOLD_ACTOR_ID", "")

EBAY_BROWSE_ENABLED = env_bool("EBAY_BROWSE_ENABLED", False)
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
EBAY_MARKETPLACE = os.getenv("EBAY_MARKETPLACE", "EBAY_FR")

AUTO_PRICE_MIN_CONFIDENCE = "MEDIUM"
AUTO_PRICE_CACHE_DAYS = 7

# ----- Filtering -----
GLOBAL_BLACKLIST_KEYWORDS = [
    "空箱",
    "箱のみ",
    "パックのみ",
    "シュリンクなし",
    "シュリンク無し",
    "開封済み",
    "サーチ済み",
    "オリパ",
    "ジャンク",
    "傷あり",
    "破損",
    "偽物",
    "レプリカ",
    "コピー",
    "ノーマルのみ",
    "説明必読",
    "返品不可",
    "海外版",
    "韓国版",
    "中国版",
    "デジタル",
    "画像のみ",
]

# Each rule is independent and can use a different market estimate / risk profile.
SEARCH_RULES = [
    {
        "name": "Pokemon 151 sealed box",
        "query": "ポケモンカード 151 BOX シュリンク付き",
        "market_price_eur": 150.0,
        "max_price_yen": 22000,
        "required_keywords": ["151", "BOX", "シュリンク"],
        "blacklist_keywords": [],
    },
    {
        "name": "Pokemon sealed box generic",
        "query": "ポケモンカード 未開封 BOX シュリンク付き",
        "market_price_eur": 95.0,
        "max_price_yen": 16000,
        "required_keywords": ["BOX", "シュリンク"],
        "blacklist_keywords": [],
    },
    {
        "name": "Pokemon retirement lot",
        "query": "ポケモンカード 引退品 まとめ売り",
        "market_price_eur": 220.0,
        "max_price_yen": 25000,
        "required_keywords": ["引退品"],
        "blacklist_keywords": [],
    },
    {
        "name": "Pokemon PSA10",
        "query": "ポケモンカード PSA10",
        "market_price_eur": 80.0,
        "max_price_yen": 14000,
        "required_keywords": ["PSA10"],
        "blacklist_keywords": [],
    },
    {
        "name": "Terastal Festival sealed box",
        "query": "テラスタルフェス BOX シュリンク付き",
        "market_price_eur": 90.0,
        "max_price_yen": 14000,
        "required_keywords": ["BOX", "シュリンク"],
        "blacklist_keywords": [],
    },
]

# ----- Listing type signals -----
LISTING_TYPE_KEYWORDS = {
    "buy_now": ["即決", "即決価格", "即決可", "すぐ購入"],
    "fixed_price": ["定額", "フリマ", "PayPayフリマ"],
    "low_start_auction": ["1円スタート！！", "1円スタート", "1円", "売り切り"],
    "auction": [],
}

# ----- Keyword files (optional) -----
KEYWORDS_BUY_NOW_FILE = "keywords_buy_now.txt"
KEYWORDS_AUCTION_FILE = "keywords_auction.txt"
KEYWORDS_LOTS_FILE = "keywords_lots.txt"
BLACKLIST_WORDS_FILE = "blacklist_words.txt"

# If text files are missing, these defaults are used.
KEYWORDS_BUY_NOW_DEFAULT = [
    "ポケモンカード BOX 即決",
    "ポケカ BOX 即決",
    "ポケモンカード シュリンク付き 即決",
    "ポケモンカード 未開封 即決",
    "ポケモンカード 151 BOX 即決",
    "ポケモンカード151 BOX 即決",
    "テラスタルフェス BOX 即決",
    "シャイニートレジャー BOX 即決",
    "クレイバースト BOX 即決",
    "スノーハザード BOX 即決",
    "黒炎の支配者 BOX 即決",
    "VSTARユニバース BOX 即決",
    "VMAXクライマックス BOX 即決",
    "ロストアビス BOX 即決",
    "イーブイヒーローズ BOX 即決",
    "蒼空ストリーム BOX 即決",
    "白熱のアルカナ BOX 即決",
    "ポケモンカード PSA10 即決",
    "ポケカ PSA10 即決",
    "ポケモンカード まとめ売り 即決",
    "ポケモンカード 引退品 即決",
]

KEYWORDS_AUCTION_DEFAULT = [
    "ポケモンカード BOX シュリンク付き",
    "ポケカ BOX シュリンク付き",
    "ポケモンカード 未開封BOX",
    "ポケカ 未開封BOX",
    "ポケモンカード 新品未開封 BOX",
    "ポケモンカード 絶版BOX",
    "ポケモンカード ハイクラスパック BOX",
    "ポケモンカード 151 BOX シュリンク付き",
    "ポケモンカード151 シュリンク付き",
    "テラスタルフェス BOX シュリンク付き",
    "シャイニートレジャー BOX シュリンク付き",
    "クレイバースト BOX シュリンク付き",
    "スノーハザード BOX シュリンク付き",
    "黒炎の支配者 BOX シュリンク付き",
    "VSTARユニバース BOX シュリンク付き",
    "VMAXクライマックス BOX シュリンク付き",
    "ロストアビス BOX シュリンク付き",
    "イーブイヒーローズ BOX",
    "蒼空ストリーム BOX",
    "双璧のファイター BOX",
    "白熱のアルカナ BOX",
    "レイジングサーフ BOX",
    "スペシャルBOX ポケモンカード",
]

KEYWORDS_LOTS_DEFAULT = [
    "ポケモンカード 大量",
    "ポケカ 大量",
    "ポケモンカード まとめ",
    "ポケモンカード まとめ売り",
    "ポケモンカード 引退品",
    "ポケカ 引退品",
    "ポケモンカード コレクション 引退",
    "ポケモンカード 旧裏 まとめ",
    "ポケモンカード キラ まとめ売り",
    "ポケモンカード SR SAR まとめ売り",
    "ポケモンカード プロモ まとめ売り",
    "ポケモンカード PSA まとめ売り",
]

# Rule defaults for generated keyword searches
BUY_NOW_RULE_DEFAULTS = {
    "market_price_eur": 120.0,
    "max_price_yen": 26000,
}
AUCTION_RULE_DEFAULTS = {
    "market_price_eur": 100.0,
    "max_price_yen": 22000,
}
LOTS_RULE_DEFAULTS = {
    "market_price_eur": 220.0,
    "max_price_yen": 30000,
}
